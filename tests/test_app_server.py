import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

from websockets.asyncio.server import ServerConnection, serve

from spotter.app_server import (
    AppServerProtocolError,
    AppServerTransportError,
    CapabilityStatus,
    CodexAppServerClient,
    ConnectionState,
    UnsupportedAppServerCapability,
)

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
    assert request["params"]["capabilities"] == {"experimentalApi": True}
    await _reply(
        connection,
        request,
        {
            "codexHome": "/tmp/codex",
            "platformFamily": "unix",
            "platformOs": "macos",
            "userAgent": "codex_cli_rs/0.147.0",
        },
    )
    await _receive(connection, "initialized")


@asynccontextmanager
async def _server(handler: Handler) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}"


async def _ready(connection: ServerConnection) -> None:
    await _initialize(connection)
    request = await _receive(connection, "thread/list")
    await _reply(connection, request, {"data": [], "nextCursor": None})


def test_connect_negotiates_observation_and_preserves_raw_events() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _ready(connection)
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
            client = CodexAppServerClient(endpoint)
            await client.connect()
            event = await client.next_event()
            assert client.state == ConnectionState.CONNECTED
            assert client.capabilities.observation == CapabilityStatus.AVAILABLE
            assert client.capabilities.thread_query == CapabilityStatus.AVAILABLE
            assert client.capabilities.steer == CapabilityStatus.UNKNOWN
            assert client.capabilities.atomic_pre_tool_veto == CapabilityStatus.UNAVAILABLE
            assert event.method == "turn/started"
            assert event.raw["params"] == {"threadId": "thread-1", "turn": {"id": "turn-1"}}
            await client.disconnect()

    asyncio.run(scenario())


def test_thread_queries_and_controls_hide_codex_method_names() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _ready(connection)
            for method, result in [
                ("thread/read", {"thread": {"id": "thread-1"}}),
                ("thread/resume", {"thread": {"id": "thread-1"}}),
                ("turn/steer", {"turnId": "turn-1"}),
                ("turn/interrupt", {}),
            ]:
                request = await _receive(connection, method)
                if method == "turn/steer":
                    assert request["params"] == {
                        "threadId": "thread-1",
                        "expectedTurnId": "turn-1",
                        "input": [{"type": "text", "text": "verify this"}],
                    }
                await _reply(connection, request, result)
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            client = CodexAppServerClient(endpoint)
            await client.connect()
            assert (await client.read_thread("thread-1"))["thread"] == {"id": "thread-1"}
            await client.resume_thread("thread-1")
            assert (await client.steer("thread-1", "turn-1", "verify this"))["turnId"] == "turn-1"
            await client.interrupt("thread-1", "turn-1")
            assert client.capabilities.observation == CapabilityStatus.AVAILABLE
            assert client.capabilities.steer == CapabilityStatus.AVAILABLE
            assert client.capabilities.interrupt == CapabilityStatus.AVAILABLE
            await client.disconnect()

    asyncio.run(scenario())


def test_concurrent_requests_are_matched_by_id() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _ready(connection)
            first = await _receive(connection, "thread/read")
            second = await _receive(connection, "thread/read")
            await _reply(connection, second, {"thread": {"id": second["params"]["threadId"]}})
            await _reply(connection, first, {"thread": {"id": first["params"]["threadId"]}})
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            client = CodexAppServerClient(endpoint)
            await client.connect()
            first, second = await asyncio.gather(
                client.read_thread("thread-1"), client.read_thread("thread-2")
            )
            assert first["thread"] == {"id": "thread-1"}
            assert second["thread"] == {"id": "thread-2"}
            await client.disconnect()

    asyncio.run(scenario())


def test_server_request_is_rejected_without_blocking_the_server() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _ready(connection)
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "approval-1",
                        "method": "commandExecution/requestApproval",
                        "params": {"threadId": "thread-1"},
                    }
                )
            )
            response = _message(await connection.recv())
            assert response["id"] == "approval-1"
            assert response["error"]["code"] == -32601
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            client = CodexAppServerClient(endpoint)
            await client.connect()
            event = await client.next_event()
            assert event.method == "commandExecution/requestApproval"
            await client.disconnect()

    asyncio.run(scenario())


def test_missing_capability_degrades_only_that_surface() -> None:
    async def scenario() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _ready(connection)
            request = await _receive(connection, "turn/steer")
            await connection.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32601, "message": "method not found"},
                    }
                )
            )
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            client = CodexAppServerClient(endpoint)
            await client.connect()
            try:
                await client.steer("thread-1", "turn-1", "verify")
            except UnsupportedAppServerCapability as error:
                assert error.method == "turn/steer"
            else:
                raise AssertionError("missing turn/steer wasn't reported")
            assert client.state == ConnectionState.DEGRADED
            assert client.capabilities.observation == CapabilityStatus.AVAILABLE
            assert client.capabilities.steer == CapabilityStatus.UNAVAILABLE
            assert client.capabilities.interrupt == CapabilityStatus.UNKNOWN
            await client.disconnect()

    asyncio.run(scenario())


def test_reconnect_renegotiates_changed_capability() -> None:
    async def scenario() -> None:
        connection_number = 0

        async def handler(connection: ServerConnection) -> None:
            nonlocal connection_number
            connection_number += 1
            await _ready(connection)
            request = await _receive(connection, "turn/steer")
            if connection_number == 1:
                await connection.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "error": {"code": -32601, "message": "method not found"},
                        }
                    )
                )
            else:
                await _reply(connection, request, {"turnId": "turn-1"})
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            client = CodexAppServerClient(endpoint)
            await client.connect()
            with suppress(UnsupportedAppServerCapability):
                await client.steer("thread-1", "turn-1", "verify")
            await client.disconnect()

            await client.connect()
            await client.steer("thread-1", "turn-1", "verify")
            assert client.capabilities.steer == CapabilityStatus.AVAILABLE
            await client.disconnect()

    asyncio.run(scenario())


def test_disconnect_and_protocol_failure_are_structured() -> None:
    async def disconnected() -> None:
        client = CodexAppServerClient("ws://127.0.0.1:1", request_timeout=0.1)
        try:
            await client.connect()
        except AppServerTransportError:
            pass
        else:
            raise AssertionError("unreachable server wasn't reported")
        assert client.state == ConnectionState.UNAVAILABLE

    async def malformed() -> None:
        async def handler(connection: ServerConnection) -> None:
            await connection.recv()
            await connection.send("not json")
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            client = CodexAppServerClient(endpoint)
            try:
                await client.connect()
            except AppServerProtocolError:
                pass
            else:
                raise AssertionError("malformed JSON wasn't reported")
            assert client.state == ConnectionState.UNAVAILABLE

    async def clean_close() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _ready(connection)
            request = await _receive(connection, "thread/read")
            await _reply(connection, request, {"thread": {"id": "thread-1"}})
            await connection.close()

        async with _server(handler) as endpoint:
            client = CodexAppServerClient(endpoint)
            await client.connect()
            await client.read_thread("thread-1")
            error = await client.wait_closed()
            assert isinstance(error, AppServerTransportError)
            assert client.state == ConnectionState.DEGRADED
            try:
                await client.next_event()
            except AppServerTransportError:
                pass
            else:
                raise AssertionError("event consumer wasn't released on disconnect")
            try:
                await client.next_event()
            except AppServerTransportError:
                pass
            else:
                raise AssertionError("closed event stream became readable again")
            await client.disconnect()

    async def malformed_result() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _ready(connection)
            request = await _receive(connection, "thread/read")
            await _reply(connection, request, None)
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            client = CodexAppServerClient(endpoint)
            await client.connect()
            try:
                await client.read_thread("thread-1")
            except AppServerProtocolError:
                pass
            else:
                raise AssertionError("malformed result wasn't reported")
            assert isinstance(client.last_error, AppServerProtocolError)
            assert client.state == ConnectionState.DEGRADED
            await client.disconnect()

    async def request_timeout() -> None:
        async def handler(connection: ServerConnection) -> None:
            await _ready(connection)
            await _receive(connection, "thread/read")
            await connection.wait_closed()

        async with _server(handler) as endpoint:
            client = CodexAppServerClient(endpoint, request_timeout=0.1)
            await client.connect()
            try:
                await client.read_thread("thread-1")
            except AppServerTransportError as error:
                assert "timed out" in str(error)
            else:
                raise AssertionError("request timeout wasn't reported")
            assert client.state == ConnectionState.DEGRADED
            assert isinstance(await client.wait_closed(), AppServerTransportError)
            try:
                await client.list_threads()
            except AppServerTransportError:
                pass
            else:
                raise AssertionError("failed connection accepted another request")
            await client.disconnect()

    asyncio.run(disconnected())
    asyncio.run(malformed())
    asyncio.run(clean_close())
    asyncio.run(malformed_result())
    asyncio.run(request_timeout())
