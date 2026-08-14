from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.review_scheduler import ReviewScheduler
from spotter.signals import SignalEngine
from spotter.snapshot import StepRecord
from spotter.thread_state import ThreadStateStore
from spotter.trace import TraceEvent, TraceProvenance


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        ThreadId("thread-1"),
        TurnId("turn-1"),
        None,
        IdentityProvenance("codex", "external-thread", "turn-1"),
    )


def _event(event_id: str, kind: str, payload: dict[str, object]) -> TraceEvent:
    return TraceEvent(
        kind,
        payload,
        event_id=event_id,
        occurred_at=float(len(event_id)),
        identity=_identity(),
        provenance=TraceProvenance("codex_app_server", "fixture"),
        connection_epoch=1,
    )


def _active_candidate() -> tuple[list[TraceEvent], TraceEvent]:
    turn = _event("turn-start", "turn_started", {})
    first = _event(
        "failure-1", "tool_result", {"status": "failed", "server": "fixture", "tool": "lookup"}
    )
    second = _event(
        "failure-2", "tool_result", {"status": "failed", "server": "fixture", "tool": "lookup"}
    )
    engine = SignalEngine()
    assert engine.update(turn, 1) == ()
    assert engine.update(first, 2) == ()
    candidate = engine.update(second, 3)[0].to_trace_event(second)
    return [turn, first, second], candidate


def test_active_candidate_queues_one_immutable_snapshot() -> None:
    source_events, candidate = _active_candidate()
    states = ThreadStateStore()
    states.replay(source_events)
    snapshot = states.snapshot(ThreadId("thread-1"))
    state_after = states.observe(candidate)
    scheduler = ReviewScheduler()

    queued = scheduler.update(candidate, snapshot, state_after)
    duplicate = scheduler.update(candidate, snapshot, state_after)

    assert len(queued) == 1
    assert queued[0].kind == "review_job_queued"
    assert duplicate == ()
    assert len(scheduler.pending()) == 1
    assert scheduler.pending()[0].snapshot is snapshot
    assert scheduler.pending()[0].state_version == 3


def test_target_change_discards_pending_job() -> None:
    source_events, candidate = _active_candidate()
    states = ThreadStateStore()
    states.replay(source_events)
    snapshot = states.snapshot(ThreadId("thread-1"))
    state_after = states.observe(candidate)
    scheduler = ReviewScheduler()
    scheduler.update(candidate, snapshot, state_after)
    completed = _event("turn-done", "turn_completed", {"status": "completed"})
    before_completed = states.snapshot(ThreadId("thread-1"))
    after_completed = states.observe(completed)

    discarded = scheduler.update(completed, before_completed, after_completed)

    assert len(discarded) == 1
    assert discarded[0].kind == "review_job_discarded"
    assert discarded[0].payload["reason"] == "target_changed"
    assert scheduler.pending() == ()
    assert scheduler.update(candidate, snapshot, state_after) == ()


def test_candidate_without_a_proven_active_turn_is_discarded() -> None:
    source_events, _ = _active_candidate()
    engine = SignalEngine()
    assert engine.update(source_events[1], 1) == ()
    candidate = engine.update(source_events[2], 2)[0].to_trace_event(source_events[2])
    states = ThreadStateStore()
    states.replay(source_events[1:])
    snapshot = states.snapshot(ThreadId("thread-1"))
    state_after = states.observe(candidate)
    scheduler = ReviewScheduler()

    discarded = scheduler.update(candidate, snapshot, state_after)

    assert len(discarded) == 1
    assert discarded[0].kind == "review_job_discarded"
    assert discarded[0].payload["reason"] == "target_not_active"
    assert scheduler.pending() == ()


def test_hydration_backfills_only_missing_queue_transition() -> None:
    source_events, candidate = _active_candidate()
    records = [
        StepRecord(index, event, None) for index, event in enumerate([*source_events, candidate])
    ]
    scheduler = ReviewScheduler()

    missing = scheduler.hydrate(records)
    queued = missing[0]
    recovered = ReviewScheduler()
    complete = recovered.hydrate([*records, StepRecord(len(records), queued, None)])

    assert len(missing) == 1
    assert missing[0].kind == "review_job_queued"
    assert complete == ()
    assert len(recovered.pending()) == 1
    assert recovered.pending()[0].snapshot.version == 3
    assert recovered.pending()[0].reviewer_input == scheduler.pending()[0].reviewer_input


def test_hydration_recovers_running_job_as_stale_not_discarded() -> None:
    source_events, candidate = _active_candidate()
    scheduler = ReviewScheduler()
    initial = [
        StepRecord(index, event, None) for index, event in enumerate([*source_events, candidate])
    ]
    queued = scheduler.hydrate(initial)[0]
    job_id = queued.payload["review_job_id"]
    assert isinstance(job_id, str)
    started = _event(
        "review-started",
        "review_inference_started",
        {"review_job_id": job_id},
    )
    completed = _event("turn-done", "turn_completed", {"status": "completed"})
    records = [
        *initial,
        StepRecord(4, queued, None),
        StepRecord(5, started, None),
        StepRecord(6, completed, None),
    ]
    recovered = ReviewScheduler()

    missing = recovered.hydrate(records)

    assert len(missing) == 1
    assert missing[0].kind == "review_job_stale"
    decision = _event(
        "review-decision",
        "reviewer_decision",
        {"review_job_id": job_id, "stale": True},
    )
    complete = ReviewScheduler().hydrate(
        [
            *records,
            StepRecord(7, missing[0], None),
            StepRecord(8, decision, None),
        ]
    )
    assert complete == ()
