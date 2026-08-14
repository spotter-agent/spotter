from pathlib import Path

from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.review_scheduler import ReviewerJob, ReviewScheduler
from spotter.runtime_connection import AppServerRecoveryLoop
from spotter.signals import (
    SignalCandidate,
    SignalEngine,
    SignalStatus,
    SignalType,
    deterministic_block_equivalence,
)
from spotter.snapshot import StepRecord
from spotter.thread_state import ThreadState, ThreadStateStore
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


def _tool_call(
    event_id: str,
    arguments: object,
    *,
    tool: str = "read",
    result: object = "same evidence",
) -> TraceEvent:
    event = _event(event_id, "completed")
    return TraceEvent(
        "tool_result",
        {
            "status": "completed",
            "server": "fixture",
            "tool": tool,
            "arguments": arguments,
            "result": result,
        },
        event_id=event.event_id,
        occurred_at=event.occurred_at,
        identity=event.identity,
        provenance=event.provenance,
        connection_epoch=event.connection_epoch,
    )


def _blocked_action(event_id: str, key: str, resources: list[str] | None = None) -> TraceEvent:
    event = _event(event_id, "completed")
    return TraceEvent(
        "deterministic_gate_block",
        {
            "equivalence_key": key,
            "involved_resources": resources or ["gate-rule:git_reset_hard"],
        },
        event_id=event.event_id,
        occurred_at=event.occurred_at,
        identity=event.identity,
        connection_epoch=event.connection_epoch,
    )


def _trace(kind: str, event_id: str, payload: dict[str, object]) -> TraceEvent:
    event = _event(event_id, "completed")
    return TraceEvent(
        kind,
        payload,
        event_id=event.event_id,
        occurred_at=event.occurred_at,
        identity=event.identity,
        connection_epoch=event.connection_epoch,
    )


def _observe(
    engine: SignalEngine, store: ThreadStateStore, event: TraceEvent
) -> tuple[SignalCandidate, ...]:
    state = store.observe(event)
    return engine.update(event, state.version, state)


def test_failure_streak_emits_once_then_cools_down_until_success() -> None:
    engine = SignalEngine(failure_threshold=2)
    first = _event("event-1", "failed")
    second = _event("event-2", "failed")
    third = _event("event-3", "failed")
    success = _event("event-4", "completed")

    assert engine.update(first, 1) == ()
    active = engine.update(second, 2)[0]
    suppressed = next(
        candidate
        for candidate in engine.update(third, 3)
        if candidate.signal_type == SignalType.FAILURE_STREAK
    )
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


def test_repeated_equivalent_tool_calls_emit_once_then_cool_down() -> None:
    engine = SignalEngine(repeated_tool_threshold=3)
    first = _tool_call("event-1", {"path": "src/example.py", "line": 1})
    second = _tool_call("event-2", {"line": 1, "path": "src/example.py"})
    third = _tool_call("event-3", {"path": "src/example.py", "line": 1})
    fourth = _tool_call("event-4", {"path": "src/example.py", "line": 1})

    assert engine.update(first, 1) == ()
    assert engine.update(second, 2) == ()
    active = engine.update(third, 3)[0]
    suppressed = engine.update(fourth, 4)[0]

    assert active.signal_type == SignalType.REPEATED_EQUIVALENT_TOOL_CALL
    assert active.status == SignalStatus.ACTIVE
    assert active.severity_hint == 3
    assert active.evidence_event_ids == ("event-1", "event-2", "event-3")
    assert active.involved_resources == ("tool:fixture/read",)
    assert suppressed.signal_id == active.signal_id
    assert suppressed.status == SignalStatus.COOLED_DOWN
    assert suppressed.severity_hint == 4
    assert active.to_trace_event(third).payload["features"] == {"consecutive_equivalent_calls": 3}


def test_recurrence_after_deterministic_block_emits_once_and_hydrates_cooldown() -> None:
    first = _blocked_action("event-1", "git_reset_hard:gate-rule:git_reset_hard")
    second = _blocked_action("event-2", "git_reset_hard:gate-rule:git_reset_hard")
    third = _blocked_action("event-3", "git_reset_hard:gate-rule:git_reset_hard")
    engine = SignalEngine()

    assert engine.update(first, 1) == ()
    active = engine.update(second, 2)[0]

    assert active.signal_type == SignalType.RECURRENCE_AFTER_DETERMINISTIC_BLOCK
    assert active.status == SignalStatus.ACTIVE
    assert active.evidence_event_ids == ("event-1", "event-2")
    assert active.to_trace_event(second).payload["features"] == {"equivalent_blocked_attempts": 2}

    recovered = SignalEngine()
    assert (
        recovered.hydrate(
            [
                StepRecord(0, first, None),
                StepRecord(1, second, None),
                StepRecord(2, active.to_trace_event(second), None),
            ]
        )
        == ()
    )
    cooled = recovered.update(third, 4)[0]
    assert cooled.signal_id == active.signal_id
    assert cooled.status == SignalStatus.COOLED_DOWN
    assert cooled.severity_hint == 3


def test_block_recurrence_does_not_guess_across_resources_or_unknown_shapes() -> None:
    engine = SignalEngine()
    first = _blocked_action("event-1", "forbidden_path:file:a", ["file:a"])
    different = _blocked_action("event-2", "forbidden_path:file:b", ["file:b"])
    unknown = _blocked_action("event-3", "", [])

    assert engine.update(first, 1) == ()
    assert engine.update(different, 2) == ()
    assert engine.update(unknown, 3) == ()
    assert deterministic_block_equivalence(
        "git_reset_hard", {"command": "git reset --hard"}
    ) == deterministic_block_equivalence(
        "git_reset_hard", {"command": "bash -c 'git reset --hard'"}
    )


def test_stale_hypothesis_reuse_requires_explicit_causal_action_link() -> None:
    engine = SignalEngine()
    store = ThreadStateStore()
    turn = _trace("turn_started", "event-1", {})
    evidence = _trace(
        "command_result",
        "event-2",
        {"status": "completed", "resource": "config-check"},
    )
    hypothesis = _trace(
        "hypothesis",
        "event-3",
        {"text": "The config is loaded", "evidence_ids": ["event-2"]},
    )
    action_while_fresh = _trace(
        "command_started",
        "event-4",
        {"hypothesis_ids": ["event-3"], "resource": "src/config.py"},
    )
    invalidated = _trace("evidence_invalidated", "event-5", {"evidence_id": "event-2"})
    unlinked = _trace("command_started", "event-6", {"resource": "src/config.py"})
    linked = _trace(
        "command_started",
        "event-7",
        {"hypothesis_ids": ["event-3"], "resource": "src/config.py"},
    )
    linked_again = _trace(
        "tool_started",
        "event-8",
        {
            "hypothesis_ids": ["event-3"],
            "server": "fixture",
            "tool": "rewrite",
        },
    )

    for event in (turn, evidence, hypothesis, action_while_fresh, invalidated, unlinked):
        assert _observe(engine, store, event) == ()
    active = _observe(engine, store, linked)[0]
    snapshot = store.snapshot(ThreadId("thread-1"))
    cooled = _observe(engine, store, linked_again)[0]

    assert active.signal_type == SignalType.STALE_HYPOTHESIS_REUSE
    assert active.status == SignalStatus.ACTIVE
    assert active.evidence_event_ids == ("event-7",)
    assert active.involved_resources == (
        "hypothesis:event-3",
        "resource:src/config.py",
    )
    assert active.to_trace_event(linked).payload["features"] == {"causally_linked_actions": 1}
    assert cooled.signal_id == active.signal_id
    assert cooled.status == SignalStatus.COOLED_DOWN
    assert cooled.severity_hint == 2
    assert cooled.involved_resources == (
        "hypothesis:event-3",
        "resource:src/config.py",
        "tool:fixture/rewrite",
    )

    candidate_event = active.to_trace_event(linked)
    state_after = store.observe(candidate_event)
    scheduler = ReviewScheduler()
    assert scheduler.update(candidate_event, snapshot, state_after)
    assert scheduler.pending()[0].reviewer_input.stale_hypotheses == (
        "event-3: The config is loaded",
    )


def test_transitively_stale_hypothesis_reuse_is_detected_without_guessing_unknown_ids() -> None:
    engine = SignalEngine()
    store = ThreadStateStore()
    evidence = _trace("command_result", "event-1", {"status": "completed", "resource": "proof"})
    first = _trace(
        "hypothesis",
        "event-2",
        {"text": "First claim", "evidence_ids": ["event-1"]},
    )
    invalidated = _trace("evidence_invalidated", "event-3", {"evidence_id": "event-1"})
    dependent = _trace(
        "hypothesis",
        "event-4",
        {"text": "Dependent claim", "depends_on": ["event-2"]},
    )
    oversized = _trace("command_started", "event-5", {"hypothesis_ids": ["event-4"] * 129})
    unknown = _trace("command_started", "event-6", {"hypothesis_ids": ["missing"]})
    linked = _trace("command_started", "event-7", {"hypothesis_ids": ["event-4"]})

    for event in (evidence, first, invalidated, dependent, oversized, unknown):
        assert _observe(engine, store, event) == ()
    candidate = _observe(engine, store, linked)[0]

    assert candidate.signal_type == SignalType.STALE_HYPOTHESIS_REUSE
    assert candidate.involved_resources == ("hypothesis:event-4",)


def test_stale_hypothesis_reuse_hydrates_cooldown() -> None:
    evidence = _trace("command_result", "event-1", {"status": "completed", "resource": "proof"})
    hypothesis = _trace(
        "hypothesis",
        "event-2",
        {"text": "Old claim", "evidence_ids": ["event-1"]},
    )
    invalidated = _trace("evidence_invalidated", "event-3", {"evidence_id": "event-1"})
    first_action = _trace("command_started", "event-4", {"hypothesis_ids": ["event-2"]})
    original = SignalEngine()
    store = ThreadStateStore()
    active: SignalCandidate | None = None
    for event in (evidence, hypothesis, invalidated, first_action):
        candidates = _observe(original, store, event)
        if candidates:
            active = candidates[0]
    assert active is not None

    recovered = SignalEngine()
    records = [
        StepRecord(index, event, None)
        for index, event in enumerate(
            (evidence, hypothesis, invalidated, first_action, active.to_trace_event(first_action))
        )
    ]
    assert recovered.hydrate(records) == ()
    next_action = _trace(
        "tool_started",
        "event-5",
        {"hypothesis_ids": ["event-2"], "server": "fixture", "tool": "retry"},
    )
    recovered_state = ThreadStateStore()
    recovered_state.replay(record.event for record in records)
    state = recovered_state.observe(next_action)
    cooled = recovered.update(next_action, state.version, state)[0]

    assert cooled.signal_id == active.signal_id
    assert cooled.status == SignalStatus.COOLED_DOWN


def test_edits_without_validation_emits_for_distinct_successful_files() -> None:
    engine = SignalEngine(unvalidated_edit_threshold=3)

    assert (
        engine.update(
            _trace("file_edit", "event-1", {"status": "completed", "files": ["src/a.py"]}),
            1,
        )
        == ()
    )
    assert (
        engine.update(
            _trace("file_edit", "event-2", {"status": "completed", "files": ["src/b.py"]}),
            2,
        )
        == ()
    )
    active = engine.update(
        _trace("diff_updated", "event-3", {"files": ["src/a.py", "src/c.py"]}),
        3,
    )[0]
    cooled = engine.update(
        _trace("file_edit", "event-4", {"status": "completed", "files": ["src/d.py"]}),
        4,
    )[0]

    assert active.signal_type == SignalType.EDITS_WITHOUT_VALIDATION
    assert active.status == SignalStatus.ACTIVE
    assert active.involved_resources == (
        "file:src/a.py",
        "file:src/b.py",
        "file:src/c.py",
    )
    assert active.evidence_event_ids == ("event-1", "event-2", "event-3")
    assert active.to_trace_event(_trace("diff_updated", "event-3", {})).payload["features"] == {
        "files_without_validation": 3
    }
    assert cooled.signal_id == active.signal_id
    assert cooled.status == SignalStatus.COOLED_DOWN
    assert cooled.severity_hint == 4


def test_scoped_validation_rearms_unrelated_unvalidated_files() -> None:
    engine = SignalEngine(unvalidated_edit_threshold=2)
    engine.update(
        _trace(
            "file_edit",
            "event-1",
            {"status": "completed", "files": ["src/pkg/a.py"]},
        ),
        1,
    )
    active = engine.update(
        _trace(
            "file_edit",
            "event-2",
            {"status": "completed", "files": ["src/other/b.py"]},
        ),
        2,
    )[0]

    resolved = engine.update(
        _trace(
            "test_result",
            "event-3",
            {"status": "passed", "validated_paths": ["src/pkg"]},
        ),
        3,
    )[0]
    rearmed = engine.update(
        _trace(
            "file_edit",
            "event-4",
            {"status": "completed", "files": ["src/other/c.py"]},
        ),
        4,
    )[0]

    assert resolved.signal_id == active.signal_id
    assert resolved.status == SignalStatus.RESOLVED
    assert rearmed.signal_id != active.signal_id
    assert rearmed.status == SignalStatus.ACTIVE
    assert rearmed.involved_resources == (
        "file:src/other/b.py",
        "file:src/other/c.py",
    )
    assert rearmed.evidence_event_ids == ("event-2", "event-4")


def test_unknown_validation_scope_and_failed_edits_do_not_clear_or_invent_evidence() -> None:
    engine = SignalEngine(unvalidated_edit_threshold=2)
    failed = _trace(
        "file_edit",
        "event-1",
        {"status": "failed", "files": ["src/failed-a.py", "src/failed-b.py"]},
    )
    first = _trace("file_edit", "event-2", {"status": "completed", "files": ["src/a.py"]})
    unscoped_pass = _trace("test_result", "event-3", {"status": "passed"})
    second = _trace("file_edit", "event-4", {"status": "completed", "files": ["src/b.py"]})

    assert engine.update(failed, 1) == ()
    assert engine.update(first, 2) == ()
    assert engine.update(unscoped_pass, 3) == ()
    active = engine.update(second, 4)[0]

    assert active.signal_type == SignalType.EDITS_WITHOUT_VALIDATION
    assert active.involved_resources == ("file:src/a.py", "file:src/b.py")
    assert active.evidence_event_ids == ("event-2", "event-4")


def test_touched_scope_growth_emits_for_new_files_outside_explicit_request_scope() -> None:
    engine = SignalEngine(scope_growth_threshold=3, unvalidated_edit_threshold=999)
    prompt = _trace(
        "user_prompt",
        "event-1",
        {"content": [{"type": "text", "text": "Update src/api.py and tests/api.py"}]},
    )

    assert engine.update(prompt, 1) == ()
    assert (
        engine.update(
            _trace("file_edit", "event-2", {"status": "completed", "files": ["src/api.py"]}),
            2,
        )
        == ()
    )
    assert (
        engine.update(
            _trace("file_edit", "event-3", {"status": "completed", "files": ["src/one.py"]}),
            3,
        )
        == ()
    )
    assert (
        engine.update(
            _trace("file_edit", "event-4", {"status": "completed", "files": ["src/two.py"]}),
            4,
        )
        == ()
    )
    active = engine.update(
        _trace("file_edit", "event-5", {"status": "completed", "files": ["src/three.py"]}),
        5,
    )[0]
    cooled = engine.update(
        _trace("file_edit", "event-6", {"status": "completed", "files": ["src/four.py"]}),
        6,
    )[0]

    assert active.signal_type == SignalType.TOUCHED_SCOPE_GROWTH
    assert active.status == SignalStatus.ACTIVE
    assert active.involved_resources == (
        "file:src/one.py",
        "file:src/two.py",
        "file:src/three.py",
    )
    assert active.evidence_event_ids == ("event-3", "event-4", "event-5")
    assert active.to_trace_event(_trace("file_edit", "event-5", {})).payload["features"] == {
        "files_outside_declared_scope": 3
    }
    assert cooled.signal_id == active.signal_id
    assert cooled.status == SignalStatus.COOLED_DOWN


def test_scope_growth_stays_unknown_for_broad_or_unscoped_tasks() -> None:
    engine = SignalEngine(scope_growth_threshold=2, unvalidated_edit_threshold=999)
    broad = _trace("user_prompt", "event-1", {"prompt": "Refactor src/ and tests/"})
    in_scope = _trace(
        "file_edit",
        "event-2",
        {"status": "completed", "files": ["src/a.py", "src/pkg/b.py", "tests/test_a.py"]},
    )
    failed = _trace(
        "file_edit",
        "event-3",
        {"status": "failed", "files": ["outside/a.py", "outside/b.py"]},
    )
    unscoped = _trace("user_prompt", "event-4", {"prompt": "Refactor the project"})
    unknown = _trace(
        "file_edit",
        "event-5",
        {"status": "completed", "files": ["one.py", "two.py", "three.py"]},
    )

    assert engine.update(broad, 1) == ()
    assert engine.update(in_scope, 2) == ()
    assert engine.update(failed, 3) == ()
    assert engine.update(unscoped, 4) == ()
    assert engine.update(unknown, 5) == ()


def test_new_user_scope_stales_active_scope_growth() -> None:
    engine = SignalEngine(scope_growth_threshold=2, unvalidated_edit_threshold=999)
    engine.update(_trace("user_prompt", "event-1", {"prompt": "Update src/api.py"}), 1)
    engine.update(
        _trace("file_edit", "event-2", {"status": "completed", "files": ["src/one.py"]}),
        2,
    )
    active = engine.update(
        _trace("file_edit", "event-3", {"status": "completed", "files": ["src/two.py"]}),
        3,
    )[0]

    stale = engine.update(_trace("user_prompt", "event-4", {"prompt": "Now refactor src/"}), 4)[0]

    assert stale.signal_id == active.signal_id
    assert stale.status == SignalStatus.STALE


def test_file_mutation_rearms_repeated_tool_call_detection() -> None:
    engine = SignalEngine(repeated_tool_threshold=3)
    identity = _event("event-3", "completed").identity
    mutation = TraceEvent(
        "file_edit",
        {"status": "completed", "files": ["src/example.py"]},
        event_id="event-3",
        identity=identity,
        connection_epoch=1,
    )

    assert engine.update(_tool_call("event-1", {"path": "src/example.py"}), 1) == ()
    assert engine.update(_tool_call("event-2", {"path": "src/example.py"}), 2) == ()
    assert engine.update(mutation, 3) == ()
    assert engine.update(_tool_call("event-4", {"path": "src/example.py"}), 4) == ()
    assert engine.update(_tool_call("event-5", {"path": "src/example.py"}), 5) == ()
    candidate = engine.update(_tool_call("event-6", {"path": "src/example.py"}), 6)[0]

    assert candidate.evidence_event_ids == ("event-4", "event-5", "event-6")


def test_repeated_reads_without_frontier_expansion_emit_then_cool_down() -> None:
    engine = SignalEngine(no_frontier_threshold=3)

    call = {"path": "src/example.py", "line": 1}
    assert engine.update(_tool_call("event-1", call), 1) == ()
    assert engine.update(_tool_call("event-2", call), 2) == ()
    assert engine.update(_tool_call("event-3", call), 3)
    active = next(
        candidate
        for candidate in engine.update(_tool_call("event-4", call), 4)
        if candidate.signal_type == SignalType.REPEATED_READ_NO_FRONTIER
    )
    suppressed = next(
        candidate
        for candidate in engine.update(_tool_call("event-5", call), 5)
        if candidate.signal_type == SignalType.REPEATED_READ_NO_FRONTIER
    )

    assert active.signal_type == SignalType.REPEATED_READ_NO_FRONTIER
    assert active.status == SignalStatus.ACTIVE
    assert active.severity_hint == 3
    assert active.evidence_event_ids == ("event-2", "event-3", "event-4")
    assert active.involved_resources == ("file:src/example.py",)
    assert active.to_trace_event(_tool_call("event-4", {})).payload["features"] == {
        "reads_without_frontier_expansion": 3
    }
    assert suppressed.signal_id == active.signal_id
    assert suppressed.status == SignalStatus.COOLED_DOWN


def test_new_read_resource_rearms_no_frontier_detection() -> None:
    engine = SignalEngine(no_frontier_threshold=2)

    assert engine.update(_tool_call("event-1", {"path": "src/one.py"}), 1) == ()
    assert engine.update(_tool_call("event-2", {"path": "src/one.py"}), 2) == ()
    assert engine.update(_tool_call("event-3", {"path": "src/two.py"}), 3) == ()
    assert engine.update(_tool_call("event-4", {"path": "src/two.py"}), 4) == ()
    active = next(
        candidate
        for candidate in engine.update(_tool_call("event-5", {"path": "src/two.py"}), 5)
        if candidate.signal_type == SignalType.REPEATED_READ_NO_FRONTIER
    )

    assert active.evidence_event_ids == ("event-4", "event-5")
    assert active.involved_resources == ("file:src/two.py",)


def test_new_read_evidence_rearms_no_frontier_detection() -> None:
    engine = SignalEngine(no_frontier_threshold=2)

    assert engine.update(_tool_call("event-1", {"path": "src/example.py"}), 1) == ()
    assert engine.update(_tool_call("event-2", {"path": "src/example.py"}), 2) == ()
    assert not any(
        candidate.signal_type == SignalType.REPEATED_READ_NO_FRONTIER
        for candidate in engine.update(
            _tool_call(
                "event-3",
                {"path": "src/example.py"},
                result="new evidence",
            ),
            3,
        )
    )
    assert not any(
        candidate.signal_type == SignalType.REPEATED_READ_NO_FRONTIER
        for candidate in engine.update(
            _tool_call(
                "event-4",
                {"path": "src/example.py"},
                result="new evidence",
            ),
            4,
        )
    )
    active = next(
        candidate
        for candidate in engine.update(
            _tool_call(
                "event-5",
                {"path": "src/example.py"},
                result="new evidence",
            ),
            5,
        )
        if candidate.signal_type == SignalType.REPEATED_READ_NO_FRONTIER
    )

    assert active.evidence_event_ids == ("event-4", "event-5")


def test_new_read_scope_rearms_no_frontier_detection() -> None:
    engine = SignalEngine(no_frontier_threshold=2)

    assert engine.update(_tool_call("event-1", {"path": "src/example.py", "line": 1}), 1) == ()
    assert engine.update(_tool_call("event-2", {"path": "src/example.py", "line": 1}), 2) == ()
    assert engine.update(_tool_call("event-3", {"path": "src/example.py", "line": 2}), 3) == ()
    assert engine.update(_tool_call("event-4", {"path": "src/example.py", "line": 2}), 4) == ()
    active = next(
        candidate
        for candidate in engine.update(
            _tool_call("event-5", {"path": "src/example.py", "line": 2}), 5
        )
        if candidate.signal_type == SignalType.REPEATED_READ_NO_FRONTIER
    )

    assert active.evidence_event_ids == ("event-4", "event-5")


def test_unknown_read_and_observation_gap_break_no_frontier_streak() -> None:
    engine = SignalEngine(no_frontier_threshold=2)
    identity = _event("event-3", "completed").identity

    assert engine.update(_tool_call("event-1", {"path": "src/example.py"}), 1) == ()
    assert engine.update(_tool_call("event-2", {"path": "src/example.py"}), 2) == ()
    assert engine.update(_tool_call("event-3", {"line": 3}), 3) == ()
    assert engine.update(_tool_call("event-4", {"path": "src/example.py"}), 4) == ()
    gap = TraceEvent(
        "observation_gap",
        {},
        event_id="gap-1",
        identity=identity,
        connection_epoch=1,
    )
    assert engine.update(gap, 5) == ()
    assert engine.update(_tool_call("event-5", {"path": "src/example.py"}), 6) == ()
    assert engine.update(_tool_call("event-6", {"path": "src/example.py"}), 7) == ()
    active = next(
        candidate
        for candidate in engine.update(_tool_call("event-7", {"path": "src/example.py"}), 8)
        if candidate.signal_type == SignalType.REPEATED_READ_NO_FRONTIER
    )

    assert active.evidence_event_ids == ("event-6", "event-7")


def test_non_read_tool_with_resource_does_not_claim_no_frontier() -> None:
    engine = SignalEngine(no_frontier_threshold=2)

    for index in range(1, 5):
        assert (
            engine.update(
                _tool_call(
                    f"event-{index}",
                    {"path": "src/example.py", "attempt": index},
                    tool="execute",
                ),
                index,
            )
            == ()
        )


def test_different_or_unknown_tool_arguments_do_not_collapse() -> None:
    engine = SignalEngine(repeated_tool_threshold=2)

    assert engine.update(_tool_call("event-1", {"path": "src/one.py"}), 1) == ()
    assert engine.update(_tool_call("event-2", {"path": "src/two.py"}), 2) == ()
    assert engine.update(_tool_call("event-3", object()), 3) == ()
    assert engine.update(_tool_call("event-4", object()), 4) == ()


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
    next_update = next(
        candidate
        for candidate in recovered.update(_event("event-3", "failed"), 4)
        if candidate.signal_type == SignalType.FAILURE_STREAK
    )

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


def test_runtime_correlates_live_hook_blocks_to_exact_app_server_turn(tmp_path: Path) -> None:
    runtime = AppServerRecoveryLoop("ws://unused", tmp_path / "sessions", ThreadStateStore())
    identity = runtime._runtime_identity("external-thread", "external-turn", "attachment-1")
    runtime._record(
        TraceEvent(
            "turn_started",
            {},
            event_id="turn-start",
            identity=identity,
            connection_epoch=1,
        )
    )
    runtime._record(
        TraceEvent(
            "runtime_reconciled",
            {"active_turn": True},
            event_id="runtime-ready",
            identity=identity,
            connection_epoch=1,
        )
    )
    proposal: dict[str, object] = {
        "tool_use_id": "call-1",
        "command": "git reset --hard",
        "files": [],
    }
    base: dict[str, object] = {
        "identity": {"thread_id": "external-thread", "turn_id": "external-turn"},
        "proposal": proposal,
    }
    decision = {"allowed": False, "rule": "git_reset_hard"}

    runtime.record_gate_decision(
        {**base, "identity": {"thread_id": "external-thread", "turn_id": "other-turn"}},
        decision,
    )
    runtime.record_gate_decision(base, decision)
    runtime.record_gate_decision(
        {**base, "proposal": {**proposal, "tool_use_id": "call-2"}}, decision
    )

    records = runtime.ingestor.records()
    blocks = [record.event for record in records if record.event.kind == "deterministic_gate_block"]
    candidates = [record.event for record in records if record.event.kind == "signal_candidate"]
    assert len(blocks) == 2
    assert len(candidates) == 1
    assert candidates[0].payload["signal_type"] == "recurrence_after_deterministic_block"
    assert candidates[0].payload["target_connection_epoch"] == 1


def test_runtime_batches_same_source_candidates_before_submitting_review(tmp_path: Path) -> None:
    class TwoSignalEngine(SignalEngine):
        def update(
            self,
            event: TraceEvent,
            state_version: int,
            state: ThreadState | None = None,
        ) -> tuple[SignalCandidate, ...]:
            if event.event_id != "event-1" or event.identity is None:
                return ()
            assert event.identity.thread_id is not None
            return tuple(
                SignalCandidate(
                    f"signal-{index}",
                    signal_type,
                    event.identity.thread_id,
                    event.identity.turn_id.value if event.identity.turn_id else None,
                    event.connection_epoch,
                    state_version,
                    event.occurred_at,
                    event.occurred_at,
                    index,
                    (f"evidence-{index}",),
                    (f"resource:{index}",),
                    SignalStatus.ACTIVE,
                    event.event_id,
                    ((feature, index),),
                )
                for index, signal_type, feature in (
                    (2, SignalType.FAILURE_STREAK, "consecutive_failures"),
                    (
                        3,
                        SignalType.REPEATED_EQUIVALENT_TOOL_CALL,
                        "consecutive_equivalent_calls",
                    ),
                )
            )

    submitted: list[ReviewerJob] = []
    runtime = AppServerRecoveryLoop(
        "ws://unused",
        tmp_path / "sessions",
        ThreadStateStore(),
        signals=TwoSignalEngine(),
        on_review_job=submitted.append,
    )
    runtime._record(
        TraceEvent(
            "turn_started",
            {},
            event_id="turn-start",
            identity=_event("event-1", "completed").identity,
            connection_epoch=1,
        )
    )
    runtime._record(_event("event-1", "completed"))

    records = runtime.ingestor.records()
    candidates = [record.event for record in records if record.event.kind == "signal_candidate"]
    queued = [record.event for record in records if record.event.kind == "review_job_queued"]

    assert len(candidates) == 2
    assert len(queued) == 1
    assert queued[0].payload["signal_ids"] == ["signal-2", "signal-3"]
    assert len(submitted) == 1
    assert submitted[0].reviewer_input.evidence_event_ids == ("evidence-2", "evidence-3")


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
