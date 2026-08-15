"""Coverage-aware delay and post-window work from opportunity annotations."""

import math
from dataclasses import dataclass
from statistics import mean

from spotter.opportunities import EventAnchor, load_opportunities, matches
from spotter.outcomes import outcome_failure
from spotter.snapshot import StepRecord

_ACTION_KINDS = {"tool_proposal", "command_started", "tool_started", "file_change_started"}
_OUTCOME_KINDS = {"tool_result", "command_result", "file_edit"}


@dataclass(frozen=True)
class OpportunityTimingReport:
    annotations: int
    unique_opportunities: int
    current_annotations: int
    stale_annotations: int
    early: int
    within_window: int
    late: int
    never: int
    unjudgeable: int
    step_from_earliest: tuple[int, ...]
    step_from_latest: tuple[int, ...]
    source_wall_ms: tuple[float, ...]
    post_window_actions: tuple[int, ...]
    post_window_unattributed_actions: tuple[int, ...]
    post_window_failed_outcomes: tuple[int, ...]
    post_window_unattributed_failed_outcomes: tuple[int, ...]
    post_window_files: tuple[int, ...]
    linked_signal_annotations: int
    review_jobs_queued: int
    review_inferences_started: int
    review_decisions: int
    review_early: int
    review_within_window: int
    review_late: int
    review_terminal_without_decision: int
    review_unjudgeable: int
    review_stale: int
    review_step_from_earliest: tuple[int, ...]
    review_step_from_latest: tuple[int, ...]
    signal_to_queue_steps: tuple[int, ...]
    queue_to_inference_steps: tuple[int, ...]
    inference_to_decision_steps: tuple[int, ...]
    queue_to_decision_steps: tuple[int, ...]


def measure_opportunity_timing(session: str, records: list[StepRecord]) -> OpportunityTimingReport:
    windows = tuple(load_opportunities(session).values())
    early = within = late = never = unjudgeable = stale = 0
    step_from_earliest: list[int] = []
    step_from_latest: list[int] = []
    source_wall_ms: list[float] = []
    post_window_actions: list[int] = []
    post_window_unattributed_actions: list[int] = []
    post_window_failed: list[int] = []
    post_window_unattributed_failed: list[int] = []
    post_window_files: list[int] = []
    linked_signals = review_queued = review_started = review_decisions = 0
    review_early = review_within = review_late = 0
    review_terminal_without_decision = review_unjudgeable = review_stale = 0
    review_step_from_earliest: list[int] = []
    review_step_from_latest: list[int] = []
    signal_to_queue_steps: list[int] = []
    queue_to_inference_steps: list[int] = []
    inference_to_decision_steps: list[int] = []
    queue_to_decision_steps: list[int] = []

    for window in windows:
        if not matches(window, records):
            stale += 1
            continue
        candidate = _linked_signal(window.required_evidence, records)
        earliest = window.observable_earliest.step
        latest = window.observable_latest.step
        measurement_end = candidate.step if candidate is not None else len(records) - 1
        if measurement_end >= earliest and _has_observation_gap(
            records[earliest : measurement_end + 1]
        ):
            unjudgeable += 1
            continue
        if candidate is None:
            if _window_closed(latest, records):
                never += 1
            else:
                unjudgeable += 1
            end = len(records)
        else:
            end = max(earliest + 1, candidate.step)
            step_from_earliest.append(candidate.step - earliest)
            step_from_latest.append(candidate.step - latest)
            wall = _source_wall_delay(records[earliest], candidate)
            if wall is not None:
                source_wall_ms.append(wall)
            if candidate.step < earliest:
                early += 1
            elif candidate.step <= latest:
                within += 1
            else:
                late += 1
        work = records[earliest + 1 : end]
        actions, unattributed_actions = _unique_actions(work)
        failed, unattributed_failed = _unique_failed_outcomes(work)
        post_window_actions.append(actions)
        post_window_unattributed_actions.append(unattributed_actions)
        post_window_failed.append(failed)
        post_window_unattributed_failed.append(unattributed_failed)
        post_window_files.append(len(_files(work)))
        if candidate is None:
            continue
        linked_signals += 1
        queued = _linked_review_job(candidate, records)
        if queued is None:
            if _has_observation_gap(records[candidate.step :]):
                review_unjudgeable += 1
            elif _window_closed(latest, records):
                review_terminal_without_decision += 1
            else:
                review_unjudgeable += 1
            continue
        review_queued += 1
        inference = _review_inference(queued, records)
        if inference is not None:
            review_started += 1
        decision = _review_decision(queued, records)
        review_end = decision.step if decision is not None else len(records) - 1
        if _has_observation_gap(records[candidate.step : review_end + 1]):
            review_unjudgeable += 1
            continue
        signal_to_queue_steps.append(queued.step - candidate.step)
        if inference is not None:
            queue_to_inference_steps.append(inference.step - queued.step)
        if decision is None:
            if _window_closed(queued.step, records):
                review_terminal_without_decision += 1
            else:
                review_unjudgeable += 1
            continue
        if decision.event.payload.get("stale") is True:
            review_stale += 1
            continue
        review_decisions += 1
        review_step_from_earliest.append(decision.step - earliest)
        review_step_from_latest.append(decision.step - latest)
        queue_to_decision_steps.append(decision.step - queued.step)
        if inference is not None and inference.step <= decision.step:
            inference_to_decision_steps.append(decision.step - inference.step)
        if decision.step < earliest:
            review_early += 1
        elif decision.step <= latest:
            review_within += 1
        else:
            review_late += 1

    return OpportunityTimingReport(
        annotations=len(windows),
        unique_opportunities=len({window.opportunity_id for window in windows}),
        current_annotations=len(windows) - stale,
        stale_annotations=stale,
        early=early,
        within_window=within,
        late=late,
        never=never,
        unjudgeable=unjudgeable,
        step_from_earliest=tuple(step_from_earliest),
        step_from_latest=tuple(step_from_latest),
        source_wall_ms=tuple(source_wall_ms),
        post_window_actions=tuple(post_window_actions),
        post_window_unattributed_actions=tuple(post_window_unattributed_actions),
        post_window_failed_outcomes=tuple(post_window_failed),
        post_window_unattributed_failed_outcomes=tuple(post_window_unattributed_failed),
        post_window_files=tuple(post_window_files),
        linked_signal_annotations=linked_signals,
        review_jobs_queued=review_queued,
        review_inferences_started=review_started,
        review_decisions=review_decisions,
        review_early=review_early,
        review_within_window=review_within,
        review_late=review_late,
        review_terminal_without_decision=review_terminal_without_decision,
        review_unjudgeable=review_unjudgeable,
        review_stale=review_stale,
        review_step_from_earliest=tuple(review_step_from_earliest),
        review_step_from_latest=tuple(review_step_from_latest),
        signal_to_queue_steps=tuple(signal_to_queue_steps),
        queue_to_inference_steps=tuple(queue_to_inference_steps),
        inference_to_decision_steps=tuple(inference_to_decision_steps),
        queue_to_decision_steps=tuple(queue_to_decision_steps),
    )


def render_opportunity_timing(report: OpportunityTimingReport) -> str:
    if not report.annotations:
        return "Intervention opportunity timing (annotation-aware):\n  no opportunity annotations"
    linked = report.linked_signal_annotations
    queued = report.review_jobs_queued
    started = report.review_inferences_started
    return "\n".join(
        [
            "Intervention opportunity timing (annotation-aware):",
            "  Coverage: "
            f"{report.current_annotations}/{report.annotations} current annotations across "
            f"{report.unique_opportunities} opportunities; stale={report.stale_annotations}",
            "  Evidence-linked signal: "
            f"EARLY={report.early} WITHIN_WINDOW={report.within_window} "
            f"LATE={report.late} NEVER={report.never} UNJUDGEABLE={report.unjudgeable}; "
            f"step_from_earliest={_sample(report.step_from_earliest, report.current_annotations)}, "
            f"step_from_latest={_sample(report.step_from_latest, report.current_annotations)}, "
            f"source_wall={_sample(report.source_wall_ms, report.current_annotations, 'ms')}",
            "  Post-window work until linked signal or journal end: "
            f"actions={_sample(report.post_window_actions, report.current_annotations)}, "
            "failed_outcomes="
            f"{_sample(report.post_window_failed_outcomes, report.current_annotations)}, "
            f"files={_sample(report.post_window_files, report.current_annotations)}; "
            "unattributed observations: actions="
            f"{sum(report.post_window_unattributed_actions)}, failed_outcomes="
            f"{sum(report.post_window_unattributed_failed_outcomes)}",
            "  Link rule: an active candidate must cite every required evidence event; "
            "unrelated candidates do not stop the clock",
            "  Evidence-linked reviewer: "
            f"signals={report.linked_signal_annotations}, queued={report.review_jobs_queued}, "
            f"started={report.review_inferences_started}, decided={report.review_decisions}; "
            f"EARLY={report.review_early} "
            f"WITHIN_WINDOW={report.review_within_window} LATE={report.review_late} "
            f"TERMINAL_NO_DECISION={report.review_terminal_without_decision} "
            f"UNJUDGEABLE={report.review_unjudgeable} STALE={report.review_stale}",
            "  Reviewer step delay: "
            f"from_earliest={_sample(report.review_step_from_earliest, linked)}, "
            f"from_latest={_sample(report.review_step_from_latest, linked)}, "
            f"signal_to_queue={_sample(report.signal_to_queue_steps, linked)}, "
            f"queue_to_inference={_sample(report.queue_to_inference_steps, queued)}, "
            f"inference_to_decision={_sample(report.inference_to_decision_steps, started)}, "
            f"queue_to_decision={_sample(report.queue_to_decision_steps, queued)}",
        ]
    )


def merge_opportunity_timing(
    reports: list[OpportunityTimingReport],
) -> OpportunityTimingReport:
    return OpportunityTimingReport(
        annotations=sum(report.annotations for report in reports),
        unique_opportunities=sum(report.unique_opportunities for report in reports),
        current_annotations=sum(report.current_annotations for report in reports),
        stale_annotations=sum(report.stale_annotations for report in reports),
        early=sum(report.early for report in reports),
        within_window=sum(report.within_window for report in reports),
        late=sum(report.late for report in reports),
        never=sum(report.never for report in reports),
        unjudgeable=sum(report.unjudgeable for report in reports),
        step_from_earliest=tuple(
            value for report in reports for value in report.step_from_earliest
        ),
        step_from_latest=tuple(value for report in reports for value in report.step_from_latest),
        source_wall_ms=tuple(value for report in reports for value in report.source_wall_ms),
        post_window_actions=tuple(
            value for report in reports for value in report.post_window_actions
        ),
        post_window_unattributed_actions=tuple(
            value for report in reports for value in report.post_window_unattributed_actions
        ),
        post_window_failed_outcomes=tuple(
            value for report in reports for value in report.post_window_failed_outcomes
        ),
        post_window_unattributed_failed_outcomes=tuple(
            value for report in reports for value in report.post_window_unattributed_failed_outcomes
        ),
        post_window_files=tuple(value for report in reports for value in report.post_window_files),
        linked_signal_annotations=sum(report.linked_signal_annotations for report in reports),
        review_jobs_queued=sum(report.review_jobs_queued for report in reports),
        review_inferences_started=sum(report.review_inferences_started for report in reports),
        review_decisions=sum(report.review_decisions for report in reports),
        review_early=sum(report.review_early for report in reports),
        review_within_window=sum(report.review_within_window for report in reports),
        review_late=sum(report.review_late for report in reports),
        review_terminal_without_decision=sum(
            report.review_terminal_without_decision for report in reports
        ),
        review_unjudgeable=sum(report.review_unjudgeable for report in reports),
        review_stale=sum(report.review_stale for report in reports),
        review_step_from_earliest=tuple(
            value for report in reports for value in report.review_step_from_earliest
        ),
        review_step_from_latest=tuple(
            value for report in reports for value in report.review_step_from_latest
        ),
        signal_to_queue_steps=tuple(
            value for report in reports for value in report.signal_to_queue_steps
        ),
        queue_to_inference_steps=tuple(
            value for report in reports for value in report.queue_to_inference_steps
        ),
        inference_to_decision_steps=tuple(
            value for report in reports for value in report.inference_to_decision_steps
        ),
        queue_to_decision_steps=tuple(
            value for report in reports for value in report.queue_to_decision_steps
        ),
    )


def _linked_signal(
    required_evidence: tuple[EventAnchor, ...], records: list[StepRecord]
) -> StepRecord | None:
    required = {anchor.event_id for anchor in required_evidence}
    for record in records:
        event = record.event
        evidence = event.payload.get("evidence_event_ids")
        if (
            event.kind == "signal_candidate"
            and event.payload.get("status") == "active"
            and isinstance(evidence, list)
            and required.issubset(value for value in evidence if isinstance(value, str))
        ):
            return record
    return None


def _linked_review_job(candidate: StepRecord, records: list[StepRecord]) -> StepRecord | None:
    candidate_id = candidate.event.event_id
    if candidate_id is None:
        return None
    for record in records[candidate.step + 1 :]:
        if record.event.kind != "review_job_queued":
            continue
        payload = record.event.payload
        linked = payload.get("candidate_event_ids")
        if payload.get("candidate_event_id") == candidate_id or (
            isinstance(linked, list) and candidate_id in linked
        ):
            return record
    return None


def _review_decision(queued: StepRecord, records: list[StepRecord]) -> StepRecord | None:
    job_id = queued.event.payload.get("review_job_id")
    if not isinstance(job_id, str) or not job_id:
        return None
    return next(
        (
            record
            for record in records[queued.step + 1 :]
            if record.event.kind == "reviewer_decision"
            and record.event.payload.get("review_job_id") == job_id
        ),
        None,
    )


def _review_inference(queued: StepRecord, records: list[StepRecord]) -> StepRecord | None:
    job_id = queued.event.payload.get("review_job_id")
    if not isinstance(job_id, str) or not job_id:
        return None
    return next(
        (
            record
            for record in records[queued.step + 1 :]
            if record.event.kind == "review_inference_started"
            and record.event.payload.get("review_job_id") == job_id
        ),
        None,
    )


def _window_closed(latest: int, records: list[StepRecord]) -> bool:
    terminal_kinds = {
        "session_end",
        "thread_archived",
        "thread_closed",
        "thread_deleted",
        "turn_completed",
    }
    return any(record.event.kind in terminal_kinds for record in records[latest:])


def _has_observation_gap(records: list[StepRecord]) -> bool:
    return any(
        record.event.kind in {"observation_gap", "runtime_attachment_unavailable"}
        for record in records
    )


def _source_wall_delay(start: StepRecord, end: StepRecord) -> float | None:
    left = start.event
    right = end.event
    if (
        left.connection_epoch is None
        or left.connection_epoch != right.connection_epoch
        or left.occurred_at is None
        or right.occurred_at is None
        or not math.isfinite(left.occurred_at)
        or not math.isfinite(right.occurred_at)
        or right.occurred_at < left.occurred_at
    ):
        return None
    return (right.occurred_at - left.occurred_at) * 1000


def _unique_actions(records: list[StepRecord]) -> tuple[int, int]:
    actions = [record for record in records if record.event.kind in _ACTION_KINDS]
    identities = {
        identity for record in actions if (identity := _operation_identity(record)) is not None
    }
    return len(identities), sum(_operation_identity(record) is None for record in actions)


def _unique_failed_outcomes(records: list[StepRecord]) -> tuple[int, int]:
    failures = [
        record
        for record in records
        if record.event.kind in _OUTCOME_KINDS and outcome_failure(record.event.payload) is True
    ]
    identities = {
        identity for record in failures if (identity := _operation_identity(record)) is not None
    }
    return len(identities), sum(_operation_identity(record) is None for record in failures)


def _operation_identity(record: StepRecord) -> str | None:
    value = record.event.operation_id or record.event.payload.get("tool_use_id")
    if isinstance(value, str) and value:
        return value
    return None


def _files(records: list[StepRecord]) -> set[str]:
    files: set[str] = set()
    for record in records:
        values = record.event.payload.get("files")
        if isinstance(values, list):
            files.update(value for value in values if isinstance(value, str) and value)
    return files


def _sample(values: tuple[int | float, ...], eligible: int, unit: str = "") -> str:
    if not values:
        return f"unknown (0/{eligible})"
    suffix = unit
    return (
        f"avg={mean(values):.2f}{suffix} min={min(values):.2f}{suffix} "
        f"max={max(values):.2f}{suffix} ({len(values)}/{eligible})"
    )
