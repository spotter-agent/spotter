"""Coverage-aware cost and timing projection from durable trajectory records."""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from statistics import mean

from spotter.snapshot import StepRecord

_START_KINDS = {"tool_proposal", "command_started", "tool_started", "file_change_started"}
_OUTCOME_KINDS = {"tool_result", "command_result", "file_edit"}


@dataclass(frozen=True)
class SurfaceCost:
    actions: int = 0
    action_observations: int = 0
    outcomes: int = 0
    outcome_observations: int = 0
    classified_outcomes: int = 0
    failed_outcomes: int = 0


@dataclass(frozen=True)
class RuntimeCostReport:
    sessions: int
    events: int
    surfaces: Mapping[str, SurfaceCost]
    completed_turns: int
    token_turns: int
    token_observations: int
    cumulative_main_tokens: int | None
    reviewer_calls: int
    reviewer_tokens: int | None
    tool_duration_ms: tuple[float, ...]
    gate_calls: int
    hook_ms: tuple[float, ...]
    ipc_ms: tuple[float, ...]
    daemon_evaluation_ms: tuple[float, ...]
    source_timestamps: int
    receipt_timestamps: int
    journal_bytes: int


def measure_runtime_costs(
    journals: Iterable[tuple[Iterable[StepRecord], int]],
) -> RuntimeCostReport:
    surfaces: dict[str, SurfaceCost] = {"hook": SurfaceCost(), "app_server": SurfaceCost()}
    sessions = events = completed_turns = token_turns = token_observations = 0
    cumulative_main_tokens = reviewer_calls = reviewer_tokens = journal_bytes = gate_calls = 0
    main_tokens_known = reviewer_tokens_known = False
    tool_duration_ms: list[float] = []
    hook_ms: list[float] = []
    ipc_ms: list[float] = []
    daemon_evaluation_ms: list[float] = []
    source_timestamps = receipt_timestamps = 0

    for records_iter, size in journals:
        records = tuple(records_iter)
        sessions += 1
        journal_bytes += size
        surface = _surface(records)
        actions: set[str] = set()
        outcomes: set[str] = set()
        action_observations = outcome_observations = 0
        classified: set[str] = set()
        failed: set[str] = set()
        completed: set[str] = set()
        token_covered: set[str] = set()
        latest_main_tokens: int | None = None
        latest_reviewer_tokens: int | None = None
        for record in records:
            event = record.event
            events += 1
            source_timestamps += event.occurred_at is not None
            receipt_timestamps += record.at is not None
            key = _action_key(record)
            if event.kind in _START_KINDS | _OUTCOME_KINDS:
                action_observations += 1
                if key is not None:
                    actions.add(key)
            if event.kind in _OUTCOME_KINDS:
                outcome_observations += 1
                if key is not None:
                    outcomes.add(key)
                    failure = _outcome_failure(event.payload)
                    if failure is not None:
                        classified.add(key)
                    if failure is True:
                        failed.add(key)
            duration = _number(event.payload.get("durationMs"))
            if duration is not None and event.kind in _OUTCOME_KINDS:
                tool_duration_ms.append(duration)
            turn_id = _turn_id(record)
            if event.kind == "turn_completed" and turn_id is not None:
                completed.add(turn_id)
            if event.kind == "token_usage":
                token_observations += 1
                if turn_id is not None:
                    token_covered.add(turn_id)
                observed_tokens = _total_tokens(event.payload)
                if observed_tokens is not None:
                    latest_main_tokens = observed_tokens
            if event.kind == "reviewer_decision":
                reviewer_calls += 1
                spend = event.payload.get("spend")
                if isinstance(spend, Mapping):
                    value = spend.get("session_tokens")
                    if isinstance(value, int) and not isinstance(value, bool):
                        latest_reviewer_tokens = value
            if event.kind == "gate_ipc":
                gate_calls += 1
                _append_number(hook_ms, event.payload.get("hook_ms"))
                _append_number(ipc_ms, event.payload.get("ipc_ms"))
                _append_number(daemon_evaluation_ms, event.payload.get("daemon_evaluation_ms"))
        current = surfaces[surface]
        surfaces[surface] = SurfaceCost(
            actions=current.actions + len(actions),
            action_observations=current.action_observations + action_observations,
            outcomes=current.outcomes + len(outcomes),
            outcome_observations=current.outcome_observations + outcome_observations,
            classified_outcomes=current.classified_outcomes + len(classified),
            failed_outcomes=current.failed_outcomes + len(failed),
        )
        completed_turns += len(completed)
        token_turns += len(completed & token_covered)
        if latest_main_tokens is not None:
            main_tokens_known = True
            cumulative_main_tokens += latest_main_tokens
        if latest_reviewer_tokens is not None:
            reviewer_tokens_known = True
            reviewer_tokens += latest_reviewer_tokens

    return RuntimeCostReport(
        sessions,
        events,
        surfaces,
        completed_turns,
        token_turns,
        token_observations,
        cumulative_main_tokens if main_tokens_known else None,
        reviewer_calls,
        reviewer_tokens if reviewer_tokens_known else None,
        tuple(tool_duration_ms),
        gate_calls,
        tuple(hook_ms),
        tuple(ipc_ms),
        tuple(daemon_evaluation_ms),
        source_timestamps,
        receipt_timestamps,
        journal_bytes,
    )


def render_runtime_costs(report: RuntimeCostReport) -> str:
    lines = ["Runtime cost / efficiency (coverage-aware):", "  Main semantic actions:"]
    for surface, cost in report.surfaces.items():
        lines.append(
            f"    {surface}: actions={cost.actions}/{cost.action_observations} correlated, "
            f"outcomes={cost.outcomes}/{cost.outcome_observations} correlated, "
            f"failed={cost.failed_outcomes}/{cost.classified_outcomes} classified"
        )
    tokens = (
        f"{report.cumulative_main_tokens} cumulative/unknown-scope tokens"
        if report.cumulative_main_tokens is not None
        else "unknown"
    )
    lines.append(
        f"  Main tokens: {tokens}; turn coverage "
        f"{report.token_turns}/{report.completed_turns}; observations={report.token_observations}"
    )
    reviewer_tokens = (
        str(report.reviewer_tokens) if report.reviewer_tokens is not None else "unknown"
    )
    lines.append(
        f"  Spotter semantic: reviewer_calls={report.reviewer_calls}, "
        f"recorded_session_tokens={reviewer_tokens}"
    )
    lines.append(
        f"  Spotter deterministic: gate_calls={report.gate_calls}; "
        f"hook={_sample(report.hook_ms, report.gate_calls)}, "
        f"ipc={_sample(report.ipc_ms, report.gate_calls)}, "
        f"daemon_eval={_sample(report.daemon_evaluation_ms, report.gate_calls)}"
    )
    tool_outcomes = sum(cost.outcome_observations for cost in report.surfaces.values())
    lines.append(
        f"  Timing: receipt_wall={report.receipt_timestamps}/{report.events}, "
        f"source={report.source_timestamps}/{report.events}, "
        f"tool_duration={_sample(report.tool_duration_ms, tool_outcomes)}"
    )
    lines.append(
        f"  Storage: journals={report.journal_bytes} bytes across {report.sessions} sessions"
    )
    return "\n".join(lines)


def _surface(records: tuple[StepRecord, ...]) -> str:
    return (
        "app_server"
        if any(
            record.event.connection_epoch is not None
            or (
                record.event.provenance is not None
                and record.event.provenance.source == "codex_app_server"
            )
            for record in records
        )
        else "hook"
    )


def _action_key(record: StepRecord) -> str | None:
    event = record.event
    value = event.operation_id or event.payload.get("tool_use_id") or event.event_id
    turn = _turn_id(record) or "unknown-turn"
    return f"{turn}:{value}" if value is not None else None


def _turn_id(record: StepRecord) -> str | None:
    identity = record.event.identity
    if identity is not None and identity.turn_id is not None:
        return identity.turn_id.value
    value = record.event.payload.get("turn_id")
    return value if isinstance(value, str) and value else None


def _total_tokens(payload: Mapping[str, object]) -> int | None:
    total = payload.get("total")
    value = total.get("totalTokens") if isinstance(total, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _outcome_failure(payload: Mapping[str, object]) -> bool | None:
    response = payload.get("tool_response")
    exit_code = payload.get("exitCode")
    if isinstance(response, Mapping):
        exit_code = response.get("exit_code", exit_code)
        ok = response.get("ok")
        if isinstance(ok, bool):
            return not ok
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return exit_code != 0
    status = payload.get("status")
    if status in {"failed", "error", "interrupted", "cancelled"}:
        return True
    if status in {"completed", "succeeded", "success", "passed"}:
        return False
    return None


def _number(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _append_number(target: list[float], value: object) -> None:
    parsed = _number(value)
    if parsed is not None:
        target.append(parsed)


def _sample(values: tuple[float, ...], eligible: int) -> str:
    if not values:
        return f"unknown (0/{eligible})"
    return f"avg={mean(values):.2f}ms max={max(values):.2f}ms ({len(values)}/{eligible})"
