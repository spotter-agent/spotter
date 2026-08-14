from pathlib import Path

from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.runtime_connection import AppServerRecoveryLoop
from spotter.signals import SignalEngine, SignalStatus, SignalType
from spotter.snapshot import StepRecord
from spotter.thread_state import ThreadStateStore
from spotter.trace import TraceEvent, TraceProvenance


def _event(
    event_id: str,
    status: str,
    *,
    turn: str = "turn-1",
    epoch: int = 1,
    resource: bool = True,
) -> TraceEvent:
    payload: dict[str, object] = {"status": status}
    if resource:
        payload.update({"server": "fixture", "tool": "lookup"})
    return TraceEvent(
        "tool_result",
        payload,
        event_id=event_id,
        occurred_at=float(event_id.removeprefix("event-")),
        identity=RuntimeIdentity(
            ThreadId("thread-1"),
            TurnId(turn),
            None,
            IdentityProvenance("codex", "external-thread", turn),
        ),
        provenance=TraceProvenance("codex_app_server", "item/completed"),
        connection_epoch=epoch,
    )


def test_failure_streak_emits_once_then_cools_down_until_success() -> None:
    engine = SignalEngine(failure_threshold=2)
    first = _event("event-1", "failed")
    second = _event("event-2", "failed")
    third = _event("event-3", "failed")
    success = _event("event-4", "completed")

    assert engine.update(first, 1) == ()
    active = engine.update(second, 2)[0]
    suppressed = engine.update(third, 3)[0]
    resolved = engine.update(success, 4)[0]

    assert active.signal_type == SignalType.FAILURE_STREAK
    assert active.status == SignalStatus.ACTIVE
    assert active.severity_hint == 2
    assert active.evidence_event_ids == ("event-1", "event-2")
    assert active.involved_resources == ("tool:fixture/lookup",)
    assert suppressed.signal_id == active.signal_id
    assert suppressed.status == SignalStatus.COOLED_DOWN
    assert suppressed.severity_hint == 3
    assert resolved.signal_id == active.signal_id
    assert resolved.status == SignalStatus.RESOLVED
    assert resolved.source_event_id == "event-4"

    trace = active.to_trace_event(second)
    assert trace.kind == "signal_candidate"
    assert trace.payload["status"] == "active"
    assert trace.payload["state_version"] == 2
    assert trace.payload["features"] == {"consecutive_failures": 2}


def test_duplicate_event_does_not_rearm_or_inflate_streak() -> None:
    engine = SignalEngine()
    first = _event("event-1", "failed")

    assert engine.update(first, 1) == ()
    assert engine.update(first, 2) == ()
    candidate = engine.update(_event("event-2", "failed"), 3)[0]

    assert candidate.severity_hint == 2
    assert candidate.evidence_event_ids == ("event-1", "event-2")


def test_unknown_resource_and_shell_probe_remain_unknown() -> None:
    engine = SignalEngine()
    identity = _event("event-1", "failed").identity
    grep = TraceEvent(
        "command_result",
        {"command": "rg missing src", "exitCode": 1},
        event_id="grep-1",
        identity=identity,
        connection_epoch=1,
    )
    repeated = TraceEvent(
        "command_result",
        {"command": "rg missing src", "exitCode": 1},
        event_id="grep-2",
        identity=identity,
        connection_epoch=1,
    )

    assert engine.update(grep, 1) == ()
    assert engine.update(repeated, 2) == ()


def test_turn_boundary_stales_an_active_candidate() -> None:
    engine = SignalEngine()
    engine.update(_event("event-1", "failed"), 1)
    active = engine.update(_event("event-2", "failed"), 2)[0]
    boundary = TraceEvent(
        "turn_completed",
        {"status": "completed"},
        event_id="turn-done",
        identity=_event("event-3", "completed").identity,
        connection_epoch=1,
    )

    stale = engine.update(boundary, 3)[0]

    assert stale.signal_id == active.signal_id
    assert stale.status == SignalStatus.STALE


def test_hydration_restores_cooldown_without_duplicate_candidate() -> None:
    first = _event("event-1", "failed")
    second = _event("event-2", "failed")
    original = SignalEngine()
    original.update(first, 1)
    active = original.update(second, 2)[0]
    candidate_event = active.to_trace_event(second)

    recovered = SignalEngine()
    missing = recovered.hydrate(
        [
            StepRecord(0, first, None),
            StepRecord(1, second, None),
            StepRecord(2, candidate_event, None),
        ]
    )
    next_update = recovered.update(_event("event-3", "failed"), 4)[0]

    assert missing == ()
    assert next_update.signal_id == active.signal_id
    assert next_update.status == SignalStatus.COOLED_DOWN
    assert next_update.severity_hint == 3


def test_runtime_journals_signal_candidates_and_recovers_cooldown(tmp_path: Path) -> None:
    journals = tmp_path / "sessions"
    first_store = ThreadStateStore()
    runtime = AppServerRecoveryLoop("ws://unused", journals, first_store)

    runtime._record(
        TraceEvent(
            "turn_started",
            {},
            event_id="turn-start",
            identity=_event("event-1", "failed").identity,
            connection_epoch=1,
        )
    )
    runtime._record(_event("event-1", "failed"))
    runtime._record(_event("event-2", "failed"))

    records = runtime.ingestor.records()
    candidate = next(record.event for record in records if record.event.kind == "signal_candidate")
    assert candidate.payload["status"] == "active"
    assert first_store.snapshot(ThreadId("thread-1")).version == 5
    queued = [record.event for record in records if record.event.kind == "review_job_queued"]
    assert len(queued) == 1
    assert queued[0].payload["signal_id"] == candidate.payload["signal_id"]

    recovered_store = ThreadStateStore()
    recovered = AppServerRecoveryLoop("ws://unused", journals, recovered_store)
    recovered._record(_event("event-3", "failed"))

    records = recovered.ingestor.records()
    suppressed = [
        record.event for record in records if record.event.kind == "signal_candidate_suppressed"
    ]
    assert len(suppressed) == 1
    assert suppressed[0].payload["status"] == "cooled_down"


def test_runtime_backfills_candidate_after_interrupted_derived_append(tmp_path: Path) -> None:
    journals = tmp_path / "sessions"
    interrupted = AppServerRecoveryLoop("ws://unused", journals, ThreadStateStore())
    interrupted.ingestor.record(
        TraceEvent(
            "turn_started",
            {},
            event_id="turn-start",
            identity=_event("event-1", "failed").identity,
            connection_epoch=1,
        )
    )
    interrupted.ingestor.record(_event("event-1", "failed"))
    interrupted.ingestor.record(_event("event-2", "failed"))

    recovered = AppServerRecoveryLoop("ws://unused", journals, ThreadStateStore())

    candidates = [
        record.event
        for record in recovered.ingestor.records()
        if record.event.kind == "signal_candidate"
    ]
    assert len(candidates) == 1
    assert candidates[0].payload["status"] == "active"
    assert candidates[0].payload["state_version"] == 3
    queued = [
        record.event
        for record in recovered.ingestor.records()
        if record.event.kind == "review_job_queued"
    ]
    assert len(queued) == 1
