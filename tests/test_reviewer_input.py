from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.review_scheduler import ReviewScheduler
from spotter.signals import SignalCandidate, SignalEngine
from spotter.thread_state import ThreadStateStore
from spotter.trace import TraceEvent


def _event(index: int, kind: str, payload: dict[str, object]) -> TraceEvent:
    return TraceEvent(
        kind,
        payload,
        event_id=f"event-{index}",
        occurred_at=float(index),
        identity=RuntimeIdentity(
            ThreadId("thread-1"),
            TurnId("turn-1"),
            None,
            IdentityProvenance("codex", "external-thread", "turn-1"),
        ),
        connection_epoch=1,
    )


def test_signal_job_builds_a_bounded_snapshot_input() -> None:
    events = [_event(0, "turn_started", {})]
    events.append(_event(1, "user_prompt", {"prompt": "goal " * 200}))
    events.extend(
        _event(index + 2, "constraint", {"text": f"constraint {index}"}) for index in range(25)
    )
    events.extend(
        _event(index + 27, "diff_updated", {"files": [f"src/file-{index}.py"]})
        for index in range(60)
    )
    events.extend(
        _event(
            index + 87,
            "observation_gap",
            {
                "epoch_before": 1,
                "epoch_after": 1,
                "backfill_status": "none",
            },
        )
        for index in range(12)
    )
    events.extend(
        [
            _event(
                99,
                "tool_result",
                {"status": "failed", "server": "fixture", "tool": "lookup"},
            ),
            _event(
                100,
                "tool_result",
                {"status": "failed", "server": "fixture", "tool": "lookup"},
            ),
        ]
    )
    states = ThreadStateStore()
    signals = SignalEngine()
    candidates: tuple[SignalCandidate, ...] = ()
    for event in events:
        state = states.observe(event)
        candidates = signals.update(event, state.version)
    assert len(candidates) == 1
    snapshot = states.snapshot(ThreadId("thread-1"))
    candidate = candidates[0].to_trace_event(events[-1])
    state_after = states.observe(candidate)
    scheduler = ReviewScheduler()

    queued = scheduler.update(candidate, snapshot, state_after)
    reviewer_input = scheduler.pending()[0].reviewer_input

    assert reviewer_input.signal_type == "failure_streak"
    assert reviewer_input.evidence_event_ids == ("event-99", "event-100")
    assert reviewer_input.involved_resources == ("tool:fixture/lookup",)
    assert reviewer_input.features == (("consecutive_failures", 2),)
    assert reviewer_input.goal is not None and len(reviewer_input.goal) == 600
    assert len(reviewer_input.constraints) == 20
    assert len(reviewer_input.touched_files) == 50
    assert len(reviewer_input.coverage_gaps) == 10
    assert len(reviewer_input.recent_failures) == 2
    assert {"constraints", "coverage_gaps", "goal", "touched_files"}.issubset(
        reviewer_input.truncated_fields
    )
    assert queued[0].payload["input_coverage"] == reviewer_input.coverage()
