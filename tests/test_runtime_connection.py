import asyncio
import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from websockets.asyncio.server import ServerConnection, serve

from spotter.app_server import (
    AppServerCapabilities,
    AppServerControlError,
    AppServerEvent,
    AppServerRpcError,
    AppServerTransportError,
    CapabilityStatus,
    CodexAppServerClient,
)
from spotter.review_scheduler import ReviewerJob
from spotter.runtime_connection import (
    AppServerRecoveryLoop,
    RecoveryState,
    RuntimeControlTarget,
    StaleControlTarget,
)
from spotter.snapshot import StepRecord
from spotter.thread_state import HistoryStatus, ThreadStateStore
from spotter.trace import TraceEvent

Handler = Callable[[ServerConnection], Awaitable[None]]


def _message(raw: str | bytes) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(raw))


async def _receive(connection: ServerConnection, method: str) -> dict[str, Any]:
    message = _message(await connection.recv())
    assert message["method"] == method
    return message


async def _reply(connection: ServerConnection, request: dict[str, Any], result: Any) -> None:
    await connection.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))


async def _reply_error(
    connection: ServerConnection, request: dict[str, Any], code: int, message: str
) -> None:
    await connection.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": code, "message": message},
            }
        )
    )


async def _initialize(connection: ServerConnection) -> None:
    request = await _receive(connection, "initialize")
    await _reply(
        connection,
        request,
        {"userAgent": "codex_cli_rs/0.147.0", "platformFamily": "unix"},
    )
    await _receive(connection, "initialized")
    probe = await _receive(connection, "thread/list")
    await _reply(connection, probe, {"data": [], "nextCursor": None})


@asynccontextmanager
async def _server(handler: Handler) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def test_turn_boundary_callback_runs_before_turn_is_reduced(tmp_path: Path) -> None:
    async def scenario() -> None:
        boundaries: list[tuple[bool, ...]] = []
        store = ThreadStateStore()

        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(connection, listed, {"data": [], "nextCursor": None})
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/started",
                        "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
                    }
                )
            )
            await connection.wait_closed()

        def boundary() -> None:
            boundaries.append(
                tuple(state.active_turn_id is not None for state in store.snapshots())
            )

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(
                endpoint,
                tmp_path / "sessions",
                store,
                on_turn_boundary=boundary,
            )
            await recovery.start()
            await _wait_until(lambda: bool(boundaries))
            await _wait_until(
                lambda: bool(store.snapshots()) and store.snapshots()[0].active_turn_id is not None
            )
            assert boundaries == [()]
            await recovery.close()

    asyncio.run(scenario())


def test_new_thread_notification_subscribes_to_followup_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(connection, listed, {"data": [], "nextCursor": None})
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "thread/started",
                        "params": {"thread": {"id": "thread-1"}},
                    }
                )
            )
            resumed = await _receive(connection, "thread/resume")
            assert resumed["params"]["threadId"] == "thread-1"
            await _reply_error(connection, resumed, -32000, "no rollout found for thread")
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "thread/status/changed",
                        "params": {"threadId": "thread-1", "status": "active"},
                    }
                )
            )
            resumed = await _receive(connection, "thread/resume")
            await _reply(connection, resumed, {"thread": {"id": "thread-1", "turns": []}})
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/started",
                        "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
                    }
                )
            )
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(endpoint, tmp_path / "sessions", ThreadStateStore())
            await recovery.start()
            await _wait_until(
                lambda: (
                    bool(recovery.thread_states.snapshots())
                    and recovery.thread_states.snapshots()[0].active_turn_id is not None
                )
            )
            state = recovery.thread_states.snapshots()[0]
            assert state.identity.provenance.agent_thread_id == "thread-1"
            assert state.identity.provenance.agent_turn_id == "turn-1"
            await recovery.close()

    asyncio.run(scenario())


def test_runtime_records_the_active_config_generation(tmp_path: Path) -> None:
    recovery = AppServerRecoveryLoop(
        "ws://unused",
        tmp_path / "sessions",
        ThreadStateStore(),
        config_generation="cfg-runtime",
    )

    record = recovery.record_review_event(TraceEvent("runtime_event_unknown"))

    assert record is not None
    assert record.event.config_generation == "cfg-runtime"


def test_reconnect_reconciles_epoch_gap_and_stale_control(tmp_path: Path) -> None:
    async def scenario() -> None:
        connections: list[ServerConnection] = []

        async def handler(connection: ServerConnection) -> None:
            connections.append(connection)
            number = len(connections)
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {"data": [{"id": "thread-1"}], "nextCursor": None},
            )
            read = await _receive(connection, "thread/resume")
            turns = [{"id": "turn-1", "status": "active"}] if number == 1 else []
            await _reply(
                connection,
                read,
                {"thread": {"id": "thread-1", "turns": turns}},
            )
            while True:
                try:
                    request = _message(await connection.recv())
                except Exception:
                    return
                if request.get("method") == "turn/steer":
                    await _reply(connection, request, {"turnId": "turn-1"})

        async with _server(handler) as endpoint:
            store = ThreadStateStore()
            recovery = AppServerRecoveryLoop(
                endpoint,
                tmp_path / "sessions",
                store,
                initial_backoff=0,
                maximum_backoff=0,
            )
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)

            first = store.snapshots()[0]
            assert first.connection_epoch == 1
            assert first.control_ready is True
            assert first.coverage.history == HistoryStatus.PARTIAL
            target = RuntimeControlTarget(first.identity, 1)
            assert (
                await recovery.steer(
                    target,
                    "verify",
                    control_id="control-1",
                    review_job_id="job-1",
                )
            )["turnId"] == "turn-1"
            with pytest.raises(ValueError, match="unique across durable runtime history"):
                await recovery.steer(target, "duplicate", control_id="control-1")
            await recovery.flush_control_telemetry()
            controls = [
                record.event
                for record in recovery.ingestor.records()
                if record.event.kind.startswith("control_")
            ]
            assert [event.kind for event in controls] == [
                "control_dispatch_started",
                "control_rpc_accepted",
            ]
            assert {event.payload["control_id"] for event in controls} == {"control-1"}
            assert controls[0].payload["target_connection_epoch"] == 1
            assert first.identity.turn_id is not None
            assert controls[0].payload["target_turn_id"] == first.identity.turn_id.value
            assert controls[0].payload["client_user_message_id"] == "control-1"
            assert controls[0].payload["intervention_id"] == "control-1"
            assert controls[0].payload["supervision_scope"] == "current_turn"
            assert controls[0].payload["must_not_become_user_goal"] is True
            assert controls[0].payload["expires_on"] == "target_turn_terminal"

            await connections[0].close()
            await _wait_until(
                lambda: (
                    recovery.state == RecoveryState.READY
                    and recovery.connection is not None
                    and recovery.connection.connection_epoch == 2
                )
            )

            second = store.snapshots()[0]
            assert second.connection_epoch == 2
            assert second.active_turn_id is None
            assert second.control_ready is False
            assert len(second.coverage.gaps) == 2
            assert recovery.metrics.reconnect_successes == 2
            assert recovery.metrics.observation_gaps == 2
            assert RecoveryState.RECONCILING in recovery.transitions
            assert RecoveryState.BACKING_OFF in recovery.transitions
            with pytest.raises(StaleControlTarget):
                await recovery.steer(target, "late", control_id="control-2")
            await recovery.flush_control_telemetry()
            stale = [
                record.event
                for record in recovery.ingestor.records()
                if record.event.payload.get("control_id") == "control-2"
            ]
            assert len(stale) == 1
            assert stale[0].kind == "control_terminal"
            assert stale[0].payload["outcome"] == "stale"
            assert stale[0].payload["reason_code"] == "stale_target"
            await recovery.close()

    asyncio.run(scenario())


def test_reconnect_records_server_and_capability_change(tmp_path: Path) -> None:
    class FakeClient:
        def __init__(self, version: str, steer: CapabilityStatus) -> None:
            self.server_info = {"userAgent": version}
            self.capabilities = AppServerCapabilities(
                observation=CapabilityStatus.AVAILABLE,
                thread_query=CapabilityStatus.AVAILABLE,
                steer=steer,
                interrupt=CapabilityStatus.UNKNOWN,
                atomic_pre_tool_veto=CapabilityStatus.UNAVAILABLE,
            )
            self.closed = asyncio.Event()

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            self.closed.set()

        async def list_threads(self, *, limit: int, cursor: str | None = None) -> dict[str, object]:
            return {"data": [{"id": "thread-1"}], "nextCursor": None}

        async def resume_thread(self, thread_id: str) -> dict[str, object]:
            return {
                "thread": {
                    "id": thread_id,
                    "turns": [{"id": "turn-1", "status": "active"}],
                }
            }

        async def next_event(self) -> AppServerEvent:
            await self.closed.wait()
            raise AppServerTransportError("closed")

        async def wait_closed(self) -> AppServerTransportError:
            await self.closed.wait()
            return AppServerTransportError("closed")

    async def scenario() -> None:
        first = FakeClient("codex/1", CapabilityStatus.AVAILABLE)
        second = FakeClient("codex/2", CapabilityStatus.UNAVAILABLE)
        pending = iter((first, second))
        recovery = AppServerRecoveryLoop(
            "ws://unused",
            tmp_path / "sessions",
            ThreadStateStore(),
            client_factory=lambda _: cast(CodexAppServerClient, next(pending)),
            initial_backoff=0,
            maximum_backoff=0,
        )

        await recovery.start()
        await _wait_until(lambda: recovery.state == RecoveryState.READY)
        first.closed.set()
        await _wait_until(
            lambda: (
                recovery.state == RecoveryState.READY
                and recovery.connection is not None
                and recovery.connection.connection_epoch == 2
            )
        )

        assert recovery.connection is not None
        assert recovery.connection.server_changed is True
        assert recovery.connection.capabilities_changed is True
        [changed] = [
            record.event
            for record in recovery.ingestor.records()
            if record.event.kind == "runtime_capabilities_changed"
        ]
        assert changed.payload["epoch_before"] == 1
        assert changed.payload["epoch_after"] == 2
        assert changed.payload["server_changed"] is True
        assert changed.payload["capability_changes"] == [
            {"capability": "steer", "before": "available", "after": "unavailable"}
        ]
        await recovery.close()

    asyncio.run(scenario())


def test_restart_hydrates_without_control_until_live_reconciliation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {"data": [{"id": "thread-1"}], "nextCursor": None},
            )
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {
                    "thread": {
                        "id": "thread-1",
                        "turns": [{"id": "turn-1", "status": "active"}],
                    }
                },
            )
            await connection.wait_closed()

        sessions = tmp_path / "sessions"
        async with _server(handler) as endpoint:
            first_store = ThreadStateStore()
            first = AppServerRecoveryLoop(endpoint, sessions, first_store)
            await first.start()
            await _wait_until(lambda: first.state == RecoveryState.READY)
            await first.close()

            recovered_store = ThreadStateStore()
            recovered = AppServerRecoveryLoop(endpoint, sessions, recovered_store)
            hydrated = recovered_store.snapshots()[0]
            assert hydrated.active_turn_id is None
            assert hydrated.control_ready is False

            await recovered.start()
            await _wait_until(lambda: recovered.state == RecoveryState.READY)
            reconciled = recovered_store.snapshots()[0]
            assert reconciled.connection_epoch == 2
            assert reconciled.active_turn_id is not None
            assert reconciled.control_ready is True
            await recovered.close()

    asyncio.run(scenario())


def test_settled_final_answer_fences_steer_but_not_interrupt(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {"data": [{"id": "thread-1"}], "nextCursor": None},
            )
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {
                    "thread": {
                        "id": "thread-1",
                        "turns": [{"id": "turn-1", "status": "active"}],
                    }
                },
            )
            interrupt = await _receive(connection, "turn/interrupt")
            await _reply(connection, interrupt, {})
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(endpoint, tmp_path / "sessions", ThreadStateStore())
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            recovery._record(
                TraceEvent(
                    "agent_message",
                    {
                        "text": "The task is complete",
                        "phase": "final_answer",
                        "lifecycle": "completed",
                    },
                    event_id="final-answer",
                    identity=state.identity,
                    connection_epoch=state.connection_epoch,
                )
            )
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            with pytest.raises(StaleControlTarget, match="settled terminal answer"):
                await recovery.steer(target, "too late", control_id="late-steer")
            assert await recovery.interrupt(target, control_id="late-interrupt") == {}
            await recovery.flush_control_telemetry()

            stale = next(
                record.event
                for record in recovery.ingestor.records()
                if record.event.payload.get("control_id") == "late-steer"
            )
            assert stale.kind == "control_terminal"
            assert stale.payload["outcome"] == "stale"
            assert stale.payload["reason_code"] == "terminal_answer_settled"
            await recovery.close()

    asyncio.run(scenario())


def test_control_rejection_and_unknown_acceptance_are_distinct(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {"data": [{"id": "thread-1"}], "nextCursor": None},
            )
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {
                    "thread": {
                        "id": "thread-1",
                        "turns": [{"id": "turn-1", "status": "active"}],
                    }
                },
            )
            stale = await _receive(connection, "turn/steer")
            await _reply_error(connection, stale, -32000, "no active turn to steer")
            rejected = await _receive(connection, "turn/steer")
            await _reply_error(connection, rejected, -32000, "not steerable")
            await _receive(connection, "turn/steer")
            await connection.close()

        async with _server(handler) as endpoint:
            store = ThreadStateStore()
            recovery = AppServerRecoveryLoop(
                endpoint,
                tmp_path / "sessions",
                store,
                initial_backoff=1,
                maximum_backoff=1,
            )
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = store.snapshots()[0]
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            with pytest.raises(AppServerControlError):
                await recovery.steer(target, "verify", control_id="control-stale")
            with pytest.raises(AppServerRpcError):
                await recovery.steer(target, "verify", control_id="control-rejected")
            with pytest.raises(AppServerTransportError):
                await recovery.steer(target, "verify", control_id="control-unknown")
            await recovery.flush_control_telemetry()

            terminals = {
                record.event.payload["control_id"]: (
                    record.event.payload["outcome"],
                    record.event.payload["reason_code"],
                )
                for record in recovery.ingestor.records()
                if record.event.kind == "control_terminal"
            }
            assert terminals == {
                "control-stale": ("stale", "no_active_turn"),
                "control-rejected": ("failed", "rpc_rejected"),
                "control-unknown": ("unknown", "acceptance_unknown"),
            }
            await recovery.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("method", "params", "reason_code"),
    [
        (
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
            "target_completed_without_observed_input",
        ),
        (
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "answer-1",
                    "type": "agentMessage",
                    "text": "Done",
                    "phase": "final_answer",
                },
            },
            "terminal_answer_without_observed_input",
        ),
    ],
)
def test_accepted_steer_without_observed_input_finishes_durably(
    tmp_path: Path,
    method: str,
    params: dict[str, object],
    reason_code: str,
) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {"data": [{"id": "thread-1"}], "nextCursor": None},
            )
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {
                    "thread": {
                        "id": "thread-1",
                        "turns": [{"id": "turn-1", "status": "active"}],
                    }
                },
            )
            steer = await _receive(connection, "turn/steer")
            await _reply(connection, steer, {"turnId": "turn-1"})
            await connection.send(
                json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
            )
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(endpoint, tmp_path / "sessions", ThreadStateStore())
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            await recovery.steer(
                target,
                "verify",
                control_id="spotter:intervention:job-1",
                review_job_id="job-1",
            )
            await _wait_until(
                lambda: any(
                    record.event.kind in {"turn_completed", "agent_message"}
                    for record in recovery.ingestor.records()
                )
            )
            await recovery.flush_control_telemetry()

            controls = [
                record.event
                for record in recovery.ingestor.records()
                if record.event.payload.get("control_id") == "spotter:intervention:job-1"
            ]
            assert [event.kind for event in controls] == [
                "control_dispatch_started",
                "control_rpc_accepted",
                "control_terminal",
            ]
            assert controls[-1].payload["outcome"] == "rpc_accepted_only"
            assert controls[-1].payload["reason_code"] == reason_code
            assert not any(
                event.kind.startswith("control_observed")
                for event in (record.event for record in recovery.ingestor.records())
            )
            await recovery.close()

    asyncio.run(scenario())


def test_turn_terminal_before_steer_ack_closes_acceptance_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {"data": [{"id": "thread-1"}], "nextCursor": None},
            )
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {
                    "thread": {
                        "id": "thread-1",
                        "turns": [{"id": "turn-1", "status": "active"}],
                    }
                },
            )
            steer = await _receive(connection, "turn/steer")
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    }
                )
            )
            await _wait_until(
                lambda: any(
                    record.event.kind == "turn_completed" for record in recovery.ingestor.records()
                )
            )
            await _reply(connection, steer, {"turnId": "turn-1"})
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(
                endpoint,
                tmp_path / "sessions",
                ThreadStateStore(),
            )
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            assert await recovery.steer(
                target,
                "verify",
                control_id="spotter:intervention:job-race",
                review_job_id="job-race",
            ) == {"turnId": "turn-1"}
            await recovery.flush_control_telemetry()

            terminals = [
                record.event
                for record in recovery.ingestor.records()
                if record.event.kind == "control_terminal"
                and record.event.payload.get("control_id") == "spotter:intervention:job-race"
            ]
            assert len(terminals) == 1
            assert terminals[0].payload["outcome"] == "rpc_accepted_only"
            assert terminals[0].payload["reason_code"] == "target_completed_without_observed_input"
            await recovery.close()

    asyncio.run(scenario())


def test_control_rpc_does_not_wait_for_bounded_telemetry_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {"data": [{"id": "thread-1"}], "nextCursor": None},
            )
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {
                    "thread": {
                        "id": "thread-1",
                        "turns": [{"id": "turn-1", "status": "active"}],
                    }
                },
            )
            while True:
                try:
                    request = await _receive(connection, "turn/steer")
                except Exception:
                    return
                await _reply(connection, request, {"turnId": "turn-1"})

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(
                endpoint,
                tmp_path / "sessions",
                ThreadStateStore(),
                control_telemetry_queue_size=1,
            )
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)
            writer_started = threading.Event()
            release_writer = threading.Event()
            append = recovery.ingestor.append_operational

            def slow_append(event: TraceEvent, *, observed_at: float) -> StepRecord:
                writer_started.set()
                assert release_writer.wait(2)
                return append(event, observed_at=observed_at)

            monkeypatch.setattr(recovery.ingestor, "append_operational", slow_append)
            first = asyncio.create_task(recovery.steer(target, "first", control_id="control-first"))
            try:
                assert await asyncio.to_thread(writer_started.wait, 1)
                assert (await asyncio.wait_for(asyncio.shield(first), 0.5))["turnId"] == "turn-1"
                assert (
                    await asyncio.wait_for(
                        recovery.steer(target, "second", control_id="control-second"), 0.5
                    )
                )["turnId"] == "turn-1"
                assert recovery.metrics.control_telemetry_dropped == 2
                assert recovery.metrics.control_telemetry_backlog_peak == 1
                assert recovery.last_control_telemetry_error == "control telemetry queue is full"
            finally:
                release_writer.set()
                if not first.done():
                    await first

            await recovery.flush_control_telemetry()
            controls = [
                record
                for record in recovery.ingestor.records()
                if record.event.kind.startswith("control_")
            ]
            assert [record.event.kind for record in controls] == [
                "control_dispatch_started",
                "control_rpc_accepted",
            ]
            assert controls[0].at is not None and controls[1].at is not None
            assert controls[0].at <= controls[1].at
            assert recovery.metrics.control_telemetry_errors == 0
            await recovery.close()

    asyncio.run(scenario())


def test_intervention_input_is_scoped_without_replacing_later_user_goals(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {"data": [{"id": "thread-1"}], "nextCursor": None},
            )
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {
                    "thread": {
                        "id": "thread-1",
                        "turns": [{"id": "turn-1", "status": "active"}],
                    }
                },
            )
            request = await _receive(connection, "turn/steer")
            await _reply(connection, request, {"turnId": "turn-1"})
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "item": {
                                "id": "spotter-message-1",
                                "type": "userMessage",
                                "clientId": "spotter:intervention:job-1",
                                "content": [
                                    {"type": "text", "text": "Verify the retry assumption"}
                                ],
                            },
                        },
                    }
                )
            )
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(endpoint, tmp_path / "sessions", ThreadStateStore())
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            recovery._record(
                TraceEvent(
                    "user_prompt",
                    {"prompt": "Fix the original bug"},
                    event_id="original-goal",
                    identity=state.identity,
                    connection_epoch=state.connection_epoch,
                )
            )
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            await recovery.steer(
                target,
                "Verify the retry assumption",
                control_id="spotter:intervention:job-1",
                review_job_id="job-1",
            )
            await _wait_until(
                lambda: any(
                    record.event.kind == "control_observed_in_turn"
                    for record in recovery.ingestor.records()
                )
            )
            await recovery.flush_control_telemetry()

            records = [record.event for record in recovery.ingestor.records()]
            prompt = next(
                event
                for event in records
                if event.kind == "user_prompt"
                and event.payload.get("client_user_message_id") == "spotter:intervention:job-1"
            )
            observed = next(event for event in records if event.kind == "control_observed_in_turn")
            current = recovery.thread_states.snapshots()[0]
            assert prompt.payload["input_origin"] == "spotter_supervision"
            assert prompt.payload["intervention_relation"] == "target_turn"
            assert observed.payload["outcome"] == "observed_in_turn"
            assert observed.payload["observed_input_event_id"] == prompt.event_id
            assert current.task.goal is not None
            assert current.task.goal.text == "Fix the original bug"
            assert current.supervision.interventions[-1].text == "Verify the retry assumption"

            recovery._record(
                TraceEvent(
                    "turn_completed",
                    {"status": "completed"},
                    event_id="turn-completed",
                    identity=state.identity,
                    connection_epoch=state.connection_epoch,
                )
            )
            attachment_id = state.identity.provenance.agent_attachment_id
            assert attachment_id is not None
            later_identity = recovery._runtime_identity(
                "thread-1",
                "turn-2",
                attachment_id,
            )
            recovery._record(
                TraceEvent(
                    "turn_started",
                    {},
                    event_id="turn-2-started",
                    identity=later_identity,
                    connection_epoch=state.connection_epoch,
                )
            )
            recovery._record(
                TraceEvent(
                    "user_prompt",
                    {"prompt": "Document the completed fix"},
                    event_id="later-user-goal",
                    identity=later_identity,
                    connection_epoch=state.connection_epoch,
                )
            )
            leaked = recovery._annotate_intervention_input(
                TraceEvent(
                    "user_prompt",
                    {"client_user_message_id": "spotter:intervention:job-1", "prompt": "old"},
                    event_id="leaked-advisory",
                    identity=later_identity,
                    connection_epoch=state.connection_epoch,
                )
            )
            recovery._record(leaked)
            leaked_observation = next(
                record.event
                for record in recovery.ingestor.records()
                if record.event.kind == "control_observed_outside_target"
            )
            assert leaked.payload["intervention_relation"] == "outside_target"
            assert leaked_observation.payload["outcome"] == "observed_outside_target"
            assert leaked_observation.payload["reason_code"] == "expired_advisory_visible"
            later_goal = recovery.thread_states.snapshots()[0].task.goal
            assert later_goal is not None
            assert later_goal.text == "Document the completed fix"
            await recovery.close()

    asyncio.run(scenario())


def test_reconciliation_keeps_multiple_threads_isolated(tmp_path: Path) -> None:
    async def scenario() -> None:
        controls: list[dict[str, Any]] = []

        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {
                    "data": [{"id": "thread-1"}, {"id": "thread-2"}],
                    "nextCursor": None,
                },
            )
            for thread_id, turn_id in (("thread-1", "turn-1"), ("thread-2", "turn-2")):
                read = await _receive(connection, "thread/resume")
                assert read["params"]["threadId"] == thread_id
                await _reply(
                    connection,
                    read,
                    {
                        "thread": {
                            "id": thread_id,
                            "turns": [{"id": turn_id, "status": "active"}],
                        }
                    },
                )
            for _ in range(2):
                request = await _receive(connection, "turn/steer")
                controls.append(request["params"])
                await _reply(connection, request, {"turnId": request["params"]["expectedTurnId"]})
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            store = ThreadStateStore()
            recovery = AppServerRecoveryLoop(endpoint, tmp_path / "sessions", store)
            jobs: list[ReviewerJob] = []
            recovery.set_review_job_callback(jobs.append)
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)

            states = sorted(
                store.snapshots(),
                key=lambda state: state.identity.provenance.agent_thread_id or "",
            )
            assert [state.identity.provenance.agent_thread_id for state in states] == [
                "thread-1",
                "thread-2",
            ]
            assert [state.identity.provenance.agent_turn_id for state in states] == [
                "turn-1",
                "turn-2",
            ]
            assert len({state.active_turn_id for state in states}) == 2
            assert len({state.identity.attachment_id for state in states}) == 2
            assert all(state.control_ready for state in states)

            for state in states:
                for index in range(2):
                    recovery._record(
                        TraceEvent(
                            "tool_result",
                            {"status": "failed", "server": "fixture", "tool": "lookup"},
                            event_id=f"{state.thread_id.value}:failure:{index}",
                            identity=state.identity,
                            connection_epoch=state.connection_epoch,
                        )
                    )
                await recovery.steer(
                    RuntimeControlTarget(state.identity, state.connection_epoch or 0),
                    "verify",
                    control_id=f"control-{state.thread_id.value}",
                )

            assert {job.thread_id for job in jobs} == {state.thread_id for state in states}
            assert {job.target_turn_id for job in jobs} == {
                state.active_turn_id for state in states
            }
            assert sorted(path.name for path in (tmp_path / "sessions").glob("*.jsonl")) == sorted(
                f"app-server-{state.thread_id.value}.jsonl" for state in states
            )
            assert [control["threadId"] for control in controls] == ["thread-1", "thread-2"]
            assert [control["expectedTurnId"] for control in controls] == ["turn-1", "turn-2"]
            await recovery.close()

    asyncio.run(scenario())


def test_unreachable_endpoint_backs_off_without_hiding_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        recovery = AppServerRecoveryLoop(
            "ws://127.0.0.1:1",
            tmp_path / "sessions",
            ThreadStateStore(),
            initial_backoff=10,
            maximum_backoff=10,
        )
        await recovery.start()
        await _wait_until(lambda: recovery.state == RecoveryState.BACKING_OFF)

        assert recovery.metrics.reconnect_failures == 1
        assert recovery.last_error is not None
        assert RecoveryState.DEGRADED in recovery.transitions
        await recovery.close()

    asyncio.run(scenario())


def test_unexpected_consumer_failure_forces_degraded_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        send_event = asyncio.Event()

        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(connection, listed, {"data": [], "nextCursor": None})
            await send_event.wait()
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "thread/status/changed",
                        "params": {"threadId": "thread-1", "status": "active"},
                    }
                )
            )
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(
                endpoint,
                tmp_path / "sessions",
                ThreadStateStore(),
                initial_backoff=10,
                maximum_backoff=10,
            )
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)

            def fail_record(event: object) -> None:
                raise RuntimeError("reducer exploded")

            monkeypatch.setattr(recovery, "_record", fail_record)
            send_event.set()
            await _wait_until(lambda: recovery.state == RecoveryState.BACKING_OFF)

            assert recovery.last_error is not None
            assert "event consumer failed" in recovery.last_error
            assert "reducer exploded" in recovery.last_error
            assert recovery.metrics.reconnect_failures == 1
            assert RecoveryState.DEGRADED in recovery.transitions
            await recovery.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("turn_status", "outcome", "reason_code"),
    [
        ("interrupted", "turn_aborted", "observed_interrupted_status"),
        ("completed", "turn_completed_otherwise", "target_completed_despite_interrupt"),
    ],
)
def test_accepted_interrupt_settles_on_the_observed_terminal_status(
    tmp_path: Path,
    turn_status: str,
    outcome: str,
    reason_code: str,
) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(connection, listed, {"data": [{"id": "thread-1"}], "nextCursor": None})
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "status": "active"}]}},
            )
            interrupt = await _receive(connection, "turn/interrupt")
            await _reply(connection, interrupt, {"turnId": "turn-1"})
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": turn_status},
                        },
                    }
                )
            )
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(endpoint, tmp_path / "sessions", ThreadStateStore())
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            await recovery.interrupt(target, control_id="spotter:interrupt:job-1")
            await _wait_until(
                lambda: any(
                    record.event.kind == "turn_completed" for record in recovery.ingestor.records()
                )
            )
            await recovery.flush_control_telemetry()

            controls = [
                record.event
                for record in recovery.ingestor.records()
                if record.event.payload.get("control_id") == "spotter:interrupt:job-1"
            ]
            assert [event.kind for event in controls] == [
                "control_dispatch_started",
                "control_rpc_accepted",
                "control_terminal",
            ]
            # A successful RPC is not evidence the turn stopped: only the observed
            # status separates a real abort from a turn that finished anyway.
            assert controls[-1].payload["outcome"] == outcome
            assert controls[-1].payload["reason_code"] == reason_code
            await recovery.close()

    asyncio.run(scenario())


def test_interrupt_after_a_final_answer_stays_unsettled_until_the_turn_ends(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(connection, listed, {"data": [{"id": "thread-1"}], "nextCursor": None})
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "status": "active"}]}},
            )
            interrupt = await _receive(connection, "turn/interrupt")
            await _reply(connection, interrupt, {"turnId": "turn-1"})
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(endpoint, tmp_path / "sessions", ThreadStateStore())
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            recovery._record(
                TraceEvent(
                    "agent_message",
                    {
                        "text": "The task is complete",
                        "phase": "final_answer",
                        "lifecycle": "completed",
                    },
                    event_id="final-answer",
                    identity=state.identity,
                    connection_epoch=state.connection_epoch,
                )
            )
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            await recovery.interrupt(target, control_id="spotter:interrupt:job-2")
            await recovery.flush_control_telemetry()

            controls = [
                record.event
                for record in recovery.ingestor.records()
                if record.event.payload.get("control_id") == "spotter:interrupt:job-2"
            ]
            # A final answer is a steer settlement signal, not an interrupt one:
            # the turn may still be running, so recovery must not treat this as
            # a settled abort.
            assert [event.kind for event in controls] == [
                "control_dispatch_started",
                "control_rpc_accepted",
            ]
            await recovery.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("turn_status", "outcome", "reason_code"),
    [
        ("completed", "turn_completed_otherwise", "target_completed_despite_interrupt"),
        ("interrupted", "turn_aborted", "observed_interrupted_status"),
    ],
)
def test_interrupt_settlement_does_not_depend_on_boundary_versus_ack_ordering(
    tmp_path: Path,
    turn_status: str,
    outcome: str,
    reason_code: str,
) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(connection, listed, {"data": [{"id": "thread-1"}], "nextCursor": None})
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "status": "active"}]}},
            )
            interrupt = await _receive(connection, "turn/interrupt")
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": turn_status},
                        },
                    }
                )
            )
            await _wait_until(
                lambda: any(
                    record.event.kind == "turn_completed" for record in recovery.ingestor.records()
                )
            )
            await _reply(connection, interrupt, {"turnId": "turn-1"})
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(endpoint, tmp_path / "sessions", ThreadStateStore())
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            await recovery.interrupt(target, control_id="spotter:interrupt:job-3")
            await recovery.flush_control_telemetry()

            terminals = [
                record.event
                for record in recovery.ingestor.records()
                if record.event.payload.get("control_id") == "spotter:interrupt:job-3"
                and record.event.kind == "control_terminal"
            ]
            # The boundary lands before the ack here, so settlement runs through
            # the post-ack path instead of the boundary path. Both must agree:
            # the outcome describes the turn, not which message arrived first.
            assert len(terminals) == 1
            assert terminals[0].payload["outcome"] == outcome
            assert terminals[0].payload["reason_code"] == reason_code
            await recovery.close()

    asyncio.run(scenario())


def test_interrupt_that_never_observes_a_terminal_turn_settles_as_unknown(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(connection, listed, {"data": [{"id": "thread-1"}], "nextCursor": None})
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "status": "active"}]}},
            )
            interrupt = await _receive(connection, "turn/interrupt")
            await _reply(connection, interrupt, {"turnId": "turn-1"})
            # The turn goes silent: no turn/completed will ever arrive.
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(
                endpoint,
                tmp_path / "sessions",
                ThreadStateStore(),
                interrupt_settlement_timeout=0.05,
            )
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            await recovery.interrupt(target, control_id="spotter:interrupt:silent")
            await _wait_until(
                lambda: any(
                    record.event.kind == "control_terminal"
                    and record.event.payload.get("control_id") == "spotter:interrupt:silent"
                    for record in recovery.ingestor.records()
                )
            )
            await recovery.flush_control_telemetry()

            terminal = next(
                record.event
                for record in recovery.ingestor.records()
                if record.event.kind == "control_terminal"
                and record.event.payload.get("control_id") == "spotter:interrupt:silent"
            )
            # Unknown, not aborted: the runtime never said the turn stopped, and
            # recovery must not infer that it did.
            assert terminal.payload["outcome"] == "unknown"
            assert terminal.payload["reason_code"] == "interrupt_settlement_timeout"
            await recovery.close()

    asyncio.run(scenario())


def test_observed_interrupt_settlement_preempts_the_timeout(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(connection, listed, {"data": [{"id": "thread-1"}], "nextCursor": None})
            read = await _receive(connection, "thread/resume")
            await _reply(
                connection,
                read,
                {"thread": {"id": "thread-1", "turns": [{"id": "turn-1", "status": "active"}]}},
            )
            interrupt = await _receive(connection, "turn/interrupt")
            await _reply(connection, interrupt, {"turnId": "turn-1"})
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "interrupted"},
                        },
                    }
                )
            )
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            recovery = AppServerRecoveryLoop(
                endpoint,
                tmp_path / "sessions",
                ThreadStateStore(),
                # Long enough that it cannot fire during this test: what is being
                # asserted is that the observed settlement retires the deadline,
                # not that one of them wins a race.
                interrupt_settlement_timeout=30,
            )
            await recovery.start()
            await _wait_until(lambda: recovery.state == RecoveryState.READY)
            state = recovery.thread_states.snapshots()[0]
            target = RuntimeControlTarget(state.identity, state.connection_epoch or 0)

            await recovery.interrupt(target, control_id="spotter:interrupt:observed")
            await _wait_until(
                lambda: any(
                    record.event.kind == "turn_completed" for record in recovery.ingestor.records()
                )
            )
            await recovery.flush_control_telemetry()

            assert recovery._interrupt_deadlines == {}

            terminals = [
                record.event
                for record in recovery.ingestor.records()
                if record.event.kind == "control_terminal"
                and record.event.payload.get("control_id") == "spotter:interrupt:observed"
            ]
            assert len(terminals) == 1
            assert terminals[0].payload["outcome"] == "turn_aborted"
            await recovery.close()

    asyncio.run(scenario())


def test_interrupt_settlement_timeout_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="interrupt settlement timeout"):
        AppServerRecoveryLoop(
            "ws://unused",
            tmp_path / "sessions",
            ThreadStateStore(),
            interrupt_settlement_timeout=0,
        )
