"""Prepare and deliver equivalent-prefix wrong-nudge susceptibility experiments."""

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from spotter.app_server import (
    AppServerControlError,
    AppServerError,
    AppServerEvent,
    AppServerRpcError,
    CodexAppServerClient,
)
from spotter.experiment import (
    CONTROL_PROMPT,
    EXPERIMENT_RESULT_SCHEMA,
    EXPERIMENT_RESULT_SCHEMA_VERSION,
    ArmClassification,
    append_experiment_result,
)
from spotter.paths import sanitize_session, spotter_home
from spotter.replay import ForkPlan, fork
from spotter.task_corpus import CommandResult, TaskManifest, file_digest, run_task_checks
from spotter.wrong_nudge_corpus import (
    FramingCondition,
    WrongNudge,
    WrongNudgeArm,
    build_wrong_nudge_arms,
)


class WrongNudgeExperimentError(ValueError):
    """Wrong-nudge arms cannot be compared or safely delivered."""


WRONG_NUDGE_RESULT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PreparedWrongNudgeArm:
    arm: WrongNudgeArm
    fork_session_id: str
    worktree: str
    rollout: str
    fork_manifest: str


class DeliveryOutcome(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    CONTROL_NO_STEER = "CONTROL_NO_STEER"
    RPC_ACCEPTED = "RPC_ACCEPTED"
    FAILED_OR_STALE = "DELIVERY_FAILED_OR_STALE"
    ACCEPTANCE_UNKNOWN = "ACCEPTANCE_UNKNOWN"


@dataclass(frozen=True)
class WrongNudgeDeliveryResult:
    condition: FramingCondition
    fork_session_id: str
    turn_id: str | None
    continuation_client_user_message_id: str
    steer_client_user_message_id: str | None
    delivery_outcome: DeliveryOutcome
    completion_observed: bool
    turn_status: str | None
    diagnostic: str | None = None


@dataclass(frozen=True)
class WrongNudgeMechanicalResult:
    experiment_id: str
    condition: FramingCondition
    wrong_nudge_id: str
    wrong_nudge_manifest_sha256: str
    wrong_nudge_source_task: str
    payload_version: int
    source_session_id: str
    source_step: int
    prefix_id: str
    environment_fingerprint: str
    fork_session_id: str
    fork_manifest: str
    worktree: str
    turn_id: str | None
    continuation_client_user_message_id: str
    steer_client_user_message_id: str | None
    delivery_outcome: DeliveryOutcome
    completion_observed: bool
    turn_status: str | None
    delivery_diagnostic: str | None
    task_id: str
    task_manifest_sha256: str
    fixture_sha256: str
    classification: ArmClassification
    checks: tuple[CommandResult, ...]
    scoring_diagnostic: str | None
    started_at: str
    ended_at: str
    result_schema_version: int = EXPERIMENT_RESULT_SCHEMA_VERSION
    wrong_nudge_result_schema_version: int = WRONG_NUDGE_RESULT_SCHEMA_VERSION


class WrongNudgeClient(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def resume_thread(self, thread_id: str) -> Mapping[str, Any]: ...

    async def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        cwd: str | None = None,
        client_user_message_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    async def steer(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
        *,
        client_user_message_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    async def next_event(self) -> AppServerEvent: ...


ClientFactory = Callable[[str, float], WrongNudgeClient]


def prepare_wrong_nudge_arms(
    nudge: WrongNudge,
    source_session_id: str,
    source_step: int,
    *,
    repo: Path | None = None,
    codex_home: Path | None = None,
    environment_resources: Sequence[str] = (),
    environment_variables: Sequence[str] = (),
    environment_venv_or_cache: Sequence[str] = (),
) -> tuple[PreparedWrongNudgeArm, ...]:
    """Fork every condition independently, then refuse non-equivalent prefixes."""

    if not source_session_id.strip():
        raise WrongNudgeExperimentError("source_session_id must be non-empty text")
    if isinstance(source_step, bool) or source_step < 0:
        raise WrongNudgeExperimentError("source_step must be non-negative")
    plans = tuple(
        fork(
            source_session_id,
            source_step,
            repo=repo,
            codex_home=codex_home,
            environment_resources=environment_resources,
            environment_variables=environment_variables,
            environment_venv_or_cache=environment_venv_or_cache,
        )
        for _ in FramingCondition
    )
    prefix_id, environment_fingerprint = _preflight_forks(plans)
    arms = build_wrong_nudge_arms(
        nudge,
        source_session_id=source_session_id,
        source_step=source_step,
        prefix_id=prefix_id,
        environment_fingerprint=environment_fingerprint,
    )
    return tuple(
        PreparedWrongNudgeArm(
            arm=arm,
            fork_session_id=plan.session_id,
            worktree=plan.worktree,
            rollout=plan.rollout,
            fork_manifest=plan.manifest or "",
        )
        for arm, plan in zip(arms, plans, strict=True)
    )


async def deliver_wrong_nudge_arms(
    prepared: tuple[PreparedWrongNudgeArm, ...],
    endpoint: str,
    *,
    timeout: float = 1800,
    request_timeout: float = 10,
    client_factory: ClientFactory | None = None,
) -> tuple[WrongNudgeDeliveryResult, ...]:
    """Run continuation turns and use real ``turn/steer`` for non-control arms."""

    if timeout <= 0:
        raise WrongNudgeExperimentError("timeout must be positive")
    if request_timeout <= 0:
        raise WrongNudgeExperimentError("request_timeout must be positive")
    _preflight_prepared(prepared)
    factory = client_factory or _default_client
    return tuple(
        [
            await _deliver_wrong_nudge_arm(row, endpoint, timeout, request_timeout, factory)
            for row in prepared
        ]
    )


def score_wrong_nudge_arms(
    prepared: tuple[PreparedWrongNudgeArm, ...],
    deliveries: tuple[WrongNudgeDeliveryResult, ...],
    task: TaskManifest,
    *,
    output: Path | None = None,
) -> tuple[Path, tuple[WrongNudgeMechanicalResult, ...]]:
    """Run frozen mechanical checks and durably record each completed arm."""

    _preflight_prepared(prepared)
    delivery_by_condition = _preflight_deliveries(prepared, deliveries)
    provenance = prepared[0].arm
    if task.task_id != provenance.source_task:
        raise WrongNudgeExperimentError("TASK_PROVENANCE_MISMATCH")
    experiment_id = str(uuid.uuid4())
    result_path = output or _wrong_nudge_results_path(prepared[0], experiment_id)
    if result_path.exists():
        raise WrongNudgeExperimentError(f"result path already exists: {result_path}")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    task_manifest_sha256 = file_digest(task.path)
    append_experiment_result(
        result_path,
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "wrong_nudge_result_schema_version": WRONG_NUDGE_RESULT_SCHEMA_VERSION,
            "meta": True,
            "experiment_id": experiment_id,
            "experiment_mode": "wrong-nudge",
            "wrong_nudge_id": provenance.wrong_nudge_id,
            "wrong_nudge_manifest_sha256": provenance.manifest_sha256,
            "wrong_nudge_source_task": provenance.source_task,
            "payload_version": provenance.payload_version,
            "source_session_id": provenance.source_session_id,
            "source_step": provenance.source_step,
            "prefix_id": provenance.prefix_id,
            "environment_fingerprint": provenance.environment_fingerprint,
            "task_id": task.task_id,
            "task_manifest_sha256": task_manifest_sha256,
            "fixture_sha256": task.source_sha256,
            "started_at": datetime.now(UTC).isoformat(),
        },
    )

    results: list[WrongNudgeMechanicalResult] = []
    for arm in prepared:
        started_at = datetime.now(UTC).isoformat()
        delivery = delivery_by_condition[arm.arm.condition]
        if delivery.completion_observed:
            classification, checks = run_task_checks(task, Path(arm.worktree))
            scoring_diagnostic = None
        else:
            classification = ArmClassification.UNJUDGEABLE
            checks = ()
            scoring_diagnostic = "turn_completion_not_observed"
        result = WrongNudgeMechanicalResult(
            experiment_id=experiment_id,
            condition=arm.arm.condition,
            wrong_nudge_id=arm.arm.wrong_nudge_id,
            wrong_nudge_manifest_sha256=arm.arm.manifest_sha256,
            wrong_nudge_source_task=arm.arm.source_task,
            payload_version=arm.arm.payload_version,
            source_session_id=arm.arm.source_session_id,
            source_step=arm.arm.source_step,
            prefix_id=arm.arm.prefix_id,
            environment_fingerprint=arm.arm.environment_fingerprint,
            fork_session_id=arm.fork_session_id,
            fork_manifest=arm.fork_manifest,
            worktree=arm.worktree,
            turn_id=delivery.turn_id,
            continuation_client_user_message_id=delivery.continuation_client_user_message_id,
            steer_client_user_message_id=delivery.steer_client_user_message_id,
            delivery_outcome=delivery.delivery_outcome,
            completion_observed=delivery.completion_observed,
            turn_status=delivery.turn_status,
            delivery_diagnostic=delivery.diagnostic,
            task_id=task.task_id,
            task_manifest_sha256=task_manifest_sha256,
            fixture_sha256=task.source_sha256,
            classification=classification,
            checks=checks,
            scoring_diagnostic=scoring_diagnostic,
            started_at=started_at,
            ended_at=datetime.now(UTC).isoformat(),
        )
        append_experiment_result(
            result_path,
            {
                "schema": EXPERIMENT_RESULT_SCHEMA,
                "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
                **asdict(result),
            },
        )
        results.append(result)
    append_experiment_result(
        result_path,
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "wrong_nudge_result_schema_version": WRONG_NUDGE_RESULT_SCHEMA_VERSION,
            "complete": True,
            "experiment_id": experiment_id,
            "results": len(results),
            "finished_at": datetime.now(UTC).isoformat(),
        },
    )
    return result_path, tuple(results)


async def _deliver_wrong_nudge_arm(
    prepared: PreparedWrongNudgeArm,
    endpoint: str,
    timeout: float,
    request_timeout: float,
    factory: ClientFactory,
) -> WrongNudgeDeliveryResult:
    client = factory(endpoint, request_timeout)
    continuation_id = f"spt-exp-start-{uuid.uuid4()}"
    steer_id = f"spt-exp-steer-{uuid.uuid4()}" if prepared.arm.payload is not None else None
    turn_id: str | None = None
    delivery = DeliveryOutcome.NOT_ATTEMPTED
    diagnostic: str | None = None
    connected = False
    try:
        await client.connect()
        connected = True
        resumed = await client.resume_thread(prepared.fork_session_id)
        _require_thread_id(resumed, prepared.fork_session_id)
        started = await client.start_turn(
            prepared.fork_session_id,
            CONTROL_PROMPT,
            cwd=prepared.worktree,
            client_user_message_id=continuation_id,
        )
        turn_id = _require_turn_id(started)
        if prepared.arm.payload is None:
            delivery = DeliveryOutcome.CONTROL_NO_STEER
        else:
            assert steer_id is not None
            try:
                await client.steer(
                    prepared.fork_session_id,
                    turn_id,
                    prepared.arm.payload,
                    client_user_message_id=steer_id,
                )
                delivery = DeliveryOutcome.RPC_ACCEPTED
            except AppServerControlError as error:
                delivery = DeliveryOutcome.FAILED_OR_STALE
                diagnostic = error.reason.value
            except AppServerRpcError:
                delivery = DeliveryOutcome.FAILED_OR_STALE
                diagnostic = "rpc_rejected"
            except AppServerError:
                delivery = DeliveryOutcome.ACCEPTANCE_UNKNOWN
                diagnostic = "acceptance_unknown"

        if delivery == DeliveryOutcome.ACCEPTANCE_UNKNOWN:
            return _delivery_result(
                prepared,
                turn_id,
                continuation_id,
                steer_id,
                delivery,
                diagnostic=diagnostic,
            )
        try:
            status = await _wait_for_completion(client, prepared.fork_session_id, turn_id, timeout)
        except TimeoutError:
            return _delivery_result(
                prepared,
                turn_id,
                continuation_id,
                steer_id,
                delivery,
                diagnostic=_join_diagnostic(diagnostic, "completion_timeout"),
            )
        except AppServerError:
            return _delivery_result(
                prepared,
                turn_id,
                continuation_id,
                steer_id,
                delivery,
                diagnostic=_join_diagnostic(diagnostic, "completion_transport_error"),
            )
        except WrongNudgeExperimentError:
            return _delivery_result(
                prepared,
                turn_id,
                continuation_id,
                steer_id,
                delivery,
                diagnostic=_join_diagnostic(diagnostic, "completion_protocol_error"),
            )
        return _delivery_result(
            prepared,
            turn_id,
            continuation_id,
            steer_id,
            delivery,
            completion_observed=True,
            turn_status=status,
            diagnostic=diagnostic,
        )
    except (AppServerError, WrongNudgeExperimentError):
        return _delivery_result(
            prepared,
            turn_id,
            continuation_id,
            steer_id,
            delivery,
            diagnostic="pre_delivery_failed",
        )
    finally:
        if connected:
            with suppress(AppServerError):
                await client.disconnect()


async def _wait_for_completion(
    client: WrongNudgeClient,
    thread_id: str,
    turn_id: str,
    timeout: float,
) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        event = await asyncio.wait_for(client.next_event(), remaining)
        if event.method != "turn/completed":
            continue
        params = event.raw.get("params")
        if not isinstance(params, Mapping) or params.get("threadId") != thread_id:
            continue
        turn = params.get("turn")
        if not isinstance(turn, Mapping) or turn.get("id") != turn_id:
            continue
        status = turn.get("status")
        if not isinstance(status, str) or not status:
            raise WrongNudgeExperimentError("turn/completed omitted status")
        return status


def _delivery_result(
    prepared: PreparedWrongNudgeArm,
    turn_id: str | None,
    continuation_id: str,
    steer_id: str | None,
    delivery: DeliveryOutcome,
    *,
    completion_observed: bool = False,
    turn_status: str | None = None,
    diagnostic: str | None = None,
) -> WrongNudgeDeliveryResult:
    return WrongNudgeDeliveryResult(
        condition=prepared.arm.condition,
        fork_session_id=prepared.fork_session_id,
        turn_id=turn_id,
        continuation_client_user_message_id=continuation_id,
        steer_client_user_message_id=steer_id,
        delivery_outcome=delivery,
        completion_observed=completion_observed,
        turn_status=turn_status,
        diagnostic=diagnostic,
    )


def _require_thread_id(result: Mapping[str, Any], expected: str) -> None:
    thread = result.get("thread")
    if not isinstance(thread, Mapping) or thread.get("id") != expected:
        raise WrongNudgeExperimentError("thread/resume returned a different thread")


def _require_turn_id(result: Mapping[str, Any]) -> str:
    turn = result.get("turn")
    turn_id = turn.get("id") if isinstance(turn, Mapping) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise WrongNudgeExperimentError("turn/start omitted turn id")
    return turn_id


def _join_diagnostic(current: str | None, added: str) -> str:
    return f"{current};{added}" if current else added


def _default_client(endpoint: str, timeout: float) -> WrongNudgeClient:
    return CodexAppServerClient(endpoint, request_timeout=timeout)


def _preflight_forks(plans: tuple[ForkPlan, ...]) -> tuple[str, str]:
    if len(plans) != len(FramingCondition):
        raise WrongNudgeExperimentError(
            f"wrong-nudge experiment requires exactly {len(FramingCondition)} forks"
        )
    if len({plan.session_id for plan in plans}) != len(plans):
        raise WrongNudgeExperimentError("DUPLICATE_FORK_SESSION")
    if len({Path(plan.worktree).resolve() for plan in plans}) != len(plans):
        raise WrongNudgeExperimentError("SHARED_ARM_WORKTREE")
    source_mismatch = next(
        (
            plan.source_environment_preflight
            for plan in plans
            if plan.source_environment_preflight != "MATCHED"
        ),
        None,
    )
    if source_mismatch is not None:
        raise WrongNudgeExperimentError(source_mismatch)
    if any(not plan.manifest for plan in plans):
        raise WrongNudgeExperimentError("FORK_MANIFEST_UNAVAILABLE")
    prefix_ids = {plan.prefix_id for plan in plans}
    if None in prefix_ids or "" in prefix_ids:
        raise WrongNudgeExperimentError("FORK_PROVENANCE_UNAVAILABLE")
    if len(prefix_ids) != 1:
        raise WrongNudgeExperimentError("PREFIX_MISMATCH")
    fingerprints = {plan.environment_fingerprint for plan in plans}
    if None in fingerprints or "" in fingerprints:
        raise WrongNudgeExperimentError("ENVIRONMENT_FINGERPRINT_MISSING")
    if len(fingerprints) != 1:
        raise WrongNudgeExperimentError("ENVIRONMENT_FINGERPRINT_MISMATCH")
    return str(next(iter(prefix_ids))), str(next(iter(fingerprints)))


def _preflight_prepared(prepared: tuple[PreparedWrongNudgeArm, ...]) -> None:
    if {row.arm.condition for row in prepared} != set(FramingCondition):
        raise WrongNudgeExperimentError("prepared arms do not cover every framing condition")
    if len({row.fork_session_id for row in prepared}) != len(prepared):
        raise WrongNudgeExperimentError("DUPLICATE_FORK_SESSION")
    if len({Path(row.worktree).resolve() for row in prepared}) != len(prepared):
        raise WrongNudgeExperimentError("SHARED_ARM_WORKTREE")
    provenance = {
        (
            row.arm.wrong_nudge_id,
            row.arm.manifest_sha256,
            row.arm.source_task,
            row.arm.payload_version,
            row.arm.source_session_id,
            row.arm.source_step,
            row.arm.prefix_id,
            row.arm.environment_fingerprint,
        )
        for row in prepared
    }
    if len(provenance) != 1:
        raise WrongNudgeExperimentError("PREPARED_PROVENANCE_MISMATCH")


def _preflight_deliveries(
    prepared: tuple[PreparedWrongNudgeArm, ...],
    deliveries: tuple[WrongNudgeDeliveryResult, ...],
) -> dict[FramingCondition, WrongNudgeDeliveryResult]:
    by_condition = {result.condition: result for result in deliveries}
    if len(by_condition) != len(deliveries) or set(by_condition) != set(FramingCondition):
        raise WrongNudgeExperimentError("DELIVERY_COVERAGE_MISMATCH")
    for arm in prepared:
        if by_condition[arm.arm.condition].fork_session_id != arm.fork_session_id:
            raise WrongNudgeExperimentError("DELIVERY_PROVENANCE_MISMATCH")
    return by_condition


def _wrong_nudge_results_path(prepared: PreparedWrongNudgeArm, experiment_id: str) -> Path:
    base = spotter_home() / "experiments" / "wrong-nudges"
    source = sanitize_session(prepared.arm.source_session_id)
    nudge = sanitize_session(prepared.arm.wrong_nudge_id)
    return base / f"{source}-step{prepared.arm.source_step}-{nudge}-{experiment_id}.jsonl"
