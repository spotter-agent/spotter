"""Coverage-aware cost, timing, and objective-outcome projections."""

import json
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from statistics import mean

from spotter.outcomes import outcome_failure
from spotter.snapshot import StepRecord

_START_KINDS = {"tool_proposal", "command_started", "tool_started", "file_change_started"}
_OUTCOME_KINDS = {"tool_result", "command_result", "file_edit"}
_ARM_CLASSIFICATIONS = {
    "PASS",
    "TASK_FAIL",
    "SETUP_FAIL",
    "INFRA_FAIL",
    "TIMEOUT_AGENT",
    "TIMEOUT_CHECK",
    "CHECK_ERROR",
    "UNJUDGEABLE",
}
_TOKENS_USED_RE = re.compile(r"tokens used\s*\n\s*([0-9][0-9,]*)", re.IGNORECASE)
_REPETITION_FEATURES = {
    "repeated_equivalent_tool_call": "consecutive_equivalent_calls",
    "repeated_read_no_frontier": "reads_without_frontier_expansion",
}


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
    unique_resources: int | None = None
    resource_actions: int = 0


@dataclass(frozen=True)
class CoveredTokens:
    value: int | None
    covered_sessions: int


@dataclass(frozen=True)
class CoveredCount:
    value: int | None
    covered: int
    eligible: int


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
    signal_candidates_active: int
    repeated_equivalent_actions: CoveredCount
    reads_without_frontier_expansion: CoveredCount
    signal_detection_ms: tuple[float, ...]
    reviewer_jobs_queued: int
    reviewer_jobs_started: int
    reviewer_jobs_decided: int
    reviewer_jobs_errored: int
    reviewer_jobs_capped: int
    reviewer_jobs_discarded: int
    reviewer_jobs_stale: int
    reviewer_queue_ms: tuple[float, ...]
    reviewer_inference_finishes: int
    reviewer_inference_ms: tuple[float, ...]
    reviewer_end_to_end_ms: tuple[float, ...]
    reviewer_decision_lead_ms: tuple[float, ...]
    reviewer_decision_lag_ms: tuple[float, ...]
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
    monotonic_receipt_events: int
    monotonic_clock_domains: int
    journal_bytes: int


@dataclass(frozen=True)
class ObjectiveOutcomeReport:
    artifacts: int
    arms: int
    judgeable_arms: int
    passing_arms: int
    failing_arms: int
    guidance_pairs: int
    judgeable_guidance_pairs: int
    guidance_better: int
    control_better: int
    guidance_tied: int
    neutral_pairs: int
    judgeable_neutral_pairs: int
    neutral_disagreements: int
    reported_tokens: int | None
    token_arms: int
    elapsed_ms: tuple[float, ...]
    paired_arm_costs: Mapping[str, "ObjectiveArmCost"]


@dataclass(frozen=True)
class ObjectiveArmCost:
    arms: int
    reported_tokens: int | None
    token_arms: int
    elapsed_ms: tuple[float, ...]


class ObjectiveOutcomeError(ValueError):
    """A durable experiment result cannot be projected safely."""


@dataclass(frozen=True)
class _ObjectiveArm:
    key: str
    pair_key: str
    arm: str
    classification: str
    reported_tokens: int | None
    elapsed_ms: float | None


@dataclass(frozen=True)
class _ReviewLifecycle:
    active_signals: int
    repeated_equivalent_actions: CoveredCount
    reads_without_frontier_expansion: CoveredCount
    signal_detection_ms: tuple[float, ...]
    jobs_queued: int
    jobs_started: int
    jobs_decided: int
    jobs_errored: int
    jobs_capped: int
    jobs_discarded: int
    jobs_stale: int
    queue_ms: tuple[float, ...]
    jobs_finished: int
    inference_ms: tuple[float, ...]
    end_to_end_ms: tuple[float, ...]
    decision_lead_ms: tuple[float, ...]
    decision_lag_ms: tuple[float, ...]


def measure_runtime_costs(
    journals: Iterable[tuple[Iterable[StepRecord], int]],
) -> RuntimeCostReport:
    surfaces: dict[str, SurfaceCost] = {"hook": SurfaceCost(), "app_server": SurfaceCost()}
    sessions = events = completed_turns = token_turns = token_observations = 0
    reviewer_calls = reviewer_tokens = journal_bytes = gate_calls = 0
    signal_candidates_active = reviewer_jobs_queued = reviewer_jobs_started = 0
    repeated_actions = [0, 0, 0]
    no_frontier_reads = [0, 0, 0]
    reviewer_jobs_decided = reviewer_jobs_errored = reviewer_jobs_capped = 0
    reviewer_jobs_discarded = reviewer_jobs_stale = 0
    reviewer_inference_finishes = 0
    reviewer_tokens_known = False
    token_sums = {field: 0 for field in _TOKEN_FIELDS}
    token_sessions = {field: 0 for field in _TOKEN_FIELDS}
    resources_by_surface: dict[str, set[str]] = {"hook": set(), "app_server": set()}
    resource_actions_by_surface = {"hook": 0, "app_server": 0}
    tool_duration_ms: list[float] = []
    reviewer_queue_ms: list[float] = []
    reviewer_inference_ms: list[float] = []
    signal_detection_ms: list[float] = []
    reviewer_end_to_end_ms: list[float] = []
    reviewer_decision_lead_ms: list[float] = []
    reviewer_decision_lag_ms: list[float] = []
    turn_wall_ms: list[float] = []
    hook_ms: list[float] = []
    ipc_ms: list[float] = []
    daemon_evaluation_ms: list[float] = []
    daemon_samples: dict[tuple[str, int], tuple[float, int]] = {}
    source_timestamps = receipt_timestamps = 0
    arrival_ordered_events = arrival_order_eligible_events = 0
    monotonic_receipt_events = 0
    monotonic_clock_ids: set[str] = set()

    for records_iter, size in journals:
        records = tuple(records_iter)
        sessions += 1
        journal_bytes += size
        surface = _surface(records)
        actions: set[str] = set()
        outcomes: set[str] = set()
        action_families: dict[str, str] = {}
        outcome_families: dict[str, str] = {}
        action_resources: dict[str, set[str]] = {}
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
            if event.observed_monotonic_ns is not None and event.monotonic_clock_id is not None:
                monotonic_receipt_events += 1
                monotonic_clock_ids.add(event.monotonic_clock_id)
            key = _action_key(record)
            if event.kind in _START_KINDS | _OUTCOME_KINDS:
                action_observations += 1
                if key is not None:
                    actions.add(key)
                    action_families.setdefault(key, _action_family(event.kind))
                    if resources := _event_resources(event.payload):
                        action_resources.setdefault(key, set()).update(resources)
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
                if _payload_id(event.payload, "review_job_id") is None and isinstance(
                    timing, Mapping
                ):
                    reviewer_inference_finishes += 1
                    _append_number(reviewer_inference_ms, timing.get("inference_ms"))
            if event.kind == "gate_ipc":
                gate_calls += 1
                _append_number(hook_ms, event.payload.get("hook_ms"))
                _append_number(ipc_ms, event.payload.get("ipc_ms"))
                _append_number(daemon_evaluation_ms, event.payload.get("daemon_evaluation_ms"))
                _append_daemon_sample(daemon_samples, event.payload.get("runtime_sample"))
        lifecycle = _review_lifecycle(records)
        signal_candidates_active += lifecycle.active_signals
        _add_covered_count(repeated_actions, lifecycle.repeated_equivalent_actions)
        _add_covered_count(no_frontier_reads, lifecycle.reads_without_frontier_expansion)
        signal_detection_ms.extend(lifecycle.signal_detection_ms)
        reviewer_jobs_queued += lifecycle.jobs_queued
        reviewer_jobs_started += lifecycle.jobs_started
        reviewer_jobs_decided += lifecycle.jobs_decided
        reviewer_jobs_errored += lifecycle.jobs_errored
        reviewer_jobs_capped += lifecycle.jobs_capped
        reviewer_jobs_discarded += lifecycle.jobs_discarded
        reviewer_jobs_stale += lifecycle.jobs_stale
        reviewer_queue_ms.extend(lifecycle.queue_ms)
        reviewer_inference_finishes += lifecycle.jobs_finished
        reviewer_inference_ms.extend(lifecycle.inference_ms)
        reviewer_end_to_end_ms.extend(lifecycle.end_to_end_ms)
        reviewer_decision_lead_ms.extend(lifecycle.decision_lead_ms)
        reviewer_decision_lag_ms.extend(lifecycle.decision_lag_ms)
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
        resources_by_surface[surface].update(
            resource for resources in action_resources.values() for resource in resources
        )
        resource_actions_by_surface[surface] += len(action_resources)
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
    surfaces = {
        surface: replace(
            cost,
            unique_resources=(
                len(resources_by_surface[surface]) if resource_actions_by_surface[surface] else None
            ),
            resource_actions=resource_actions_by_surface[surface],
        )
        for surface, cost in surfaces.items()
    }
    return RuntimeCostReport(
        sessions=sessions,
        events=events,
        surfaces=surfaces,
        completed_turns=completed_turns,
        token_turns=token_turns,
        token_observations=token_observations,
        cumulative_main_tokens=token_breakdown.total.value,
        main_token_breakdown=token_breakdown,
        reviewer_calls=reviewer_calls,
        reviewer_tokens=reviewer_tokens if reviewer_tokens_known else None,
        signal_candidates_active=signal_candidates_active,
        repeated_equivalent_actions=_total_covered_count(repeated_actions),
        reads_without_frontier_expansion=_total_covered_count(no_frontier_reads),
        signal_detection_ms=tuple(signal_detection_ms),
        reviewer_jobs_queued=reviewer_jobs_queued,
        reviewer_jobs_started=reviewer_jobs_started,
        reviewer_jobs_decided=reviewer_jobs_decided,
        reviewer_jobs_errored=reviewer_jobs_errored,
        reviewer_jobs_capped=reviewer_jobs_capped,
        reviewer_jobs_discarded=reviewer_jobs_discarded,
        reviewer_jobs_stale=reviewer_jobs_stale,
        reviewer_queue_ms=tuple(reviewer_queue_ms),
        reviewer_inference_finishes=reviewer_inference_finishes,
        reviewer_inference_ms=tuple(reviewer_inference_ms),
        reviewer_end_to_end_ms=tuple(reviewer_end_to_end_ms),
        reviewer_decision_lead_ms=tuple(reviewer_decision_lead_ms),
        reviewer_decision_lag_ms=tuple(reviewer_decision_lag_ms),
        turn_wall_ms=tuple(turn_wall_ms),
        tool_duration_ms=tuple(tool_duration_ms),
        gate_calls=gate_calls,
        hook_ms=tuple(hook_ms),
        ipc_ms=tuple(ipc_ms),
        daemon_evaluation_ms=tuple(daemon_evaluation_ms),
        daemon_resource_samples=len(daemon_samples),
        daemon_cpu_seconds=(
            sum(sample[1] for sample in latest_samples.values()) if latest_samples else None
        ),
        daemon_peak_rss_bytes=max((sample[2] for sample in latest_samples.values()), default=None),
        source_timestamps=source_timestamps,
        receipt_timestamps=receipt_timestamps,
        arrival_ordered_events=arrival_ordered_events,
        arrival_order_eligible_events=arrival_order_eligible_events,
        monotonic_receipt_events=monotonic_receipt_events,
        monotonic_clock_domains=len(monotonic_clock_ids),
        journal_bytes=journal_bytes,
    )


def _review_lifecycle(records: tuple[StepRecord, ...]) -> _ReviewLifecycle:
    by_event_id = {
        record.event.event_id: record for record in records if record.event.event_id is not None
    }
    signals: dict[str, tuple[StepRecord, StepRecord | None]] = {}
    queued: dict[str, StepRecord] = {}
    job_signals: dict[str, str] = {}
    started: dict[str, StepRecord] = {}
    decided: dict[str, StepRecord] = {}
    errored: dict[str, StepRecord] = {}
    capped: set[str] = set()
    discarded: set[str] = set()
    stale: set[str] = set()
    turn_boundaries: dict[tuple[str, str, int], StepRecord] = {}
    signal_ids: dict[str, set[str]] = {signal_type: set() for signal_type in _REPETITION_FEATURES}
    signal_features: dict[str, dict[str, int]] = {
        signal_type: {} for signal_type in _REPETITION_FEATURES
    }

    for record in records:
        event = record.event
        payload = event.payload
        if event.kind == "turn_completed" and (turn_key := _review_turn_key(record)) is not None:
            turn_boundaries.setdefault(turn_key, record)
        if event.kind in {"signal_candidate", "signal_candidate_suppressed"}:
            signal_type = payload.get("signal_type")
            signal_id = _payload_id(payload, "signal_id")
            if isinstance(signal_type, str) and signal_type in signal_ids and signal_id is not None:
                signal_ids[signal_type].add(signal_id)
                feature_name = _REPETITION_FEATURES[signal_type]
                features = payload.get("features")
                count = features.get(feature_name) if isinstance(features, Mapping) else None
                if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    previous = signal_features[signal_type].get(signal_id, 0)
                    signal_features[signal_type][signal_id] = max(previous, count)
        if (
            event.kind == "signal_candidate"
            and payload.get("status") == "active"
            and (signal_id := _payload_id(payload, "signal_id")) is not None
        ):
            evidence = payload.get("evidence_event_ids")
            first = (
                next(
                    (
                        by_event_id[event_id]
                        for event_id in evidence
                        if isinstance(event_id, str) and event_id in by_event_id
                    ),
                    None,
                )
                if isinstance(evidence, list)
                else None
            )
            signals.setdefault(signal_id, (record, first))
        job_id = _payload_id(payload, "review_job_id")
        if job_id is None:
            continue
        if event.kind == "review_job_queued":
            queued.setdefault(job_id, record)
            if (signal_id := _payload_id(payload, "signal_id")) is not None:
                job_signals.setdefault(job_id, signal_id)
        elif event.kind == "review_inference_started":
            started.setdefault(job_id, record)
        elif event.kind == "reviewer_decision":
            decided.setdefault(job_id, record)
        elif event.kind == "reviewer_error":
            errored.setdefault(job_id, record)
        elif event.kind == "reviewer_capped":
            capped.add(job_id)
        elif event.kind == "review_job_discarded":
            discarded.add(job_id)
        elif event.kind == "review_job_stale":
            stale.add(job_id)

    signal_detection_ms = tuple(
        duration
        for emitted, first in signals.values()
        if first is not None and (duration := _monotonic_elapsed_ms(first, emitted)) is not None
    )
    queue_ms: list[float] = []
    for job_id, start in started.items():
        duration = _monotonic_elapsed_ms(queued.get(job_id), start)
        if duration is None:
            duration = _number(start.event.payload.get("queue_ms"))
        if duration is not None:
            queue_ms.append(duration)
    terminals = {**errored, **decided}
    finished_job_ids = [job_id for job_id in started if job_id in terminals]
    inference_ms: list[float] = []
    for job_id in finished_job_ids:
        terminal = terminals[job_id]
        timing = terminal.event.payload.get("timing")
        duration = _number(timing.get("inference_ms")) if isinstance(timing, Mapping) else None
        if duration is None:
            duration = _monotonic_elapsed_ms(started[job_id], terminal)
        if duration is not None:
            inference_ms.append(duration)
    end_to_end_ms = tuple(
        duration
        for job_id, terminal in decided.items()
        if (signal_id := job_signals.get(job_id)) is not None
        and (signal := signals.get(signal_id)) is not None
        and signal[1] is not None
        and (duration := _monotonic_elapsed_ms(signal[1], terminal)) is not None
    )
    decision_lead_ms: list[float] = []
    decision_lag_ms: list[float] = []
    for decision in decided.values():
        turn_key = _review_turn_key(decision)
        boundary = turn_boundaries.get(turn_key) if turn_key is not None else None
        delta = _monotonic_delta_ms(decision, boundary) if boundary is not None else None
        if delta is not None:
            (decision_lead_ms if delta >= 0 else decision_lag_ms).append(abs(delta))
    return _ReviewLifecycle(
        active_signals=len(signals),
        repeated_equivalent_actions=_covered_signal_count(
            signal_ids["repeated_equivalent_tool_call"],
            signal_features["repeated_equivalent_tool_call"],
            first_is_not_repeated=True,
        ),
        reads_without_frontier_expansion=_covered_signal_count(
            signal_ids["repeated_read_no_frontier"],
            signal_features["repeated_read_no_frontier"],
        ),
        signal_detection_ms=signal_detection_ms,
        jobs_queued=len(queued),
        jobs_started=len(started),
        jobs_decided=len(decided),
        jobs_errored=len(errored),
        jobs_capped=len(capped),
        jobs_discarded=len(discarded),
        jobs_stale=len(stale),
        queue_ms=tuple(queue_ms),
        jobs_finished=len(finished_job_ids),
        inference_ms=tuple(inference_ms),
        end_to_end_ms=end_to_end_ms,
        decision_lead_ms=tuple(decision_lead_ms),
        decision_lag_ms=tuple(decision_lag_ms),
    )


def _covered_signal_count(
    signal_ids: set[str], counts: Mapping[str, int], *, first_is_not_repeated: bool = False
) -> CoveredCount:
    return CoveredCount(
        (
            sum(max(0, count - int(first_is_not_repeated)) for count in counts.values())
            if counts
            else None
        ),
        len(counts),
        len(signal_ids),
    )


def _add_covered_count(total: list[int], value: CoveredCount) -> None:
    if value.value is not None:
        total[0] += value.value
    total[1] += value.covered
    total[2] += value.eligible


def _total_covered_count(total: list[int]) -> CoveredCount:
    return CoveredCount(total[0] if total[1] else None, total[1], total[2])


def _payload_id(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _monotonic_elapsed_ms(start: StepRecord | None, end: StepRecord) -> float | None:
    duration = _monotonic_delta_ms(start, end) if start is not None else None
    if duration is None or duration < 0:
        return None
    return duration


def _monotonic_delta_ms(start: StepRecord, end: StepRecord) -> float | None:
    started = start.event.observed_monotonic_ns
    finished = end.event.observed_monotonic_ns
    clock_id = start.event.monotonic_clock_id
    if (
        started is None
        or finished is None
        or clock_id is None
        or clock_id != end.event.monotonic_clock_id
    ):
        return None
    return (finished - started) / 1_000_000


def _review_turn_key(record: StepRecord) -> tuple[str, str, int] | None:
    event = record.event
    identity = event.identity
    thread_id = identity.thread_id.value if identity is not None and identity.thread_id else None
    turn_id = _payload_id(event.payload, "target_turn_id") or _turn_id(record)
    target_epoch = event.payload.get("target_connection_epoch")
    epoch = (
        target_epoch
        if isinstance(target_epoch, int) and not isinstance(target_epoch, bool)
        else event.connection_epoch
    )
    if thread_id is None or turn_id is None or epoch is None:
        return None
    return thread_id, turn_id, epoch


def render_runtime_costs(report: RuntimeCostReport) -> str:
    lines = ["Runtime cost / efficiency (coverage-aware):", "  Main semantic actions:"]
    for surface, cost in report.surfaces.items():
        lines.append(
            f"    {surface}: actions={cost.actions} "
            f"(from {_observations(cost.action_observations)}), outcomes={cost.outcomes} "
            f"(from {_observations(cost.outcome_observations)}), "
            f"failed={cost.failed_outcomes}/{cost.classified_outcomes} classified"
        )
        resources = (
            f"{cost.unique_resources} unique" if cost.unique_resources is not None else "unknown"
        )
        lines.append(
            f"      resources={resources} ({cost.resource_actions}/{cost.actions} actions declared)"
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
    inference = _sample(report.reviewer_inference_ms, report.reviewer_inference_finishes)
    end_to_end = _sample(report.reviewer_end_to_end_ms, report.reviewer_jobs_decided)
    decision_lead = _sample(report.reviewer_decision_lead_ms, report.reviewer_jobs_decided)
    decision_lag = _sample(report.reviewer_decision_lag_ms, report.reviewer_jobs_decided)
    lines.append(
        f"  Spotter semantic: reviewer_calls={report.reviewer_calls}, "
        f"recorded_session_tokens={reviewer_tokens}; queue={queue}, "
        f"inference={inference}"
    )
    lines.append(
        "  Supervision lifecycle: "
        f"signal_delay={_sample(report.signal_detection_ms, report.signal_candidates_active)}, "
        f"detection_to_decision={end_to_end}; decision_boundary "
        f"lead={decision_lead}, lag={decision_lag}; "
        f"jobs queued={report.reviewer_jobs_queued} started={report.reviewer_jobs_started} "
        f"decided={report.reviewer_jobs_decided} errors={report.reviewer_jobs_errored} "
        f"capped={report.reviewer_jobs_capped} discarded={report.reviewer_jobs_discarded} "
        f"stale={report.reviewer_jobs_stale}"
    )
    lines.append(
        "  Detected repetition: "
        f"equivalent_actions={_covered_count(report.repeated_equivalent_actions)}, "
        "reads_without_frontier="
        f"{_covered_count(report.reads_without_frontier_expansion)}; "
        "source=durable #28 signal lifecycles"
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
        f"monotonic_receipt={report.monotonic_receipt_events}/{report.events} "
        f"across {report.monotonic_clock_domains} clock domains, "
        f"turn_wall(source)={_sample(report.turn_wall_ms, report.completed_turns)}, "
        f"tool_duration={_sample(report.tool_duration_ms, tool_outcomes)}"
    )
    lines.append(
        f"  Storage: journals={report.journal_bytes} bytes across {report.sessions} sessions"
    )
    return "\n".join(lines)


def measure_objective_outcomes(paths: Iterable[Path]) -> ObjectiveOutcomeReport:
    """Join durable mechanical outcomes with costs carried by the same arm row."""

    artifacts = 0
    arms: dict[str, _ObjectiveArm] = {}
    for path in paths:
        found = False
        for row in _result_rows(path):
            arm = _objective_arm(row, path)
            if arm is None:
                continue
            found = True
            previous = arms.get(arm.key)
            if previous is not None and previous != arm:
                raise ObjectiveOutcomeError(f"{path}: conflicting objective result {arm.key}")
            arms[arm.key] = arm
        artifacts += found

    judgeable = {"PASS", "TASK_FAIL"}
    values = tuple(arms.values())
    groups: dict[str, dict[str, _ObjectiveArm]] = {}
    for arm in values:
        groups.setdefault(arm.pair_key, {})[arm.arm] = arm

    guidance_pairs = judgeable_guidance_pairs = 0
    guidance_better = control_better = guidance_tied = 0
    neutral_pairs = judgeable_neutral_pairs = neutral_disagreements = 0
    paired_arms: dict[str, list[_ObjectiveArm]] = {}
    for pair in groups.values():
        if set(pair) == {"control", "guidance"}:
            guidance_pairs += 1
            for arm in pair.values():
                paired_arms.setdefault(arm.arm, []).append(arm)
            if all(row.classification in judgeable for row in pair.values()):
                judgeable_guidance_pairs += 1
                control_passed = pair["control"].classification == "PASS"
                guidance_passed = pair["guidance"].classification == "PASS"
                guidance_better += guidance_passed and not control_passed
                control_better += control_passed and not guidance_passed
                guidance_tied += guidance_passed == control_passed
        elif set(pair) == {"neutral_a", "neutral_b"}:
            neutral_pairs += 1
            for arm in pair.values():
                paired_arms.setdefault(arm.arm, []).append(arm)
            if all(row.classification in judgeable for row in pair.values()):
                judgeable_neutral_pairs += 1
                neutral_disagreements += (
                    pair["neutral_a"].classification != pair["neutral_b"].classification
                )

    tokens = [arm.reported_tokens for arm in values if arm.reported_tokens is not None]
    elapsed = tuple(arm.elapsed_ms for arm in values if arm.elapsed_ms is not None)
    return ObjectiveOutcomeReport(
        artifacts,
        len(values),
        sum(arm.classification in judgeable for arm in values),
        sum(arm.classification == "PASS" for arm in values),
        sum(arm.classification == "TASK_FAIL" for arm in values),
        guidance_pairs,
        judgeable_guidance_pairs,
        guidance_better,
        control_better,
        guidance_tied,
        neutral_pairs,
        judgeable_neutral_pairs,
        neutral_disagreements,
        sum(tokens) if tokens else None,
        len(tokens),
        elapsed,
        {name: _objective_arm_cost(rows) for name, rows in paired_arms.items()},
    )


def render_objective_outcomes(report: ObjectiveOutcomeReport) -> str:
    lines = ["Objective experiment/scorer outcomes (separate from user sessions):"]
    if not report.arms:
        lines.append("  no versioned objective result rows found")
        return "\n".join(lines)
    lines.append(
        f"  arms: pass={report.passing_arms}, task_fail={report.failing_arms}, "
        f"judgeable={report.judgeable_arms}/{report.arms} across {report.artifacts} artifacts"
    )
    lines.append(
        f"  guidance pairs: n={report.judgeable_guidance_pairs}/{report.guidance_pairs} "
        f"judgeable; guidance_better={report.guidance_better}, "
        f"control_better={report.control_better}, tied={report.guidance_tied}"
    )
    _append_paired_costs(lines, report, "guidance", ("control", "guidance"))
    lines.append(
        f"  neutral pairs: n={report.judgeable_neutral_pairs}/{report.neutral_pairs} "
        f"judgeable; disagreements={report.neutral_disagreements}"
    )
    _append_paired_costs(lines, report, "neutral", ("neutral_a", "neutral_b"))
    tokens = str(report.reported_tokens) if report.reported_tokens is not None else "unknown"
    lines.append(
        f"  per-arm cost join: agent_reported_tokens={tokens} "
        f"({report.token_arms}/{report.arms} arms), "
        f"elapsed={_sample(report.elapsed_ms, report.arms)}; "
        "identity=run/pair/arm from durable result rows"
    )
    return "\n".join(lines)


def _objective_arm_cost(arms: Iterable[_ObjectiveArm]) -> ObjectiveArmCost:
    values = tuple(arms)
    tokens = [arm.reported_tokens for arm in values if arm.reported_tokens is not None]
    elapsed = tuple(arm.elapsed_ms for arm in values if arm.elapsed_ms is not None)
    return ObjectiveArmCost(len(values), sum(tokens) if tokens else None, len(tokens), elapsed)


def _append_paired_costs(
    lines: list[str],
    report: ObjectiveOutcomeReport,
    pair_type: str,
    arm_names: tuple[str, str],
) -> None:
    costs = [report.paired_arm_costs.get(name) for name in arm_names]
    if not any(cost is not None for cost in costs):
        return
    rendered = []
    for name, cost in zip(arm_names, costs, strict=True):
        if cost is None:
            continue
        tokens = str(cost.reported_tokens) if cost.reported_tokens is not None else "unknown"
        rendered.append(
            f"{name} tokens={tokens} ({cost.token_arms}/{cost.arms} arms), "
            f"elapsed={_sample(cost.elapsed_ms, cost.arms)}"
        )
    lines.append(f"    {pair_type} arm costs: " + "; ".join(rendered))


def _result_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ObjectiveOutcomeError(f"cannot read objective results {path}: {error}") from error
    rows: list[Mapping[str, object]] = []
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1:
                break
            raise ObjectiveOutcomeError(f"{path}: corrupt row {index + 1}") from error
        if not isinstance(row, Mapping):
            raise ObjectiveOutcomeError(f"{path}: row {index + 1} is not an object")
        rows.append(row)
    return tuple(rows)


def _objective_arm(row: Mapping[str, object], path: Path) -> _ObjectiveArm | None:
    classification = row.get("classification")
    if classification is None:
        return None
    if classification not in _ARM_CLASSIFICATIONS:
        raise ObjectiveOutcomeError(f"{path}: unknown arm classification {classification!r}")
    schema = row.get("result_schema_version")
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise ObjectiveOutcomeError(f"{path}: objective result has no schema version")

    arm = row.get("arm")
    if not isinstance(arm, str) or arm not in {"control", "guidance", "neutral_a", "neutral_b"}:
        raise ObjectiveOutcomeError(f"{path}: invalid objective arm {arm!r}")
    run_id = row.get("run_id")
    experiment_pair_id = row.get("experiment_pair_id")
    experiment_id = row.get("experiment_id")
    pair = row.get("pair")
    if (
        isinstance(run_id, str)
        and run_id
        and isinstance(experiment_pair_id, str)
        and experiment_pair_id
    ):
        if schema != 1:
            raise ObjectiveOutcomeError(f"{path}: unsupported task result schema {schema}")
        pair_key = f"task:{run_id}:{experiment_pair_id}"
        key = f"{pair_key}:{arm}"
    elif (
        isinstance(experiment_id, str)
        and experiment_id
        and isinstance(pair, int)
        and not isinstance(pair, bool)
        and pair >= 0
    ):
        if schema not in {1, 2, 3}:
            raise ObjectiveOutcomeError(f"{path}: unsupported experiment result schema {schema}")
        pair_key = f"experiment:{experiment_id}:{pair}"
        key = f"{pair_key}:{arm}"
    else:
        raise ObjectiveOutcomeError(f"{path}: objective result has no stable run/pair identity")
    return _ObjectiveArm(
        key,
        pair_key,
        arm,
        classification,
        _agent_reported_tokens(row),
        _agent_elapsed(row),
    )


def _agent_reported_tokens(row: Mapping[str, object]) -> int | None:
    explicit = row.get("agent_reported_tokens")
    if isinstance(explicit, int) and not isinstance(explicit, bool) and explicit >= 0:
        return explicit
    stderr = row.get("agent_stderr")
    if not isinstance(stderr, str):
        return None
    matches = _TOKENS_USED_RE.findall(stderr)
    return int(matches[-1].replace(",", "")) if matches else None


def _agent_elapsed(row: Mapping[str, object]) -> float | None:
    elapsed = _number(row.get("agent_elapsed_ms"))
    return (
        elapsed
        if elapsed is not None and elapsed >= 0
        else _elapsed(row.get("started_at"), row.get("ended_at"))
    )


def _elapsed(started: object, ended: object) -> float | None:
    if not isinstance(started, str) or not isinstance(ended, str):
        return None
    try:
        duration = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
    except (TypeError, ValueError):
        return None
    return duration * 1000 if duration >= 0 else None


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


def _event_resources(payload: Mapping[str, object]) -> set[str]:
    resources: set[str] = set()
    files = payload.get("files")
    if isinstance(files, list):
        resources.update(f"file:{value}" for value in files if isinstance(value, str) and value)
    resource = payload.get("resource")
    if isinstance(resource, str) and resource:
        resources.add(f"resource:{resource}")
    server = payload.get("server") or payload.get("namespace")
    tool = payload.get("tool")
    if isinstance(server, str) and server and isinstance(tool, str) and tool:
        resources.add(f"tool:{server}/{tool}")
    return resources


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


def _covered_count(metric: CoveredCount) -> str:
    value = str(metric.value) if metric.value is not None else "unknown"
    return f"{value} ({metric.covered}/{metric.eligible} signal lifecycles)"
