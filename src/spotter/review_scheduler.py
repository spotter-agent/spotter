"""Durable, deduplicated reviewer jobs created from signal candidates."""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from spotter.identity import ThreadId, TurnId
from spotter.reviewer_input import ReviewerInput, build_reviewer_input
from spotter.snapshot import StepRecord
from spotter.thread_state import ThreadState, ThreadStateError, ThreadStateStore
from spotter.trace import TraceEvent, TraceProvenance


@dataclass(frozen=True)
class ReviewerJob:
    job_id: str
    signal_id: str
    signal_type: str
    thread_id: ThreadId
    target_turn_id: TurnId
    target_connection_epoch: int | None
    state_version: int
    candidate_event_id: str
    created_at: float | None
    snapshot: ThreadState
    reviewer_input: ReviewerInput
    signal_ids: tuple[str, ...]
    signal_types: tuple[str, ...]
    candidate_event_ids: tuple[str, ...]
    config_generation: str | None = None
    reviewer_model: str | None = None

    def queued_event(self, trigger: TraceEvent) -> TraceEvent:
        return TraceEvent(
            "review_job_queued",
            {
                "review_job_id": self.job_id,
                "signal_id": self.signal_id,
                "signal_type": self.signal_type,
                "signal_ids": list(self.signal_ids),
                "signal_types": list(self.signal_types),
                "thread_id": self.thread_id.value,
                "target_turn_id": self.target_turn_id.value,
                "target_connection_epoch": self.target_connection_epoch,
                "state_version": self.state_version,
                "candidate_event_id": self.candidate_event_id,
                "candidate_event_ids": list(self.candidate_event_ids),
                "review_trigger": "signal",
                "input_coverage": self.reviewer_input.coverage(),
            },
            event_id=f"spotter:review-job:{self.job_id}:queued",
            occurred_at=self.created_at,
            identity=trigger.identity,
            provenance=TraceProvenance("spotterd", "review_scheduler"),
            connection_epoch=trigger.connection_epoch,
            config_generation=trigger.config_generation,
        )

    def discarded_event(self, trigger: TraceEvent, reason: str) -> TraceEvent:
        return TraceEvent(
            "review_job_discarded",
            {
                "review_job_id": self.job_id,
                "signal_id": self.signal_id,
                "signal_ids": list(self.signal_ids),
                "reason": reason,
                "target_turn_id": self.target_turn_id.value,
                "target_connection_epoch": self.target_connection_epoch,
                "state_version": self.state_version,
            },
            event_id=f"spotter:review-job:{self.job_id}:discarded",
            occurred_at=trigger.occurred_at,
            identity=trigger.identity,
            provenance=TraceProvenance("spotterd", "review_scheduler"),
            connection_epoch=trigger.connection_epoch,
            config_generation=trigger.config_generation,
        )

    def stale_event(self, trigger: TraceEvent, reason: str) -> TraceEvent:
        return TraceEvent(
            "review_job_stale",
            {
                "review_job_id": self.job_id,
                "signal_id": self.signal_id,
                "signal_ids": list(self.signal_ids),
                "reason": reason,
                "target_turn_id": self.target_turn_id.value,
                "target_connection_epoch": self.target_connection_epoch,
                "state_version": self.state_version,
            },
            event_id=f"spotter:review-job:{self.job_id}:stale",
            occurred_at=trigger.occurred_at,
            identity=trigger.identity,
            provenance=TraceProvenance("spotterd", "review_scheduler"),
            connection_epoch=trigger.connection_epoch,
            config_generation=trigger.config_generation,
        )


class ReviewSchedulingError(ValueError):
    """A candidate cannot be bound to the immutable state it names."""


class ReviewScheduler:
    """Turn active signals into one pending job per candidate lifecycle."""

    def __init__(self) -> None:
        self._jobs: dict[str, ReviewerJob] = {}
        self._job_by_signal: dict[str, str] = {}
        self._seen_signal_ids: set[str] = set()
        self._started_job_ids: set[str] = set()

    def pending(self) -> tuple[ReviewerJob, ...]:
        return tuple(self._jobs.values())

    def get(self, job_id: str) -> ReviewerJob | None:
        return self._jobs.get(job_id)

    def update(
        self,
        event: TraceEvent,
        state_before: ThreadState | None,
        state_after: ThreadState,
    ) -> tuple[TraceEvent, ...]:
        transitions: list[TraceEvent] = []
        job_id = event.payload.get("review_job_id")
        if isinstance(job_id, str):
            if event.kind == "review_inference_started" and job_id in self._jobs:
                self._started_job_ids.add(job_id)
            elif event.kind in {"reviewer_decision", "reviewer_error", "reviewer_capped"}:
                self._remove(job_id)
                return ()
        for job in tuple(self._jobs.values()):
            if job.thread_id != state_after.thread_id:
                continue
            reason = None
            if _terminal_answer_matches(state_after, job.target_turn_id) and (
                state_before is None
                or not _terminal_answer_matches(state_before, job.target_turn_id)
            ):
                reason = "terminal_answer_settled"
            elif (
                state_after.connection_epoch != job.target_connection_epoch
                or state_after.active_turn_id != job.target_turn_id
                or event.kind
                in {
                    "observation_gap",
                    "runtime_attachment_unavailable",
                    "thread_archived",
                    "thread_closed",
                    "thread_deleted",
                    "turn_completed",
                }
            ):
                reason = "target_changed"
            if reason is not None:
                transitions.append(self._discard(job, event, reason))

        if event.kind != "signal_candidate":
            return tuple(transitions)
        if state_before is None and event.payload.get("status") == "active":
            raise ReviewSchedulingError("active signal has no source ThreadState snapshot")
        transitions.extend(
            self.update_candidates((event,), state_before or state_after, state_after)
        )
        return tuple(transitions)

    def update_candidates(
        self,
        events: Iterable[TraceEvent],
        source_snapshot: ThreadState,
        state_after: ThreadState,
    ) -> tuple[TraceEvent, ...]:
        """Schedule one immutable job for related candidates from one source state."""

        transitions: list[TraceEvent] = []
        active: list[TraceEvent] = []
        for event in events:
            if event.kind != "signal_candidate":
                continue
            signal_id = _required_string(event.payload, "signal_id")
            status = event.payload.get("status")
            if status in {"resolved", "stale"}:
                job_id = self._job_by_signal.get(signal_id)
                job = self._jobs.get(job_id) if job_id is not None else None
                if job is not None:
                    transitions.append(self._discard(job, event, f"signal_{status}"))
            elif status == "active" and signal_id not in self._seen_signal_ids:
                active.append(event)
        if not active:
            return tuple(transitions)

        job = _job(tuple(active), source_snapshot)
        self._seen_signal_ids.update(job.signal_ids)
        trigger = active[-1]
        if source_snapshot.active_turn_id != job.target_turn_id:
            transitions.append(job.discarded_event(trigger, "target_not_active"))
            return tuple(transitions)
        if _terminal_answer_matches(state_after, job.target_turn_id):
            transitions.append(job.discarded_event(trigger, "terminal_answer_settled"))
            return tuple(transitions)
        if (
            state_after.thread_id != job.thread_id
            or state_after.active_turn_id != job.target_turn_id
            or state_after.connection_epoch != job.target_connection_epoch
        ):
            transitions.append(job.discarded_event(trigger, "target_changed"))
            return tuple(transitions)
        self._jobs[job.job_id] = job
        for signal_id in job.signal_ids:
            self._job_by_signal[signal_id] = job.job_id
        transitions.append(job.queued_event(trigger))
        return tuple(transitions)

    def hydrate(self, records: Iterable[StepRecord]) -> tuple[TraceEvent, ...]:
        """Rebuild pending jobs and return scheduler transitions missing from the journal."""

        self._jobs.clear()
        self._job_by_signal.clear()
        self._seen_signal_ids.clear()
        self._started_job_ids.clear()
        replay = ThreadStateStore()
        snapshots: dict[tuple[ThreadId, int], ThreadState] = {}
        missing: dict[str, TraceEvent] = {}
        candidate_group: list[TraceEvent] = []
        candidate_key: tuple[ThreadId, int] | None = None

        def flush_candidates() -> None:
            nonlocal candidate_key
            if not candidate_group or candidate_key is None:
                return
            thread_id, version = candidate_key
            snapshot = snapshots.get(candidate_key)
            if snapshot is None:
                raise ReviewSchedulingError("candidate source ThreadState snapshot is missing")
            state_after = replay.snapshot(thread_id)
            for transition in self.update_candidates(candidate_group, snapshot, state_after):
                assert transition.event_id is not None
                missing[transition.event_id] = transition
            candidate_group.clear()
            candidate_key = None

        for record in records:
            event = record.event
            identity = event.identity
            if identity is None or identity.thread_id is None:
                continue
            thread_id = identity.thread_id
            next_key: tuple[ThreadId, int] | None = None
            if event.kind == "signal_candidate":
                version = event.payload.get("state_version")
                if isinstance(version, int) and not isinstance(version, bool):
                    next_key = (thread_id, version)
            if (
                candidate_group
                and next_key != candidate_key
                and event.kind != "signal_candidate_suppressed"
            ):
                flush_candidates()
            try:
                state_before = replay.snapshot(thread_id)
            except ThreadStateError:
                state_before = None
            state_after = replay.observe(event)
            snapshots[(thread_id, state_after.version)] = state_after
            if event.event_id is not None:
                missing.pop(event.event_id, None)
            if event.kind == "signal_candidate":
                if next_key is None:
                    raise ReviewSchedulingError("signal candidate has no integer state_version")
                candidate_key = next_key
                candidate_group.append(event)
                continue
            if event.kind == "signal_candidate_suppressed":
                continue
            for transition in self.update(event, state_before, state_after):
                assert transition.event_id is not None
                missing[transition.event_id] = transition
        flush_candidates()
        return tuple(missing.values())

    def _discard(self, job: ReviewerJob, trigger: TraceEvent, reason: str) -> TraceEvent:
        started = job.job_id in self._started_job_ids
        self._remove(job.job_id)
        return job.stale_event(trigger, reason) if started else job.discarded_event(trigger, reason)

    def _remove(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        self._started_job_ids.discard(job_id)
        if job is not None:
            for signal_id in job.signal_ids:
                self._job_by_signal.pop(signal_id, None)


def _terminal_answer_matches(state: ThreadState, turn_id: TurnId) -> bool:
    answer = state.execution.terminal_answer
    return answer is not None and answer.provenance.turn_id == turn_id


def _job(events: tuple[TraceEvent, ...], snapshot: ThreadState) -> ReviewerJob:
    if not events:
        raise ReviewSchedulingError("review job has no candidates")
    signal_ids = tuple(_required_string(event.payload, "signal_id") for event in events)
    signal_types = tuple(_required_string(event.payload, "signal_type") for event in events)
    candidate_event_ids = tuple(event.event_id for event in events if event.event_id is not None)
    if len(candidate_event_ids) != len(events):
        raise ReviewSchedulingError("active signal has no durable event identity")
    event = events[-1]
    identity = event.identity
    if identity is None or identity.thread_id is None or identity.turn_id is None:
        raise ReviewSchedulingError("active signal has no exact thread/turn identity")
    thread_id = identity.thread_id
    turn_id = identity.turn_id
    state_version = event.payload.get("state_version")
    target_epoch = event.payload.get("target_connection_epoch")
    if not isinstance(state_version, int) or isinstance(state_version, bool):
        raise ReviewSchedulingError("active signal has no integer state_version")
    if target_epoch is not None and (
        not isinstance(target_epoch, int) or isinstance(target_epoch, bool)
    ):
        raise ReviewSchedulingError("active signal has an invalid connection epoch")
    for candidate in events:
        if (
            candidate.identity != identity
            or candidate.payload.get("thread_id") != thread_id.value
            or candidate.payload.get("turn_id") != turn_id.value
            or candidate.payload.get("state_version") != state_version
            or candidate.payload.get("target_connection_epoch") != target_epoch
        ):
            raise ReviewSchedulingError("merged signals do not share one immutable target")
    if (
        snapshot.thread_id != thread_id
        or snapshot.version != state_version
        or snapshot.identity.turn_id != turn_id
        or snapshot.connection_epoch != target_epoch
    ):
        raise ReviewSchedulingError("active signal target does not match its ThreadState snapshot")
    merged_payload = _merge_candidate_payload(events, signal_ids, signal_types)
    reviewer_input = build_reviewer_input(merged_payload, snapshot)
    job_key = (
        f"{signal_ids[0]}:{candidate_event_ids[0]}"
        if len(events) == 1
        else ":".join(candidate_event_ids)
    )
    job_id = hashlib.sha256(job_key.encode()).hexdigest()
    return ReviewerJob(
        job_id,
        signal_ids[0],
        signal_types[0],
        thread_id,
        turn_id,
        target_epoch,
        state_version,
        candidate_event_ids[0],
        event.occurred_at,
        snapshot,
        reviewer_input,
        signal_ids,
        signal_types,
        candidate_event_ids,
    )


def _merge_candidate_payload(
    events: tuple[TraceEvent, ...],
    signal_ids: tuple[str, ...],
    signal_types: tuple[str, ...],
) -> dict[str, object]:
    if len(events) == 1:
        return dict(events[0].payload)

    evidence: list[str] = []
    resources: list[str] = []
    features: dict[str, object] = {}
    severities: list[int | float] = []
    for event, signal_type in zip(events, signal_types, strict=True):
        evidence.extend(_strings(event.payload.get("evidence_event_ids")))
        resources.extend(_strings(event.payload.get("involved_resources")))
        severity = event.payload.get("severity_hint")
        if isinstance(severity, int | float) and not isinstance(severity, bool):
            severities.append(severity)
        raw_features = event.payload.get("features")
        if isinstance(raw_features, Mapping):
            features.update(
                {
                    f"{signal_type}.{key}": value
                    for key, value in raw_features.items()
                    if isinstance(key, str)
                }
            )
    digest = hashlib.sha256(":".join(signal_ids).encode()).hexdigest()
    return {
        "signal_id": f"merged:{digest}",
        "signal_type": "+".join(dict.fromkeys(signal_types)),
        "severity_hint": max(severities) if severities else None,
        "evidence_event_ids": list(dict.fromkeys(evidence)),
        "involved_resources": list(dict.fromkeys(resources)),
        "features": features,
    }


def _strings(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ReviewSchedulingError(f"signal candidate has no {key}")
    return value
