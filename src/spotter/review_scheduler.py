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

    def queued_event(self, trigger: TraceEvent) -> TraceEvent:
        return TraceEvent(
            "review_job_queued",
            {
                "review_job_id": self.job_id,
                "signal_id": self.signal_id,
                "signal_type": self.signal_type,
                "thread_id": self.thread_id.value,
                "target_turn_id": self.target_turn_id.value,
                "target_connection_epoch": self.target_connection_epoch,
                "state_version": self.state_version,
                "candidate_event_id": self.candidate_event_id,
                "input_coverage": self.reviewer_input.coverage(),
            },
            event_id=f"spotter:review-job:{self.job_id}:queued",
            occurred_at=self.created_at,
            identity=trigger.identity,
            provenance=TraceProvenance("spotterd", "review_scheduler"),
            connection_epoch=trigger.connection_epoch,
        )

    def discarded_event(self, trigger: TraceEvent, reason: str) -> TraceEvent:
        return TraceEvent(
            "review_job_discarded",
            {
                "review_job_id": self.job_id,
                "signal_id": self.signal_id,
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
        )

    def stale_event(self, trigger: TraceEvent, reason: str) -> TraceEvent:
        return TraceEvent(
            "review_job_stale",
            {
                "review_job_id": self.job_id,
                "signal_id": self.signal_id,
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
            if (
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
                transitions.append(self._discard(job, event, "target_changed"))

        if event.kind != "signal_candidate":
            return tuple(transitions)
        status = event.payload.get("status")
        signal_id = _required_string(event.payload, "signal_id")
        if status in {"resolved", "stale"}:
            job_id = self._job_by_signal.get(signal_id)
            resolved_job = self._jobs.get(job_id) if job_id is not None else None
            if resolved_job is not None:
                transitions.append(self._discard(resolved_job, event, f"signal_{status}"))
            return tuple(transitions)
        if status != "active" or signal_id in self._seen_signal_ids:
            return tuple(transitions)
        if state_before is None:
            raise ReviewSchedulingError("active signal has no source ThreadState snapshot")
        job = _job(event, state_before, signal_id)
        self._seen_signal_ids.add(signal_id)
        if state_before.active_turn_id != job.target_turn_id:
            transitions.append(job.discarded_event(event, "target_not_active"))
            return tuple(transitions)
        self._jobs[job.job_id] = job
        self._job_by_signal[signal_id] = job.job_id
        transitions.append(job.queued_event(event))
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
        for record in records:
            event = record.event
            identity = event.identity
            if identity is None or identity.thread_id is None:
                continue
            thread_id = identity.thread_id
            try:
                state_before = replay.snapshot(thread_id)
            except ThreadStateError:
                state_before = None
            state_after = replay.observe(event)
            snapshots[(thread_id, state_after.version)] = state_after
            if event.event_id is not None:
                missing.pop(event.event_id, None)
            candidate_state = state_before
            if event.kind == "signal_candidate":
                version = event.payload.get("state_version")
                if isinstance(version, int) and not isinstance(version, bool):
                    candidate_state = snapshots.get((thread_id, version))
            for transition in self.update(event, candidate_state, state_after):
                assert transition.event_id is not None
                missing[transition.event_id] = transition
        return tuple(missing.values())

    def _discard(self, job: ReviewerJob, trigger: TraceEvent, reason: str) -> TraceEvent:
        started = job.job_id in self._started_job_ids
        self._remove(job.job_id)
        return job.stale_event(trigger, reason) if started else job.discarded_event(trigger, reason)

    def _remove(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        self._started_job_ids.discard(job_id)
        if job is not None:
            self._job_by_signal.pop(job.signal_id, None)


def _job(event: TraceEvent, snapshot: ThreadState, signal_id: str) -> ReviewerJob:
    identity = event.identity
    if identity is None or identity.thread_id is None or identity.turn_id is None:
        raise ReviewSchedulingError("active signal has no exact thread/turn identity")
    thread_id = identity.thread_id
    turn_id = identity.turn_id
    signal_type = _required_string(event.payload, "signal_type")
    candidate_event_id = event.event_id
    if candidate_event_id is None:
        raise ReviewSchedulingError("active signal has no durable event identity")
    state_version = event.payload.get("state_version")
    target_epoch = event.payload.get("target_connection_epoch")
    if not isinstance(state_version, int) or isinstance(state_version, bool):
        raise ReviewSchedulingError("active signal has no integer state_version")
    if target_epoch is not None and (
        not isinstance(target_epoch, int) or isinstance(target_epoch, bool)
    ):
        raise ReviewSchedulingError("active signal has an invalid connection epoch")
    if (
        event.payload.get("thread_id") != thread_id.value
        or event.payload.get("turn_id") != turn_id.value
        or snapshot.thread_id != thread_id
        or snapshot.version != state_version
        or snapshot.identity.turn_id != turn_id
        or snapshot.connection_epoch != target_epoch
    ):
        raise ReviewSchedulingError("active signal target does not match its ThreadState snapshot")
    job_id = hashlib.sha256(f"{signal_id}:{candidate_event_id}".encode()).hexdigest()
    return ReviewerJob(
        job_id,
        signal_id,
        signal_type,
        thread_id,
        turn_id,
        target_epoch,
        state_version,
        candidate_event_id,
        event.occurred_at,
        snapshot,
        build_reviewer_input(event.payload, snapshot),
    )


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ReviewSchedulingError(f"signal candidate has no {key}")
    return value
