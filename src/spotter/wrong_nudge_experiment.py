"""Prepare and deliver equivalent-prefix wrong-nudge susceptibility experiments."""

import asyncio
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
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
from spotter.experiment import CONTROL_PROMPT
from spotter.replay import ForkPlan, fork
from spotter.wrong_nudge_corpus import (
    FramingCondition,
    WrongNudge,
    WrongNudgeArm,
    build_wrong_nudge_arms,
)


class WrongNudgeExperimentError(ValueError):
    """Wrong-nudge arms cannot be compared or safely delivered."""


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
