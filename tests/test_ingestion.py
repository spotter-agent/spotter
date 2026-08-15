from pathlib import Path
from typing import Any

import pytest

from spotter.app_server import AppServerEvent
from spotter.config import McpToolSemantics
from spotter.effects import effect_event
from spotter.hook import event_from_hook
from spotter.ingestion import AppServerTraceIngestor, CodexTraceNormalizer, IngestionError
from spotter.snapshot import StepJournal
from spotter.trace import TraceEvent


def _event(method: str, params: dict[str, Any]) -> AppServerEvent:
    return AppServerEvent(method, {"method": method, "params": params})


def _turn(status: str = "inProgress", **timestamps: int) -> dict[str, Any]:
    return {
        "id": "turn-1",
        "status": status,
        "items": [],
        "error": None,
        **timestamps,
    }


def _item_event(method: str, item: dict[str, Any], timestamp_ms: int = 1_000) -> AppServerEvent:
    timestamp = "completedAtMs" if method == "item/completed" else "startedAtMs"
    return _event(
        method,
        {"threadId": "thread-1", "turnId": "turn-1", "item": item, timestamp: timestamp_ms},
    )


def test_normalizes_lifecycle_and_authoritative_item_families() -> None:
    normalizer = CodexTraceNormalizer()
    thread = normalizer.normalize(
        _event(
            "thread/started",
            {
                "thread": {
                    "id": "thread-1",
                    "createdAt": 100,
                    "cwd": "/repo",
                    "status": {"type": "idle"},
                    "futureField": "must not leak",
                }
            },
        )
    )
    turn = normalizer.normalize(
        _event("turn/started", {"threadId": "thread-1", "turn": _turn(startedAt=101)})
    )
    completed_turn = normalizer.normalize(
        _event(
            "turn/completed",
            {"threadId": "thread-1", "turn": _turn("completed", completedAt=103)},
        )
    )
    command = normalizer.normalize(
        _item_event(
            "item/completed",
            {
                "id": "command-7",
                "type": "commandExecution",
                "command": "pytest",
                "cwd": "/repo",
                "status": "completed",
                "aggregatedOutput": "ok",
                "exitCode": 0,
                "durationMs": 20,
                "commandActions": [],
            },
            102_000,
        )
    )
    reasoning = normalizer.normalize(
        _item_event(
            "item/completed",
            {
                "id": "reasoning-1",
                "type": "reasoning",
                "summary": ["Check the failing test"],
                "content": ["private chain of thought"],
            },
        )
    )

    assert thread.kind == "thread_started"
    assert thread.occurred_at == 100
    assert thread.payload == {"cwd": "/repo", "status": {"type": "idle"}}
    assert turn.kind == "turn_started"
    assert completed_turn.payload["observed_start"] is True
    assert thread.identity is not None and turn.identity is not None
    assert thread.identity.thread_id == turn.identity.thread_id
    assert turn.identity.provenance.agent_turn_id == "turn-1"
    assert command.kind == "command_result"
    assert command.operation_id == command.item_id == "command-7"
    assert command.occurred_at == 102
    assert command.payload["exitCode"] == 0
    assert reasoning.payload["summary"] == ["Check the failing test"]
    assert "content" not in reasoning.payload


def test_app_server_command_effects_share_native_correlation_with_hooks(tmp_path: Path) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    ingestor.ingest(_event("thread/started", {"thread": {"id": "thread-1", "createdAt": 100}}))
    ingestor.ingest(_event("turn/started", {"threadId": "thread-1", "turn": _turn()}))
    completed = ingestor.ingest(
        _item_event(
            "item/completed",
            {
                "id": "call-7",
                "type": "commandExecution",
                "command": "git push origin feature",
                "cwd": "/repo",
                "status": "completed",
                "exitCode": 0,
            },
        )
    )
    assert completed is not None
    assert (
        completed.event.payload["reversibility_class"],
        completed.event.payload["effect_classifier"],
        completed.event.payload["semantic_operation"],
    ) == ("C", "git", "git.push")

    effects = [
        record.event for record in ingestor.records() if record.event.kind == "external_effect"
    ]
    assert len(effects) == 1
    assert (effects[0].payload["outcome"], effects[0].payload["outcome_evidence"]) == (
        "unknown",
        "exit_zero_only",
    )
    assert effects[0].provenance is not None
    assert effects[0].provenance.source == "codex_app_server"

    hook_result = event_from_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_use_id": "call-7",
            "tool_input": {"command": "git push origin feature"},
            "tool_response": {"exit_code": 0},
        }
    )
    hook_effect = effect_event(hook_result)
    assert hook_effect is not None
    assert hook_effect.payload["effect_id"] == effects[0].payload["effect_id"]


def test_app_server_mcp_calls_use_exact_configured_semantics() -> None:
    semantics = (
        McpToolSemantics("inventory", "lookup", "read", "A", ("item_id",)),
        McpToolSemantics("admin", "lookup", "delete", "C", ("item_id",)),
    )
    normalizer = CodexTraceNormalizer(mcp_semantics=semantics)
    normalizer.normalize(_event("thread/started", {"thread": {"id": "thread-1", "createdAt": 100}}))
    normalizer.normalize(_event("turn/started", {"threadId": "thread-1", "turn": _turn()}))

    def call(server: str) -> TraceEvent:
        return normalizer.normalize(
            _item_event(
                "item/completed",
                {
                    "id": f"{server}-call",
                    "type": "mcpToolCall",
                    "server": server,
                    "tool": "lookup",
                    "arguments": {"item_id": 42, "description": "model-generated read claim"},
                    "status": "completed",
                },
            )
        )

    read = call("inventory")
    delete = call("admin")

    assert (read.payload["reversibility_class"], read.payload["resource"]) == (
        "A",
        "item_id=42",
    )
    assert (delete.payload["reversibility_class"], delete.payload["resource"]) == (
        "C",
        "item_id=42",
    )
    assert read.payload["effect_classifier"] == delete.payload["effect_classifier"] == "mcp_config"


def test_app_server_downgrades_uncheckpointed_class_b_mutations(tmp_path: Path) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    ingestor.ingest(_event("thread/started", {"thread": {"id": "thread-1", "createdAt": 100}}))
    ingestor.ingest(_event("turn/started", {"threadId": "thread-1", "turn": _turn()}))
    completed = ingestor.ingest(
        _item_event(
            "item/completed",
            {
                "id": "edit-1",
                "type": "fileChange",
                "changes": [{"path": "src/example.py", "kind": "update"}],
                "status": "completed",
            },
        )
    )
    assert completed is not None
    assert completed.event.payload["reversibility_class"] == "C"
    assert completed.event.payload["expected_reversibility_class"] == "B"
    assert completed.event.payload["checkpoint_required"] is True
    assert completed.event.payload["checkpoint_observed"] is False
    assert completed.event.payload["effect_reason"] == "checkpoint_unavailable"

    effects = [
        record.event for record in ingestor.records() if record.event.kind == "external_effect"
    ]
    assert len(effects) == 1
    assert effects[0].payload["kind"] == "uncheckpointed_workspace_write"
    assert effects[0].payload["outcome"] == "succeeded"


def test_normalizes_user_message_control_correlation() -> None:
    event = CodexTraceNormalizer().normalize(
        _item_event(
            "item/completed",
            {
                "id": "message-1",
                "type": "userMessage",
                "clientId": "spotter-control-1",
                "content": [{"type": "text", "text": "verify this"}],
            },
        )
    )

    assert event.kind == "user_prompt"
    assert event.payload == {
        "content": [{"type": "text", "text": "verify this"}],
        "client_user_message_id": "spotter-control-1",
        "lifecycle": "completed",
    }


def test_normalizes_completed_final_answer_phase() -> None:
    event = CodexTraceNormalizer().normalize(
        _item_event(
            "item/completed",
            {
                "id": "answer-1",
                "type": "agentMessage",
                "text": "The task is complete",
                "phase": "final_answer",
            },
        )
    )

    assert event.kind == "agent_message"
    assert event.payload == {
        "text": "The task is complete",
        "phase": "final_answer",
        "lifecycle": "completed",
    }


def test_correlates_operation_by_item_id_not_event_adjacency(tmp_path: Path) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    command = {
        "id": "command-7",
        "type": "commandExecution",
        "command": "pytest",
        "cwd": "/repo",
        "status": "inProgress",
        "commandActions": [],
    }
    started = ingestor.ingest(_item_event("item/started", command, 1_000))
    ingestor.ingest(
        _item_event(
            "item/completed",
            {"id": "message-1", "type": "agentMessage", "text": "still working"},
            1_500,
        )
    )
    completed = ingestor.ingest(
        _item_event(
            "item/completed",
            {**command, "status": "completed", "aggregatedOutput": "ok", "exitCode": 0},
            2_000,
        )
    )

    assert started is not None and completed is not None
    assert started.event.operation_id == completed.event.operation_id == "command-7"
    assert completed.event.payload["observed_start"] is True


def test_duplicate_and_out_of_order_events_reconcile_across_restart(tmp_path: Path) -> None:
    command = {
        "id": "command-7",
        "type": "commandExecution",
        "command": "pytest",
        "cwd": "/repo",
        "status": "completed",
        "commandActions": [],
    }
    completion = _item_event("item/completed", command, 2_000)
    first = AppServerTraceIngestor(tmp_path)
    record = first.ingest(completion)

    assert record is not None
    assert record.event.payload["observed_start"] is False
    assert first.ingest(completion) is None

    resumed = AppServerTraceIngestor(tmp_path)
    assert resumed.ingest(completion) is None
    assert resumed.ingest(_item_event("item/started", {**command, "status": "inProgress"})) is None


def test_recovery_repairs_a_torn_journal_tail(tmp_path: Path) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    ingestor.ingest(
        _event(
            "thread/started",
            {"thread": {"id": "thread-1", "createdAt": 100, "cwd": "/repo"}},
        )
    )
    journal = next(tmp_path.glob("app-server-*.jsonl"))
    with journal.open("ab") as handle:
        handle.write(b'{"step": 1')

    resumed = AppServerTraceIngestor(tmp_path)
    resumed.ingest(_event("thread/status/changed", {"threadId": "thread-1", "status": "active"}))

    assert [record.event.kind for record in StepJournal.load(journal)] == [
        "thread_started",
        "thread_status",
    ]


def test_identical_stateless_notifications_are_not_mistaken_for_replays(tmp_path: Path) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    status = _event("thread/status/changed", {"threadId": "thread-1", "status": "active"})

    first = ingestor.ingest(status)
    second = ingestor.ingest(status)

    assert first is not None and second is not None
    assert first.event.event_id is None
    assert second.event.event_id is None


def test_operation_and_timestamp_state_is_scoped_to_a_turn(tmp_path: Path) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    item = {
        "id": "reused-item-id",
        "type": "commandExecution",
        "command": "pytest",
        "cwd": "/repo",
        "status": "completed",
        "commandActions": [],
    }
    ingestor.ingest(_item_event("item/completed", item, 2_000))
    second_turn = ingestor.ingest(
        _event(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-2",
                "item": item,
                "completedAtMs": 1_000,
            },
        )
    )

    assert second_turn is not None
    assert second_turn.event.payload["observed_start"] is False
    assert "out_of_order" not in second_turn.event.payload


def test_operation_correlation_does_not_cross_connection_epochs(tmp_path: Path) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    item = {
        "id": "command-7",
        "type": "commandExecution",
        "command": "pytest",
        "cwd": "/repo",
        "status": "inProgress",
        "commandActions": [],
    }
    ingestor.ingest(_item_event("item/started", item, 2_000), connection_epoch=1)
    completed = ingestor.ingest(
        _item_event("item/completed", {**item, "status": "completed"}, 1_000),
        connection_epoch=2,
    )

    assert completed is not None
    assert completed.event.connection_epoch == 2
    assert completed.event.payload["observed_start"] is False
    assert "out_of_order" not in completed.event.payload


def test_equal_source_timestamps_keep_durable_arrival_order_across_restart(
    tmp_path: Path,
) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    command = {
        "type": "commandExecution",
        "command": "pytest",
        "cwd": "/repo",
        "status": "inProgress",
        "commandActions": [],
    }
    first = ingestor.ingest(
        _item_event("item/started", {**command, "id": "one"}, 1_000), connection_epoch=7
    )
    second = ingestor.ingest(
        _item_event("item/started", {**command, "id": "two"}, 1_000), connection_epoch=7
    )

    assert first is not None and first.event.arrival_seq == 1
    assert second is not None and second.event.arrival_seq == 2
    assert first.event.occurred_at == second.event.occurred_at == 1.0

    resumed = AppServerTraceIngestor(tmp_path).ingest(
        _item_event("item/started", {**command, "id": "three"}, 1_000),
        connection_epoch=7,
    )

    assert resumed is not None and resumed.event.arrival_seq == 3
    assert [record.event.arrival_seq for record in AppServerTraceIngestor(tmp_path).records()] == [
        1,
        2,
        3,
    ]


def test_out_of_order_timestamp_is_explicit_and_terminal_conflicts_fail(tmp_path: Path) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    item = {
        "id": "command-7",
        "type": "commandExecution",
        "command": "pytest",
        "cwd": "/repo",
        "status": "inProgress",
        "commandActions": [],
    }
    ingestor.ingest(_item_event("item/started", item, 2_000))
    completed = ingestor.ingest(
        _item_event("item/completed", {**item, "status": "completed"}, 1_000)
    )

    assert completed is not None and completed.event.payload["out_of_order"] is True
    with pytest.raises(IngestionError, match="changed terminal outcome"):
        ingestor.ingest(_item_event("item/completed", {**item, "status": "failed"}, 3_000))


def test_unknown_and_token_events_keep_identity_without_wire_shapes() -> None:
    normalizer = CodexTraceNormalizer()
    unknown = normalizer.normalize(
        _event(
            "future/event",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "future-1",
                "newPayload": {"transport": "shape"},
            },
        )
    )
    usage = normalizer.normalize(
        _event(
            "thread/tokenUsage/updated",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "tokenUsage": {
                    "last": {
                        "inputTokens": 10,
                        "cachedInputTokens": 2,
                        "outputTokens": 4,
                        "reasoningOutputTokens": 1,
                        "futureCount": 99,
                    },
                    "total": {"totalTokens": 14, "cacheWriteInputTokens": 3},
                    "modelContextWindow": 200_000,
                },
            },
        )
    )

    assert unknown.kind == "runtime_event_unknown"
    assert unknown.payload == {"method": "future/event"}
    assert unknown.item_id == "future-1"
    assert unknown.identity is not None and unknown.identity.turn_id is not None
    assert usage.payload == {
        "last": {
            "inputTokens": 10,
            "cachedInputTokens": 2,
            "outputTokens": 4,
            "reasoningOutputTokens": 1,
        },
        "total": {"cacheWriteInputTokens": 3, "totalTokens": 14},
        "modelContextWindow": 200_000,
    }


def test_app_server_and_hook_journals_coexist_without_identity_inference(tmp_path: Path) -> None:
    from spotter.trace import TraceEvent

    hook_path = tmp_path / "hook-session.jsonl"
    StepJournal(hook_path).record(TraceEvent("tool_result", {"session_id": "legacy"}))
    ingestor = AppServerTraceIngestor(tmp_path)
    ingestor.ingest(
        _event(
            "thread/started",
            {"thread": {"id": "thread-1", "createdAt": 100, "cwd": "/repo"}},
        )
    )

    hook_event = StepJournal.load(hook_path)[0].event
    app_journals = list(tmp_path.glob("app-server-*.jsonl"))
    assert hook_event.identity is None
    assert len(app_journals) == 1
    app_event = StepJournal.load(app_journals[0])[0].event
    assert app_event.identity is not None
    assert app_event.identity.provenance.agent_thread_id == "thread-1"
    assert app_event.provenance is not None
    assert app_event.provenance.method == "thread/started"


def test_rejects_missing_required_identity() -> None:
    with pytest.raises(IngestionError, match="omitted thread or turn"):
        CodexTraceNormalizer().normalize(
            _event("turn/completed", {"threadId": "thread-1", "turn": {"status": "failed"}})
        )
