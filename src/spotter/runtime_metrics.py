"""Coverage-aware cost and timing projection from durable trajectory records."""

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from statistics import mean

from spotter.outcomes import outcome_failure
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
    actions_by_family: Mapping[str, int] = field(default_factory=dict)
    classified_outcomes_by_family: Mapping[str, int] = field(default_factory=dict)
    failed_outcomes_by_family: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CoveredTokens:
    value: int | None
    covered_sessions: int


@dataclass(frozen=True)
class TokenBreakdown:
    total: CoveredTokens
    input: CoveredTokens
    cached_input: CoveredTokens
    cache_write_input: CoveredTokens
    output: CoveredTokens
    reasoning_output: CoveredTokens


@dataclass(frozen=True)
class RuntimeCostReport:
    sessions: int
    events: int
    surfaces: Mapping[str, SurfaceCost]
    completed_turns: int
    token_turns: int
    token_observations: int
    cumulative_main_tokens: int | None
    main_token_breakdown: TokenBreakdown
    reviewer_calls: int
    reviewer_tokens: int | None
    reviewer_jobs_queued: int
    reviewer_jobs_started: int
    reviewer_queue_ms: tuple[float, ...]
    reviewer_inference_ms: tuple[float, ...]
    turn_wall_ms: tuple[float, ...]
    tool_duration_ms: tuple[float, ...]
    gate_calls: int
    hook_ms: tuple[float, ...]
    ipc_ms: tuple[float, ...]
    daemon_evaluation_ms: tuple[float, ...]
    daemon_resource_samples: int
    daemon_cpu_seconds: float | None
    daemon_peak_rss_bytes: int | None
    source_timestamps: int
    receipt_timestamps: int
    arrival_ordered_events: int
    arrival_order_eligible_events: int
    journal_bytes: int


def measure_runtime_costs(
    journals: Iterable[tuple[Iterable[StepRecord], int]],
) -> RuntimeCostReport:
    surfaces: dict[str, SurfaceCost] = {"hook": SurfaceCost(), "app_server": SurfaceCost()}
    sessions = events = completed_turns = token_turns = token_observations = 0
    reviewer_calls = reviewer_tokens = journal_bytes = gate_calls = 0
    reviewer_jobs_queued = reviewer_jobs_started = 0
    reviewer_tokens_known = False
    token_sums = {field: 0 for field in _TOKEN_FIELDS}
    token_sessions = {field: 0 for field in _TOKEN_FIELDS}
    tool_duration_ms: list[float] = []
    reviewer_queue_ms: list[float] = []
    reviewer_inference_ms: list[float] = []
    turn_wall_ms: list[float] = []
    hook_ms: list[float] = []
    ipc_ms: list[float] = []
    daemon_evaluation_ms: list[float] = []
    daemon_samples: dict[tuple[str, int], tuple[float, int]] = {}
    source_timestamps = receipt_timestamps = 0
    arrival_ordered_events = arrival_order_eligible_events = 0

    for records_iter, size in journals:
        records = tuple(records_iter)
        sessions += 1
        journal_bytes += size
        surface = _surface(records)
        actions: set[str] = set()
        outcomes: set[str] = set()
        action_families: dict[str, str] = {}
        outcome_families: dict[str, str] = {}
        action_observations = outcome_observations = 0
        classified: set[str] = set()
        failed: set[str] = set()
        completed: set[str] = set()
        token_covered: set[str] = set()
        turn_starts: dict[tuple[str, int], float] = {}
        latest_main_tokens: Mapping[str, object] | None = None
        latest_reviewer_tokens: int | None = None
        for record in records:
            event = record.event
            events += 1
            source_timestamps += event.occurred_at is not None
            receipt_timestamps += record.at is not None
            arrival_order_eligible_events += event.connection_epoch is not None
            arrival_ordered_events += (
                event.connection_epoch is not None and event.arrival_seq is not None
            )
            key = _action_key(record)
            if event.kind in _START_KINDS | _OUTCOME_KINDS:
                action_observations += 1
                if key is not None:
                    actions.add(key)
                    action_families.setdefault(key, _action_family(event.kind))
            if event.kind in _OUTCOME_KINDS:
                outcome_observations += 1
                if key is not None:
                    outcomes.add(key)
                    outcome_families.setdefault(key, _action_family(event.kind))
                    failure = outcome_failure(event.payload)
                    if failure is not None:
                        classified.add(key)
                    if failure is True:
                        failed.add(key)
            duration = _number(event.payload.get("durationMs"))
            if duration is not None and event.kind in _OUTCOME_KINDS:
                tool_duration_ms.append(duration)
            turn_id = _turn_id(record)
            clock = _turn_clock(record)
            if event.kind == "turn_started" and clock is not None:
                turn_starts.setdefault(clock[:2], clock[2])
            if event.kind == "turn_completed" and turn_id is not None:
                completed.add(turn_id)
                if clock is not None and (started_at := turn_starts.get(clock[:2])) is not None:
                    finished_at = clock[2]
                    if finished_at >= started_at:
                        turn_wall_ms.append((finished_at - started_at) * 1000)
            if event.kind == "token_usage":
                token_observations += 1
                if turn_id is not None:
                    token_covered.add(turn_id)
                total = event.payload.get("total")
                if isinstance(total, Mapping):
                    latest_main_tokens = total
            if event.kind == "reviewer_decision":
                reviewer_calls += 1
                spend = event.payload.get("spend")
                if isinstance(spend, Mapping):
                    value = spend.get("session_tokens")
                    if isinstance(value, int) and not isinstance(value, bool):
                        latest_reviewer_tokens = value
                timing = event.payload.get("timing")
                if isinstance(timing, Mapping):
                    _append_number(reviewer_inference_ms, timing.get("inference_ms"))
            if event.kind == "review_job_queued":
                reviewer_jobs_queued += 1
            if event.kind == "review_inference_started":
                reviewer_jobs_started += 1
                _append_number(reviewer_queue_ms, event.payload.get("queue_ms"))
            if event.kind == "gate_ipc":
                gate_calls += 1
                _append_number(hook_ms, event.payload.get("hook_ms"))
                _append_number(ipc_ms, event.payload.get("ipc_ms"))
                _append_number(daemon_evaluation_ms, event.payload.get("daemon_evaluation_ms"))
                _append_daemon_sample(daemon_samples, event.payload.get("runtime_sample"))
        current = surfaces[surface]
        surfaces[surface] = SurfaceCost(
            actions=current.actions + len(actions),
            action_observations=current.action_observations + action_observations,
            outcomes=current.outcomes + len(outcomes),
            outcome_observations=current.outcome_observations + outcome_observations,
            classified_outcomes=current.classified_outcomes + len(classified),
            failed_outcomes=current.failed_outcomes + len(failed),
            actions_by_family=_merge_counts(
                current.actions_by_family, Counter(action_families.values())
            ),
            classified_outcomes_by_family=_merge_counts(
                current.classified_outcomes_by_family,
                Counter(outcome_families[key] for key in classified),
            ),
            failed_outcomes_by_family=_merge_counts(
                current.failed_outcomes_by_family,
                Counter(outcome_families[key] for key in failed),
            ),
        )
        completed_turns += len(completed)
        token_turns += len(completed & token_covered)
        if latest_main_tokens is not None:
            # Each journal represents one session; use only its latest cumulative
            # observation so repeated token updates are not double-counted.
            for field in _TOKEN_FIELDS:
                if (value := _token_count(latest_main_tokens.get(field))) is not None:
                    token_sums[field] += value
                    token_sessions[field] += 1
        if latest_reviewer_tokens is not None:
            reviewer_tokens_known = True
            reviewer_tokens += latest_reviewer_tokens

    latest_samples: dict[str, tuple[int, float, int]] = {}
    for (runtime_id, sample_seq), (cpu_seconds, peak_rss_bytes) in daemon_samples.items():
        previous = latest_samples.get(runtime_id)
        if previous is None or sample_seq > previous[0]:
            latest_samples[runtime_id] = (sample_seq, cpu_seconds, peak_rss_bytes)

    token_breakdown = TokenBreakdown(
        _covered_field("totalTokens", token_sums, token_sessions),
        _covered_field("inputTokens", token_sums, token_sessions),
        _covered_field("cachedInputTokens", token_sums, token_sessions),
        _covered_field("cacheWriteInputTokens", token_sums, token_sessions),
        _covered_field("outputTokens", token_sums, token_sessions),
        _covered_field("reasoningOutputTokens", token_sums, token_sessions),
    )
    return RuntimeCostReport(
        sessions,
        events,
        surfaces,
        completed_turns,
        token_turns,
        token_observations,
        token_breakdown.total.value,
        token_breakdown,
        reviewer_calls,
        reviewer_tokens if reviewer_tokens_known else None,
        reviewer_jobs_queued,
        reviewer_jobs_started,
        tuple(reviewer_queue_ms),
        tuple(reviewer_inference_ms),
        tuple(turn_wall_ms),
        tuple(tool_duration_ms),
        gate_calls,
        tuple(hook_ms),
        tuple(ipc_ms),
        tuple(daemon_evaluation_ms),
        len(daemon_samples),
        sum(sample[1] for sample in latest_samples.values()) if latest_samples else None,
        max((sample[2] for sample in latest_samples.values()), default=None),
        source_timestamps,
        receipt_timestamps,
        arrival_ordered_events,
        arrival_order_eligible_events,
        journal_bytes,
    )


def render_runtime_costs(report: RuntimeCostReport) -> str:
    lines = ["Runtime cost / efficiency (coverage-aware):", "  Main semantic actions:"]
    for surface, cost in report.surfaces.items():
        lines.append(
            f"    {surface}: actions={cost.actions} "
            f"(from {_observations(cost.action_observations)}), outcomes={cost.outcomes} "
            f"(from {_observations(cost.outcome_observations)}), "
            f"failed={cost.failed_outcomes}/{cost.classified_outcomes} classified"
        )
        if cost.actions_by_family:
            lines.append(f"      families: {_render_families(cost)}")
    tokens = report.main_token_breakdown
    lines.append(
        f"  Main tokens: {_covered_tokens(tokens.total, report.sessions)} "
        "cumulative/unknown-scope; breakdown "
        f"input={_covered_tokens(tokens.input, report.sessions)}, "
        f"cached_input={_covered_tokens(tokens.cached_input, report.sessions)}, "
        f"cache_write_input={_covered_tokens(tokens.cache_write_input, report.sessions)}, "
        f"output={_covered_tokens(tokens.output, report.sessions)}, "
        f"reasoning_output={_covered_tokens(tokens.reasoning_output, report.sessions)}; "
        "source=durable token_usage provenance; turn coverage "
        f"{report.token_turns}/{report.completed_turns}; observations={report.token_observations}"
    )
    reviewer_tokens = (
        str(report.reviewer_tokens) if report.reviewer_tokens is not None else "unknown"
    )
    queue = _sample(report.reviewer_queue_ms, report.reviewer_jobs_started)
    inference = _sample(report.reviewer_inference_ms, report.reviewer_calls)
    jobs = f"{report.reviewer_calls}/{report.reviewer_jobs_started}/{report.reviewer_jobs_queued}"
    lines.append(
        f"  Spotter semantic: reviewer_calls={report.reviewer_calls}, "
        f"recorded_session_tokens={reviewer_tokens}; queue={queue}, "
        f"inference={inference}; jobs={jobs} decided/started/queued"
    )
    lines.append(
        f"  Spotter deterministic: gate_calls={report.gate_calls}; "
        f"hook={_sample(report.hook_ms, report.gate_calls)}, "
        f"ipc={_sample(report.ipc_ms, report.gate_calls)}, "
        f"daemon_eval={_sample(report.daemon_evaluation_ms, report.gate_calls)}"
    )
    cpu = (
        f"{report.daemon_cpu_seconds:.3f}s" if report.daemon_cpu_seconds is not None else "unknown"
    )
    rss = (
        f"{report.daemon_peak_rss_bytes} bytes"
        if report.daemon_peak_rss_bytes is not None
        else "unknown"
    )
    lines.append(
        f"  Spotter resources: cpu={cpu}, peak_rss={rss}; "
        f"samples={report.daemon_resource_samples}/{report.gate_calls} gate calls"
    )
    tool_outcomes = sum(cost.outcome_observations for cost in report.surfaces.values())
    lines.append(
        f"  Timing: receipt_wall={report.receipt_timestamps}/{report.events}, "
        f"source={report.source_timestamps}/{report.events}, "
        f"arrival_order={report.arrival_ordered_events}/{report.arrival_order_eligible_events}, "
        f"turn_wall(source)={_sample(report.turn_wall_ms, report.completed_turns)}, "
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


def _action_family(kind: str) -> str:
    if kind in {"command_started", "command_result"}:
        return "command"
    if kind in {"file_change_started", "file_edit"}:
        return "file_change"
    return "tool"


def _merge_counts(left: Mapping[str, int], right: Mapping[str, int]) -> dict[str, int]:
    return dict(Counter(left) + Counter(right))


def _render_families(cost: SurfaceCost) -> str:
    return ", ".join(
        f"{family} actions={actions} "
        f"failed={cost.failed_outcomes_by_family.get(family, 0)}/"
        f"{cost.classified_outcomes_by_family.get(family, 0)} classified"
        for family, actions in sorted(cost.actions_by_family.items())
    )


def _turn_id(record: StepRecord) -> str | None:
    identity = record.event.identity
    if identity is not None and identity.turn_id is not None:
        return identity.turn_id.value
    value = record.event.payload.get("turn_id")
    return value if isinstance(value, str) and value else None


def _turn_clock(record: StepRecord) -> tuple[str, int, float] | None:
    """Identity for source-clock durations that must not cross reconnects."""

    turn_id = _turn_id(record)
    epoch = record.event.connection_epoch
    occurred_at = record.event.occurred_at
    if turn_id is None or epoch is None or occurred_at is None or not math.isfinite(occurred_at):
        return None
    return turn_id, epoch, occurred_at


_TOKEN_FIELDS = (
    "totalTokens",
    "inputTokens",
    "cachedInputTokens",
    "cacheWriteInputTokens",
    "outputTokens",
    "reasoningOutputTokens",
)


def _token_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _covered_field(
    field: str,
    sums: Mapping[str, int],
    sessions: Mapping[str, int],
) -> CoveredTokens:
    coverage = sessions[field]
    return CoveredTokens(sums[field] if coverage else None, coverage)


def _number(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _append_number(target: list[float], value: object) -> None:
    parsed = _number(value)
    if parsed is not None:
        target.append(parsed)


def _append_daemon_sample(target: dict[tuple[str, int], tuple[float, int]], value: object) -> None:
    if not isinstance(value, Mapping):
        return
    runtime_id = value.get("runtime_id")
    sample_seq = value.get("sample_seq")
    cpu_seconds = _number(value.get("cpu_seconds"))
    peak_rss_bytes = value.get("peak_rss_bytes")
    if (
        isinstance(runtime_id, str)
        and runtime_id
        and isinstance(sample_seq, int)
        and not isinstance(sample_seq, bool)
        and sample_seq > 0
        and cpu_seconds is not None
        and isinstance(peak_rss_bytes, int)
        and not isinstance(peak_rss_bytes, bool)
        and peak_rss_bytes >= 0
    ):
        target[(runtime_id, sample_seq)] = (cpu_seconds, peak_rss_bytes)


def _sample(values: tuple[float, ...], eligible: int) -> str:
    if not values:
        return f"unknown (0/{eligible})"
    return f"avg={mean(values):.2f}ms max={max(values):.2f}ms ({len(values)}/{eligible})"


def _observations(count: int) -> str:
    return f"{count} observation{'s' if count != 1 else ''}"


def _covered_tokens(metric: CoveredTokens, eligible: int) -> str:
    value = str(metric.value) if metric.value is not None else "unknown"
    return f"{value} ({metric.covered_sessions}/{eligible} sessions)"
