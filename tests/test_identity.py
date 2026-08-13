import pytest

from spotter.identity import (
    AttachmentStatus,
    ConflictingRuntimeIdentity,
    MissingRuntimeIdentity,
    RuntimeIdentity,
    RuntimeIdentityRegistry,
    ThreadStatus,
    TurnStatus,
    UnknownRuntimeIdentity,
)


def test_concurrent_threads_and_turns_remain_isolated() -> None:
    registry = RuntimeIdentityRegistry()
    first = registry.observe_thread("codex", "thread-1")
    second = registry.observe_thread("codex", "thread-2")
    first_attachment = registry.attach(first.id, agent_attachment_id="window-1")
    second_attachment = registry.attach(second.id, agent_attachment_id="window-2")

    first_turn = registry.start_turn(first.id, "turn-1", attachment_id=first_attachment.id)
    second_turn = registry.start_turn(second.id, "turn-1", attachment_id=second_attachment.id)

    assert first.id != second.id
    assert first_turn.id != second_turn.id
    assert registry.active_turns(first.id) == (first_turn,)
    assert registry.active_turns(second.id) == (second_turn,)


def test_duplicate_events_are_idempotent_and_terminal_turns_do_not_regress() -> None:
    registry = RuntimeIdentityRegistry()
    thread = registry.observe_thread("codex", "thread-1")
    attachment = registry.attach(thread.id, agent_attachment_id="window-1")

    first_start = registry.start_turn(thread.id, "turn-1", attachment_id=attachment.id)
    duplicate_start = registry.start_turn(thread.id, "turn-1", attachment_id=attachment.id)
    first_finish = registry.finish_turn(thread.id, "turn-1", TurnStatus.COMPLETED)
    duplicate_finish = registry.finish_turn(thread.id, "turn-1", TurnStatus.COMPLETED)
    late_start = registry.start_turn(thread.id, "turn-1", attachment_id=attachment.id)

    assert duplicate_start.id == first_start.id
    assert duplicate_finish == first_finish
    assert late_start.status == TurnStatus.COMPLETED


def test_multiple_turns_keep_individual_state_and_addresses() -> None:
    registry = RuntimeIdentityRegistry()
    thread = registry.observe_thread("codex", "thread-1")
    first = registry.start_turn(thread.id, "turn-1")
    registry.finish_turn(thread.id, "turn-1", TurnStatus.COMPLETED)
    second = registry.start_turn(thread.id, "turn-2")

    assert registry.turn(first.id).status == TurnStatus.COMPLETED
    assert registry.active_turns(thread.id) == (second,)
    assert registry.address_turn(second.id).provenance.agent_turn_id == "turn-2"


def test_out_of_order_completion_preserves_missing_start_observation() -> None:
    registry = RuntimeIdentityRegistry()
    thread = registry.observe_thread("codex", "thread-1")

    completed = registry.finish_turn(thread.id, "turn-1", TurnStatus.INTERRUPTED)
    assert not completed.observed_start

    started_late = registry.start_turn(thread.id, "turn-1")
    assert started_late.observed_start
    assert started_late.status == TurnStatus.INTERRUPTED
    with pytest.raises(ConflictingRuntimeIdentity):
        registry.finish_turn(thread.id, "turn-1", TurnStatus.COMPLETED)


def test_detach_and_resume_keep_thread_identity() -> None:
    registry = RuntimeIdentityRegistry()
    thread = registry.observe_thread("codex", "thread-1")
    first = registry.attach(thread.id)
    assert registry.thread(thread.id).status == ThreadStatus.ACTIVE

    closed = registry.detach(first.id)
    assert closed.status == AttachmentStatus.CLOSED
    assert registry.thread(thread.id).status == ThreadStatus.DORMANT

    reconciled = registry.resolve_thread("codex", "thread-1")
    resumed = registry.attach(reconciled.id)
    assert resumed.id != first.id
    assert reconciled.id == thread.id
    assert registry.thread(thread.id).status == ThreadStatus.ACTIVE


def test_attach_reopens_a_closed_external_attachment() -> None:
    registry = RuntimeIdentityRegistry()
    thread = registry.observe_thread("codex", "thread-1")
    first = registry.attach(thread.id, agent_attachment_id="window-1")
    registry.detach(first.id)

    resumed = registry.attach(thread.id, agent_attachment_id="window-1")

    assert resumed.id == first.id
    assert resumed.status == AttachmentStatus.ACTIVE
    assert registry.thread(thread.id).status == ThreadStatus.ACTIVE


def test_new_registry_derives_the_same_external_identity() -> None:
    first_registry = RuntimeIdentityRegistry()
    first_thread = first_registry.observe_thread("codex", "thread-1")
    first_turn = first_registry.start_turn(first_thread.id, "turn-1")

    recovered_registry = RuntimeIdentityRegistry()
    recovered_thread = recovered_registry.observe_thread("codex", "thread-1")
    recovered_turn = recovered_registry.start_turn(recovered_thread.id, "turn-1")

    assert recovered_thread.id == first_thread.id
    assert recovered_turn.id == first_turn.id


def test_concurrent_attachments_are_distinct_and_duplicates_are_stable() -> None:
    registry = RuntimeIdentityRegistry()
    thread = registry.observe_thread("codex", "thread-1")

    first = registry.attach(thread.id, agent_attachment_id="window-1")
    duplicate = registry.attach(thread.id, agent_attachment_id="window-1")
    second = registry.attach(thread.id, agent_attachment_id="window-2")

    assert duplicate == first
    assert second.id != first.id


def test_turn_address_is_specific_and_keeps_agent_provenance() -> None:
    registry = RuntimeIdentityRegistry()
    thread = registry.observe_thread("codex", "thread-1")
    attachment = registry.attach(thread.id, agent_attachment_id="window-1")
    turn = registry.start_turn(thread.id, "turn-7", attachment_id=attachment.id)

    address = registry.address_turn(turn.id)

    assert address.thread_id == thread.id
    assert address.turn_id == turn.id
    assert address.attachment_id == attachment.id
    assert address.provenance.agent_thread_id == "thread-1"
    assert address.provenance.agent_turn_id == "turn-7"


def test_missing_and_legacy_identity_are_explicit() -> None:
    registry = RuntimeIdentityRegistry()
    with pytest.raises(MissingRuntimeIdentity):
        registry.observe_thread("codex", "")
    with pytest.raises(MissingRuntimeIdentity):
        registry.observe_thread("codex", "thread\0suffix")
    with pytest.raises(UnknownRuntimeIdentity):
        registry.resolve_thread("codex", "missing")

    legacy = RuntimeIdentity.legacy_hook("codex", "hook-session-1")
    assert legacy.thread_id is None
    assert legacy.turn_id is None
    assert legacy.attachment_id is None
    assert legacy.provenance.legacy_session_id == "hook-session-1"
