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
    AppServerControlError,
    AppServerRpcError,
    AppServerTransportError,
)
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
            read = await _receive(connection, "thread/read")
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
            read = await _receive(connection, "thread/read")
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
            read = await _receive(connection, "thread/read")
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
            read = await _receive(connection, "thread/read")
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
            read = await _receive(connection, "thread/read")
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
            read = await _receive(connection, "thread/read")
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


def test_intervention_input_is_correlated_without_replacing_user_goal(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _initialize(connection)
            listed = await _receive(connection, "thread/list")
            await _reply(
                connection,
                listed,
                {"data": [{"id": "thread-1"}], "nextCursor": None},
            )
            read = await _receive(connection, "thread/read")
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
            leaked = recovery._annotate_intervention_input(
                TraceEvent(
                    "user_prompt",
                    {"client_user_message_id": "spotter:intervention:job-1", "prompt": "old"},
                    event_id="leaked-advisory",
                    identity=state.identity,
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
            assert recovery.thread_states.snapshots()[0].task.goal.text == "Fix the original bug"  # type: ignore[union-attr]
            await recovery.close()

    asyncio.run(scenario())


def test_reconciliation_keeps_multiple_threads_isolated(tmp_path: Path) -> None:
    async def scenario() -> None:
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
                read = await _receive(connection, "thread/read")
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
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            store = ThreadStateStore()
            recovery = AppServerRecoveryLoop(endpoint, tmp_path / "sessions", store)
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
            assert all(state.control_ready for state in states)
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
