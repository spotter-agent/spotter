"""Durable follow-up turns for wrong-nudge persistence cohorts."""

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from spotter.app_server import (
    AppServerError,
    AppServerEvent,
    AppServerRpcError,
    CodexAppServerClient,
)
from spotter.experiment import (
    EXPERIMENT_RESULT_SCHEMA,
    EXPERIMENT_RESULT_SCHEMA_VERSION,
    append_experiment_result,
)
from spotter.paths import sanitize_session, spotter_home
from spotter.wrong_nudge_annotations import wrong_nudge_result_fingerprint
from spotter.wrong_nudge_corpus import FramingCondition
from spotter.wrong_nudge_experiment import (
    WRONG_NUDGE_RESULT_SCHEMA_VERSION,
    DeliveryOutcome,
    WrongNudgeMechanicalResult,
)

WRONG_NUDGE_PERSISTENCE_SCHEMA_VERSION = 1
PERSISTENCE_FOLLOW_UP_PROMPT_VERSION = 1
PERSISTENCE_FOLLOW_UP_PROMPT = "Continue the user's original task and constraints."


class WrongNudgePersistenceError(ValueError):
    """A persistence cohort is unsafe to run or cannot be read."""


class PersistenceDeliveryOutcome(StrEnum):
    PRE_DELIVERY_FAILED = "PRE_DELIVERY_FAILED"
    START_REJECTED = "START_REJECTED"
    START_ACCEPTED = "START_ACCEPTED"
    ACCEPTANCE_UNKNOWN = "ACCEPTANCE_UNKNOWN"


@dataclass(frozen=True)
class WrongNudgePersistenceResult:
    experiment_id: str
    condition: FramingCondition
    fork_session_id: str
    worktree: str
    source_result_fingerprint: str
    follow_up_prompt_version: int
    follow_up_client_user_message_id: str
    turn_id: str | None
    delivery_outcome: PersistenceDeliveryOutcome
    completion_observed: bool
    turn_status: str | None
    diagnostic: str | None
    started_at: str
    ended_at: str
    result_schema_version: int = EXPERIMENT_RESULT_SCHEMA_VERSION
    wrong_nudge_persistence_schema_version: int = WRONG_NUDGE_PERSISTENCE_SCHEMA_VERSION


class PersistenceClient(Protocol):
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

    async def next_event(self) -> AppServerEvent: ...


ClientFactory = Callable[[str, float], PersistenceClient]


def run_wrong_nudge_persistence_cohort(
    results: tuple[WrongNudgeMechanicalResult, ...],
    endpoint: str,
    *,
    timeout: float = 1800,
    request_timeout: float = 10,
    output: Path | None = None,
    client_factory: ClientFactory | None = None,
) -> tuple[Path, tuple[WrongNudgePersistenceResult, ...]]:
    """Run one frozen follow-up turn for every complete equivalent-prefix arm."""

    if not endpoint.strip():
        raise WrongNudgePersistenceError("App Server endpoint must be non-empty")
    if timeout <= 0 or request_timeout <= 0:
        raise WrongNudgePersistenceError("timeouts must be positive")
    experiment_id = _preflight_results(results)
    path = output or wrong_nudge_persistence_path(experiment_id)
    if path.exists():
        raise WrongNudgePersistenceError(f"persistence result path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    append_experiment_result(
        path,
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "wrong_nudge_persistence_schema_version": WRONG_NUDGE_PERSISTENCE_SCHEMA_VERSION,
            "meta": True,
            "experiment_id": experiment_id,
            "experiment_mode": "wrong-nudge-persistence",
            "follow_up_prompt_version": PERSISTENCE_FOLLOW_UP_PROMPT_VERSION,
            "started_at": datetime.now(UTC).isoformat(),
        },
    )

    factory = client_factory or _default_client
    follow_ups: list[WrongNudgePersistenceResult] = []
    for source in sorted(results, key=lambda row: row.condition.value):
        result = asyncio.run(
            _deliver_follow_up(source, endpoint, timeout, request_timeout, factory)
        )
        append_experiment_result(
            path,
            {
                "schema": EXPERIMENT_RESULT_SCHEMA,
                "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
                **asdict(result),
            },
        )
        follow_ups.append(result)
    append_experiment_result(
        path,
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "wrong_nudge_persistence_schema_version": WRONG_NUDGE_PERSISTENCE_SCHEMA_VERSION,
            "complete": True,
            "experiment_id": experiment_id,
            "follow_up_prompt_version": PERSISTENCE_FOLLOW_UP_PROMPT_VERSION,
            "results": len(follow_ups),
            "finished_at": datetime.now(UTC).isoformat(),
        },
    )
    return path, tuple(follow_ups)


def load_wrong_nudge_persistence_results(
    path: Path,
) -> tuple[WrongNudgePersistenceResult, ...]:
    """Reload durable follow-up rows without treating missing arms as observations."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise WrongNudgePersistenceError(f"cannot read {path}: {error}") from error
    results: list[WrongNudgePersistenceResult] = []
    seen: set[tuple[str, FramingCondition]] = set()
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise TypeError("record is not an object")
            _validate_row_schema(row, path, number)
            if row.get("meta") is True or row.get("complete") is True:
                continue
            result = WrongNudgePersistenceResult(
                experiment_id=_text(row["experiment_id"]),
                condition=FramingCondition(row["condition"]),
                fork_session_id=_text(row["fork_session_id"]),
                worktree=_text(row["worktree"]),
                source_result_fingerprint=_text(row["source_result_fingerprint"]),
                follow_up_prompt_version=_integer(row["follow_up_prompt_version"]),
                follow_up_client_user_message_id=_text(row["follow_up_client_user_message_id"]),
                turn_id=_optional_text(row["turn_id"]),
                delivery_outcome=PersistenceDeliveryOutcome(row["delivery_outcome"]),
                completion_observed=_boolean(row["completion_observed"]),
                turn_status=_optional_text(row["turn_status"]),
                diagnostic=_optional_text(row["diagnostic"]),
                started_at=_text(row["started_at"]),
                ended_at=_text(row["ended_at"]),
                result_schema_version=_integer(row["result_schema_version"]),
                wrong_nudge_persistence_schema_version=_integer(
                    row["wrong_nudge_persistence_schema_version"]
                ),
            )
            key = (result.experiment_id, result.condition)
            if key in seen:
                raise WrongNudgePersistenceError(f"{path} contains duplicate result {key}")
            seen.add(key)
            results.append(result)
        except WrongNudgePersistenceError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise WrongNudgePersistenceError(
                f"{path} line {number} is unreadable ({error})"
            ) from error
    return tuple(results)


def wrong_nudge_persistence_path(experiment_id: str) -> Path:
    return (
        spotter_home()
        / "experiments"
        / "wrong-nudges"
        / f"{sanitize_session(experiment_id)}-persistence.jsonl"
    )


async def _deliver_follow_up(
    source: WrongNudgeMechanicalResult,
    endpoint: str,
    timeout: float,
    request_timeout: float,
    factory: ClientFactory,
) -> WrongNudgePersistenceResult:
    client = factory(endpoint, request_timeout)
    message_id = f"spt-exp-persistence-{uuid.uuid4()}"
    started_at = datetime.now(UTC).isoformat()
    turn_id: str | None = None
    delivery = PersistenceDeliveryOutcome.PRE_DELIVERY_FAILED
    diagnostic: str | None = None
    connected = False
    completion_observed = False
    turn_status: str | None = None
    try:
        await client.connect()
        connected = True
        resumed = await client.resume_thread(source.fork_session_id)
        _require_thread_id(resumed, source.fork_session_id)
        try:
            started = await client.start_turn(
                source.fork_session_id,
                PERSISTENCE_FOLLOW_UP_PROMPT,
                cwd=source.worktree,
                client_user_message_id=message_id,
            )
            turn_id = _require_turn_id(started)
            delivery = PersistenceDeliveryOutcome.START_ACCEPTED
        except AppServerRpcError:
            delivery = PersistenceDeliveryOutcome.START_REJECTED
            diagnostic = "rpc_rejected"
        except AppServerError:
            delivery = PersistenceDeliveryOutcome.ACCEPTANCE_UNKNOWN
            diagnostic = "acceptance_unknown"
        except WrongNudgePersistenceError:
            delivery = PersistenceDeliveryOutcome.ACCEPTANCE_UNKNOWN
            diagnostic = "start_protocol_error"

        if delivery != PersistenceDeliveryOutcome.START_ACCEPTED:
            return _persistence_result(
                source,
                message_id,
                turn_id,
                delivery,
                started_at,
                diagnostic=diagnostic,
            )
        assert turn_id is not None
        try:
            turn_status = await _wait_for_completion(
                client, source.fork_session_id, turn_id, timeout
            )
            completion_observed = True
        except TimeoutError:
            diagnostic = "completion_timeout"
        except AppServerError:
            diagnostic = "completion_transport_error"
        except WrongNudgePersistenceError:
            diagnostic = "completion_protocol_error"
    except (AppServerError, WrongNudgePersistenceError):
        diagnostic = "pre_delivery_failed"
    finally:
        if connected:
            with suppress(AppServerError):
                await client.disconnect()
    return _persistence_result(
        source,
        message_id,
        turn_id,
        delivery,
        started_at,
        completion_observed=completion_observed,
        turn_status=turn_status,
        diagnostic=diagnostic,
    )


async def _wait_for_completion(
    client: PersistenceClient,
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
            raise WrongNudgePersistenceError("turn/completed omitted status")
        return status


def _persistence_result(
    source: WrongNudgeMechanicalResult,
    message_id: str,
    turn_id: str | None,
    delivery: PersistenceDeliveryOutcome,
    started_at: str,
    *,
    completion_observed: bool = False,
    turn_status: str | None = None,
    diagnostic: str | None = None,
) -> WrongNudgePersistenceResult:
    return WrongNudgePersistenceResult(
        experiment_id=source.experiment_id,
        condition=source.condition,
        fork_session_id=source.fork_session_id,
        worktree=source.worktree,
        source_result_fingerprint=wrong_nudge_result_fingerprint(source),
        follow_up_prompt_version=PERSISTENCE_FOLLOW_UP_PROMPT_VERSION,
        follow_up_client_user_message_id=message_id,
        turn_id=turn_id,
        delivery_outcome=delivery,
        completion_observed=completion_observed,
        turn_status=turn_status,
        diagnostic=diagnostic,
        started_at=started_at,
        ended_at=datetime.now(UTC).isoformat(),
    )


def _preflight_results(results: tuple[WrongNudgeMechanicalResult, ...]) -> str:
    if {result.condition for result in results} != set(FramingCondition) or len(results) != len(
        FramingCondition
    ):
        raise WrongNudgePersistenceError("persistence cohort requires exactly one complete arm set")
    experiment_ids = {result.experiment_id for result in results}
    if len(experiment_ids) != 1:
        raise WrongNudgePersistenceError("persistence cohort spans multiple experiments")
    provenance = {
        (
            result.wrong_nudge_id,
            result.wrong_nudge_manifest_sha256,
            result.wrong_nudge_source_task,
            result.payload_version,
            result.source_session_id,
            result.source_step,
            result.prefix_id,
            result.environment_fingerprint,
            result.task_id,
            result.task_manifest_sha256,
            result.fixture_sha256,
        )
        for result in results
    }
    if len(provenance) != 1:
        raise WrongNudgePersistenceError("persistence cohort provenance mismatch")
    for result in results:
        if (
            result.result_schema_version != EXPERIMENT_RESULT_SCHEMA_VERSION
            or result.wrong_nudge_result_schema_version != WRONG_NUDGE_RESULT_SCHEMA_VERSION
        ):
            raise WrongNudgePersistenceError("unsupported source result schema")
        expected = (
            DeliveryOutcome.CONTROL_NO_STEER
            if result.condition == FramingCondition.NEUTRAL_CONTROL
            else DeliveryOutcome.RPC_ACCEPTED
        )
        if result.delivery_outcome != expected or not result.completion_observed:
            raise WrongNudgePersistenceError(
                f"{result.condition} lacks accepted, completed source delivery"
            )
        if not Path(result.worktree).is_dir():
            raise WrongNudgePersistenceError(f"source worktree is unavailable: {result.worktree}")
    return next(iter(experiment_ids))


def _require_thread_id(result: Mapping[str, Any], expected: str) -> None:
    thread = result.get("thread")
    if not isinstance(thread, Mapping) or thread.get("id") != expected:
        raise WrongNudgePersistenceError("thread/resume returned a different thread")


def _require_turn_id(result: Mapping[str, Any]) -> str:
    turn = result.get("turn")
    turn_id = turn.get("id") if isinstance(turn, Mapping) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise WrongNudgePersistenceError("turn/start omitted turn id")
    return turn_id


def _validate_row_schema(row: dict[str, Any], path: Path, number: int) -> None:
    if (
        row.get("schema") != EXPERIMENT_RESULT_SCHEMA
        or row.get("schema_version") != EXPERIMENT_RESULT_SCHEMA_VERSION
        or row.get("result_schema_version") != EXPERIMENT_RESULT_SCHEMA_VERSION
        or row.get("wrong_nudge_persistence_schema_version")
        != WRONG_NUDGE_PERSISTENCE_SCHEMA_VERSION
        or row.get("follow_up_prompt_version") != PERSISTENCE_FOLLOW_UP_PROMPT_VERSION
    ):
        raise WrongNudgePersistenceError(f"{path} line {number} has unsupported schema")


def _default_client(endpoint: str, timeout: float) -> PersistenceClient:
    return CodexAppServerClient(endpoint, request_timeout=timeout)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected non-empty text")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expected integer")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected boolean")
    return value
