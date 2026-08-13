import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from websockets.asyncio.server import ServerConnection, serve

from spotter.runtime_connection import (
    AppServerRecoveryLoop,
    RecoveryState,
    RuntimeControlTarget,
    StaleControlTarget,
)
from spotter.thread_state import HistoryStatus, ThreadStateStore

Handler = Callable[[ServerConnection], Awaitable[None]]


def _message(raw: str | bytes) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(raw))


async def _receive(connection: ServerConnection, method: str) -> dict[str, Any]:
    message = _message(await connection.recv())
    assert message["method"] == method
    return message


async def _reply(connection: ServerConnection, request: dict[str, Any], result: Any) -> None:
    await connection.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}))


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
            assert (await recovery.steer(target, "verify"))["turnId"] == "turn-1"

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
                await recovery.steer(target, "late")
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
