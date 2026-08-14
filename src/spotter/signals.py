"""Cheap incremental candidates derived from runtime-neutral Trace IR."""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from spotter.identity import RuntimeIdentity, ThreadId
from spotter.outcomes import outcome_failure
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent, TraceProvenance

_OUTCOME_KINDS = {"command_result", "tool_result", "file_edit", "test_result"}
_TERMINAL_KINDS = {
    "observation_gap",
    "runtime_attachment_unavailable",
    "thread_archived",
    "thread_closed",
    "thread_deleted",
    "turn_completed",
}
_EVIDENCE_LIMIT = 20


class SignalType(StrEnum):
    FAILURE_STREAK = "failure_streak"


class SignalStatus(StrEnum):
    ACTIVE = "active"
    COOLED_DOWN = "cooled_down"
    RESOLVED = "resolved"
    STALE = "stale"


@dataclass(frozen=True)
class SignalCandidate:
    signal_id: str
    signal_type: SignalType
    thread_id: ThreadId
    turn_id: str | None
    connection_epoch: int | None
    state_version: int
    first_seen_at: float | None
    last_seen_at: float | None
    severity_hint: int
    evidence_event_ids: tuple[str, ...]
    involved_resources: tuple[str, ...]
    status: SignalStatus
    source_event_id: str

    def to_trace_event(self, trigger: TraceEvent) -> TraceEvent:
        suffix = hashlib.sha256(f"{self.status}:{self.source_event_id}".encode()).hexdigest()[:16]
        return TraceEvent(
            (
                "signal_candidate_suppressed"
                if self.status == SignalStatus.COOLED_DOWN
                else "signal_candidate"
            ),
            {
                "signal_id": self.signal_id,
                "signal_type": self.signal_type,
                "thread_id": self.thread_id.value,
                "turn_id": self.turn_id,
                "target_connection_epoch": self.connection_epoch,
                "state_version": self.state_version,
                "first_seen_at": self.first_seen_at,
                "last_seen_at": self.last_seen_at,
                "severity_hint": self.severity_hint,
                "evidence_event_ids": list(self.evidence_event_ids),
                "involved_resources": list(self.involved_resources),
                "features": {"consecutive_failures": self.severity_hint},
                "status": self.status,
                "source_event_id": self.source_event_id,
            },
            event_id=f"spotter:signal:{self.signal_id}:{suffix}",
            occurred_at=trigger.occurred_at,
            identity=trigger.identity,
            provenance=TraceProvenance("spotterd", "signal_engine"),
            connection_epoch=trigger.connection_epoch,
        )


@dataclass(frozen=True)
class _FailureStreak:
    signal_id: str
    key: str
    identity: RuntimeIdentity
    connection_epoch: int | None
    first_seen_at: float | None
    last_seen_at: float | None
    count: int
    evidence_event_ids: tuple[str, ...]
    resources: tuple[str, ...]
    emitted: bool = False


class SignalEngine:
    """Incremental signal state; candidates are evidence, never semantic verdicts."""

    def __init__(self, *, failure_threshold: int = 2) -> None:
        if failure_threshold < 2:
            raise ValueError("failure signal threshold must be >= 2")
        self.failure_threshold = failure_threshold
        self._failure_streaks: dict[ThreadId, _FailureStreak] = {}
        self._seen_event_ids: dict[ThreadId, set[str]] = {}

    def update(self, event: TraceEvent, state_version: int) -> tuple[SignalCandidate, ...]:
        identity = event.identity
        if identity is None or identity.thread_id is None or event.event_id is None:
            return ()
        thread_id = identity.thread_id
        seen = self._seen_event_ids.setdefault(thread_id, set())
        if event.event_id in seen:
            return ()
        seen.add(event.event_id)

        candidates: list[SignalCandidate] = []
        streak = self._failure_streaks.get(thread_id)
        if streak is not None and _target_changed(streak, event):
            candidates.extend(self._finish(streak, SignalStatus.STALE, event, state_version))
            self._failure_streaks.pop(thread_id, None)
            streak = None
        if event.kind in _TERMINAL_KINDS:
            if streak is not None:
                candidates.extend(self._finish(streak, SignalStatus.STALE, event, state_version))
                self._failure_streaks.pop(thread_id, None)
            return tuple(candidates)
        if event.kind not in _OUTCOME_KINDS:
            return tuple(candidates)

        key, resources = _equivalence(event)
        failure = outcome_failure(event.payload)
        if key is None or failure is None:
            if streak is not None:
                candidates.extend(self._finish(streak, SignalStatus.STALE, event, state_version))
                self._failure_streaks.pop(thread_id, None)
            return tuple(candidates)
        if failure is False:
            if streak is not None:
                candidates.extend(self._finish(streak, SignalStatus.RESOLVED, event, state_version))
                self._failure_streaks.pop(thread_id, None)
            return tuple(candidates)
        if streak is not None and streak.key != key:
            candidates.extend(self._finish(streak, SignalStatus.STALE, event, state_version))
            streak = None

        if streak is None:
            streak = _FailureStreak(
                _signal_id(event, key),
                key,
                identity,
                event.connection_epoch,
                event.occurred_at,
                event.occurred_at,
                1,
                (event.event_id,),
                resources,
            )
        else:
            streak = replace(
                streak,
                last_seen_at=event.occurred_at,
                count=streak.count + 1,
                evidence_event_ids=(streak.evidence_event_ids + (event.event_id,))[
                    -_EVIDENCE_LIMIT:
                ],
            )
        self._failure_streaks[thread_id] = streak
        if streak.count >= self.failure_threshold:
            status = SignalStatus.COOLED_DOWN if streak.emitted else SignalStatus.ACTIVE
            candidates.append(_candidate(streak, status, event.event_id, state_version))
            if not streak.emitted:
                self._failure_streaks[thread_id] = replace(streak, emitted=True)
        return tuple(candidates)

    def hydrate(
        self, records: Iterable[StepRecord]
    ) -> tuple[tuple[SignalCandidate, TraceEvent], ...]:
        """Rebuild state and return derived events missing after an interrupted append."""

        self._failure_streaks.clear()
        self._seen_event_ids.clear()
        versions: dict[ThreadId, int] = {}
        pending: dict[str, tuple[SignalCandidate, TraceEvent]] = {}
        for record in records:
            event = record.event
            identity = event.identity
            if identity is None or identity.thread_id is None:
                continue
            thread_id = identity.thread_id
            versions[thread_id] = versions.get(thread_id, 0) + 1
            if event.kind in {"signal_candidate", "signal_candidate_suppressed"}:
                if event.event_id is not None:
                    pending.pop(event.event_id, None)
                continue
            for candidate in self.update(event, versions[thread_id]):
                derived = candidate.to_trace_event(event)
                assert derived.event_id is not None
                pending[derived.event_id] = (candidate, event)
        return tuple(pending.values())

    def _finish(
        self,
        streak: _FailureStreak,
        status: SignalStatus,
        event: TraceEvent,
        state_version: int,
    ) -> tuple[SignalCandidate, ...]:
        if not streak.emitted or event.event_id is None:
            return ()
        return (_candidate(streak, status, event.event_id, state_version),)


def _candidate(
    streak: _FailureStreak,
    status: SignalStatus,
    source_event_id: str,
    state_version: int,
) -> SignalCandidate:
    turn_id = streak.identity.turn_id.value if streak.identity.turn_id is not None else None
    assert streak.identity.thread_id is not None
    return SignalCandidate(
        streak.signal_id,
        SignalType.FAILURE_STREAK,
        streak.identity.thread_id,
        turn_id,
        streak.connection_epoch,
        state_version,
        streak.first_seen_at,
        streak.last_seen_at,
        streak.count,
        streak.evidence_event_ids,
        streak.resources,
        status,
        source_event_id,
    )


def _target_changed(streak: _FailureStreak, event: TraceEvent) -> bool:
    identity = event.identity
    return bool(
        identity is not None
        and (
            identity.turn_id != streak.identity.turn_id
            or (
                event.connection_epoch is not None
                and streak.connection_epoch is not None
                and event.connection_epoch != streak.connection_epoch
            )
        )
    )


def _equivalence(event: TraceEvent) -> tuple[str | None, tuple[str, ...]]:
    resources = _resources(event.payload)
    if event.kind == "test_result" and not resources:
        resources = ("validation",)
    if not resources:
        return None, ()
    family = {
        "command_result": "command",
        "file_edit": "file_change",
        "test_result": "test",
        "tool_result": "tool",
    }[event.kind]
    return f"{family}:{'|'.join(resources)}", resources


def _resources(payload: Mapping[str, object]) -> tuple[str, ...]:
    resources: set[str] = set()
    files = payload.get("files")
    if isinstance(files, list):
        resources.update(f"file:{value}" for value in files if isinstance(value, str) and value)
    resource = payload.get("resource") or payload.get("scope")
    if isinstance(resource, str) and resource:
        resources.add(f"resource:{resource}")
    server = payload.get("server") or payload.get("namespace")
    tool = payload.get("tool")
    if isinstance(server, str) and server and isinstance(tool, str) and tool:
        resources.add(f"tool:{server}/{tool}")
    return tuple(sorted(resources))


def _signal_id(event: TraceEvent, key: str) -> str:
    assert event.identity is not None and event.identity.thread_id is not None
    turn_id = event.identity.turn_id.value if event.identity.turn_id is not None else "unknown"
    return hashlib.sha256(
        (
            f"{event.identity.thread_id.value}:{turn_id}:{event.connection_epoch}:"
            f"{SignalType.FAILURE_STREAK}:{key}:{event.event_id}"
        ).encode()
    ).hexdigest()
