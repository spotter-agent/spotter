from dataclasses import FrozenInstanceError

import pytest

from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.snapshot import StepRecord
from spotter.thread_state import (
    ConditionStatus,
    HistoryStatus,
    StateItemKind,
    StateItemStatus,
    ThreadStateError,
    ThreadStateStore,
    ValidationStatus,
)
from spotter.trace import TraceEvent, TraceProvenance


def _identity(thread: str = "thread-1", turn: str | None = "turn-1") -> RuntimeIdentity:
    return RuntimeIdentity(
        ThreadId(thread),
        TurnId(turn) if turn else None,
        None,
        IdentityProvenance("codex", thread, turn),
    )


def _event(
    kind: str,
    payload: dict[str, object] | None = None,
    *,
    event_id: str | None = None,
    thread: str = "thread-1",
    turn: str | None = "turn-1",
    operation_id: str | None = None,
    epoch: int | None = 1,
) -> TraceEvent:
    return TraceEvent(
        kind,
        payload or {},
        event_id=event_id,
        occurred_at=100,
        identity=_identity(thread, turn),
        operation_id=operation_id,
        provenance=TraceProvenance("codex_app_server", kind),
        connection_epoch=epoch,
    )


def test_reduces_normal_trace_incrementally_into_typed_state() -> None:
    store = ThreadStateStore()
    store.observe(_event("thread_started", {"cwd": "/repo"}, event_id="thread"))
    store.observe(
        _event(
            "user_prompt",
            {"content": [{"type": "text", "text": "Fix login timeout"}]},
            event_id="goal",
        )
    )
    store.observe(_event("constraint", {"text": "Do not change the API"}, event_id="c1"))
    store.observe(
        _event(
            "reasoning_summary",
            {"summary": ["The retry loop may be wrong"]},
            event_id="h1",
        )
    )
    store.observe(_event("plan", {"text": "Inspect then patch"}, event_id="plan"))
    store.observe(_event("turn_started", event_id="turn-start"))
    store.observe(
        _event(
            "command_started",
            {"command": "pytest"},
            event_id="cmd-start",
            operation_id="cmd-1",
        )
    )
    store.observe(
        _event(
            "command_result",
            {"command": "pytest", "status": "failed", "exitCode": 1},
            event_id="cmd-done",
            operation_id="cmd-1",
        )
    )
    state = store.observe(
        _event(
            "reviewer_decision",
            {"status": "nudge", "text": "Verify the timeout path"},
            event_id="review",
        )
    )

    assert state.version == 9
    assert state.task.goal is not None and state.task.goal.text == "Fix login timeout"
    assert state.task.constraints[0].kind == StateItemKind.CONSTRAINT
    assert state.execution.plan_summary is not None
    assert state.active_turn_id == TurnId("turn-1")
    assert state.execution.active_items == frozenset()
    assert state.execution.recent_failures[-1].provenance.event_id == "cmd-done"
    assert state.execution.recent_failures[-1].provenance.created_at == 100
    assert state.supervision.interventions[-1].kind == StateItemKind.INTERVENTION
    assert {item.kind for item in state.evidence.items} == {
        StateItemKind.HYPOTHESIS,
        StateItemKind.OBSERVATION,
    }
    assert state.coverage.history == HistoryStatus.COMPLETE


def test_duplicate_event_id_is_idempotent() -> None:
    store = ThreadStateStore()
    event = _event("file_edit", {"files": ["src/a.py"], "status": "completed"}, event_id="edit-1")

    first = store.observe(event)
    duplicate = store.observe(event)

    assert duplicate is first
    assert duplicate.version == 1
    assert duplicate.workspace.touched_files == {"src/a.py"}
    assert len(duplicate.execution.recent_outcomes) == 1


def test_out_of_order_turn_lifecycle_is_explicit_and_does_not_regress() -> None:
    store = ThreadStateStore()
    completed = store.observe(_event("turn_completed", event_id="done"))
    late_start = store.observe(_event("turn_started", event_id="start"))

    assert completed.active_turn_id is None
    assert late_start.active_turn_id is None
    assert late_start.coverage.history == HistoryStatus.PARTIAL
    assert late_start.coverage.inconsistencies == (
        "turn_completed_without_start:turn-1",
        "late_start_for_completed_turn:turn-1",
    )


def test_interleaved_threads_remain_isolated() -> None:
    store = ThreadStateStore()
    first = store.observe(_event("user_prompt", {"prompt": "First"}, event_id="a"))
    second = store.observe(
        _event("user_prompt", {"prompt": "Second"}, event_id="b", thread="thread-2")
    )

    assert first.thread_id != second.thread_id
    assert store.snapshot(ThreadId("thread-1")).task.goal.text == "First"  # type: ignore[union-attr]
    assert store.snapshot(ThreadId("thread-2")).task.goal.text == "Second"  # type: ignore[union-attr]

    store.observe(
        _event(
            "reasoning_summary",
            {"summary": ["Child-only context"], "parent_thread_id": "thread-1"},
            event_id="child",
            thread="child-thread",
        )
    )
    assert all(
        item.text != "Child-only context"
        for item in store.snapshot(ThreadId("thread-1")).evidence.items
    )


def test_spotter_advisory_input_does_not_replace_the_user_goal() -> None:
    store = ThreadStateStore()
    original = store.observe(
        _event("user_prompt", {"prompt": "Fix the login timeout"}, event_id="goal")
    )

    advised = store.observe(
        _event(
            "user_prompt",
            {
                "content": [{"type": "text", "text": "Verify the retry assumption"}],
                "client_user_message_id": "spotter:intervention:job-1",
                "input_origin": "spotter_supervision",
                "intervention_relation": "target_turn",
            },
            event_id="advisory",
        )
    )

    assert advised.task.goal == original.task.goal
    assert advised.evidence.items == original.evidence.items
    assert advised.supervision.interventions[-1].text == "Verify the retry assumption"


def test_snapshot_is_deeply_immutable_and_version_anchored() -> None:
    store = ThreadStateStore()
    snapshot = store.observe(_event("user_prompt", {"prompt": "Goal"}, event_id="goal"))
    store.observe(_event("constraint", {"text": "Keep API"}, event_id="constraint"))

    assert snapshot.version == 1
    assert snapshot.task.constraints == ()
    with pytest.raises(FrozenInstanceError):
        snapshot.version = 9  # type: ignore[misc]


def test_replay_is_deterministic_and_hydration_is_not_control_ready() -> None:
    events = [
        _event("thread_started", event_id="thread"),
        _event("turn_started", event_id="turn"),
        _event("runtime_reconciled", {"capabilities": ["steer"]}, event_id="ready"),
    ]
    uninterrupted = ThreadStateStore().replay(events)
    replayed = ThreadStateStore().replay(events)
    records = [StepRecord(index, event, None) for index, event in enumerate(events)]
    hydrated = ThreadStateStore().hydrate(records)

    assert replayed == uninterrupted
    assert uninterrupted[0].control_ready is True
    assert hydrated[0].control_ready is False
    assert hydrated[0].active_turn_id is None


def test_connection_epoch_and_gap_require_live_reconciliation() -> None:
    store = ThreadStateStore()
    store.observe(_event("runtime_reconciled", {"capabilities": ["steer"]}, event_id="r1"))
    changed = store.observe(
        _event(
            "observation_gap",
            {
                "started_at": 100,
                "ended_at": 110,
                "epoch_before": 1,
                "epoch_after": 2,
                "backfill_status": "partial",
            },
            event_id="gap",
            epoch=2,
        )
    )
    reconciled = store.observe(
        _event(
            "runtime_reconciled",
            {"capabilities": ["interrupt", "steer"], "active_turn": True},
            epoch=2,
        )
    )

    assert changed.thread_id == ThreadId("thread-1")
    assert changed.connection_epoch == 2
    assert changed.control_ready is False
    assert changed.coverage.history == HistoryStatus.PARTIAL
    assert changed.coverage.gaps[0].backfill_status == "partial"
    assert reconciled.control_ready is True
    assert reconciled.active_turn_id == TurnId("turn-1")
    assert reconciled.capabilities == ("interrupt", "steer")


def test_verification_and_invalidation_keep_fact_types_distinct() -> None:
    store = ThreadStateStore()
    store.observe(
        _event(
            "test_result",
            {"status": "passed", "text": "login tests passed"},
            event_id="e1",
        )
    )
    store.observe(
        _event(
            "verification_condition",
            {
                "condition_id": "vc1",
                "kind": "test_passes",
                "scope": "tests/login.py",
                "required_evidence": ["test_result"],
                "created_from": "constraint",
            },
            event_id="condition",
        )
    )
    store.observe(
        _event(
            "verification_satisfied",
            {"condition_id": "vc1", "evidence_id": "e1", "text": "login tests pass"},
            event_id="fact",
        )
    )
    store.observe(
        _event(
            "hypothesis",
            {"text": "The fix is safe", "evidence_ids": ["e1"]},
            event_id="h1",
        )
    )
    invalidated = store.observe(
        _event("evidence_invalidated", {"evidence_id": "e1"}, event_id="invalidate")
    )

    assert invalidated.evidence.conditions[0].status == ConditionStatus.INVALIDATED
    fact = next(
        item for item in invalidated.evidence.items if item.kind == StateItemKind.VERIFIED_FACT
    )
    hypothesis = next(
        item for item in invalidated.evidence.items if item.kind == StateItemKind.HYPOTHESIS
    )
    assert fact.status == StateItemStatus.STALE
    assert hypothesis.status == StateItemStatus.STALE
    assert invalidated.evidence.stale_hypothesis_ids == {"h1"}

    dependent = store.observe(
        _event(
            "hypothesis",
            {"text": "Proceed with the fix", "depends_on": ["h1"]},
            event_id="h2",
        )
    )
    later = next(item for item in dependent.evidence.items if item.id == "h2")
    assert later.status == StateItemStatus.STALE
    assert dependent.evidence.stale_hypothesis_ids == {"h1", "h2"}


def test_old_evidence_remains_addressable_for_later_verification() -> None:
    store = ThreadStateStore()
    store.observe(
        _event(
            "test_result",
            {"status": "passed", "text": "original proof"},
            event_id="proof",
        )
    )
    for index in range(51):
        store.observe(
            _event(
                "command_result",
                {"status": "completed", "command": f"echo {index}"},
                event_id=f"outcome-{index}",
            )
        )
    store.observe(
        _event(
            "verification_condition",
            {
                "condition_id": "condition",
                "kind": "test_passes",
                "required_evidence": ["test_result"],
            },
            event_id="condition-event",
        )
    )
    state = store.observe(
        _event(
            "verification_satisfied",
            {"condition_id": "condition", "evidence_id": "proof"},
            event_id="fact",
        )
    )

    assert state.evidence.conditions[0].status == ConditionStatus.SATISFIED
    assert all(
        "unknown_verification_evidence" not in value for value in state.coverage.inconsistencies
    )


def test_validation_and_unknown_coverage_never_infer_missing_outcomes() -> None:
    store = ThreadStateStore()
    state = store.observe(_event("runtime_event_unknown", {"method": "future"}))
    state = store.observe(
        _event("file_edit", {"files": ["src/a.py"]}, event_id="edit", operation_id="edit")
    )
    unknown = store.observe(_event("test_result", {}, event_id="test-unknown"))
    failed = store.observe(_event("test_result", {"status": "failed"}, event_id="test-failed"))
    passed = store.observe(_event("test_result", {"status": "passed"}, event_id="test-passed"))
    edited_after_validation = store.observe(
        _event("file_edit", {"files": ["src/b.py"]}, event_id="edit-after-pass")
    )

    assert state.coverage.unknown_event_count == 1
    assert unknown.execution.validation == ValidationStatus.UNKNOWN
    assert unknown.workspace.edits_since_validation == {"src/a.py"}
    assert failed.execution.validation == ValidationStatus.FAILED
    assert passed.execution.validation == ValidationStatus.PASSED
    assert passed.workspace.edits_since_validation == {"src/a.py"}
    assert edited_after_validation.execution.validation == ValidationStatus.STALE
    assert edited_after_validation.workspace.edits_since_validation == {
        "src/a.py",
        "src/b.py",
    }


def test_validation_clears_only_explicitly_validated_scope() -> None:
    store = ThreadStateStore()
    store.observe(
        _event(
            "file_edit",
            {"status": "completed", "files": ["src/pkg/a.py", "src/other/b.py"]},
            event_id="edit",
        )
    )

    state = store.observe(
        _event(
            "test_result",
            {"status": "passed", "validated_paths": ["src/pkg"]},
            event_id="test-passed",
        )
    )

    assert state.execution.validation == ValidationStatus.PASSED
    assert state.workspace.edits_since_validation == {"src/other/b.py"}


def test_nested_hook_outcome_uses_shared_failure_classification() -> None:
    state = ThreadStateStore().observe(
        _event(
            "tool_result",
            {"tool_response": {"exit_code": 1}},
            event_id="hook-failure",
        )
    )

    assert state.execution.recent_failures[-1].provenance.event_id == "hook-failure"


def test_hydration_skips_legacy_records_without_inventing_thread_identity() -> None:
    app_event = _event("thread_started", event_id="thread")
    records = [
        StepRecord(0, TraceEvent("tool_result", {"session_id": "legacy"}), None),
        StepRecord(1, app_event, None),
    ]

    states = ThreadStateStore().hydrate(records)

    assert len(states) == 1
    assert states[0].thread_id == ThreadId("thread-1")


def test_missing_thread_identity_is_rejected() -> None:
    with pytest.raises(ThreadStateError, match="no logical thread identity"):
        ThreadStateStore().observe(TraceEvent("thread_started"))
