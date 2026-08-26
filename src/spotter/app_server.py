"""Async Codex App Server transport with explicit capability state."""

import asyncio
import contextlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from spotter.app_server_endpoint import display_app_server_endpoint, redact_app_server_error
from spotter.codex_host import (
    CodexHostVersion,
    CodexHostVersionError,
    validate_codex_host_version,
)

JsonObject = dict[str, Any]


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CapabilityStatus(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class AppServerCapability(StrEnum):
    OBSERVATION = "observation"
    THREAD_QUERY = "thread_query"
    STEER = "steer"
    INTERRUPT = "interrupt"
    ATOMIC_PRE_TOOL_VETO = "atomic_pre_tool_veto"


@dataclass(frozen=True)
class AppServerCapabilities:
    observation: CapabilityStatus
    thread_query: CapabilityStatus
    steer: CapabilityStatus
    interrupt: CapabilityStatus
    atomic_pre_tool_veto: CapabilityStatus


@dataclass(frozen=True)
class AppServerEvent:
    """A raw server notification retained for later Trace IR normalization."""

    method: str
    raw: Mapping[str, Any]


class AppServerError(RuntimeError):
    """Base error for the App Server boundary."""


class AppServerTransportError(AppServerError):
    """The server couldn't be reached or the live connection was lost."""


class AppServerProtocolError(AppServerError):
    """The peer sent malformed JSON-RPC data."""


class AppServerRpcError(AppServerError):
    """The server rejected a valid JSON-RPC request."""

    def __init__(self, method: str, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.method = method
        self.code = code
        self.message = message
        self.data = data


class ControlFailureReason(StrEnum):
    NO_ACTIVE_TURN = "no_active_turn"
    TURN_MISMATCH = "turn_mismatch"
    TURN_NOT_STEERABLE = "turn_not_steerable"


class AppServerControlError(AppServerRpcError):
    """A control RPC failed with a semantic reason isolated at the Codex adapter."""

    def __init__(
        self,
        method: str,
        code: int,
        message: str,
        reason: ControlFailureReason,
        data: Any = None,
    ) -> None:
        super().__init__(method, code, message, data)
        self.reason = reason


class UnsupportedAppServerCapability(AppServerRpcError):
    """The connected server doesn't implement the requested capability."""


# The `websockets` default is 1 MiB, and a real Codex App Server exceeded it on
# the first live connection: a 2,607,959-byte frame closed the socket with 1009
# before any thread could be observed. Thread history is the payload here, so the
# ceiling has to be sized for a long real session, not a synthetic one — but it
# stays a ceiling, because an unbounded frame is an unbounded allocation in a
# daemon that is supposed to be the dependable part of the system.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

_CAPABILITY_BY_METHOD = {
    "thread/list": AppServerCapability.THREAD_QUERY,
    "thread/read": AppServerCapability.THREAD_QUERY,
    "thread/resume": AppServerCapability.OBSERVATION,
    "turn/steer": AppServerCapability.STEER,
    "turn/interrupt": AppServerCapability.INTERRUPT,
}


class CodexAppServerClient:
    """One initialized JSON-RPC client for an external Codex App Server.

    A request timeout invalidates and closes the entire connection. This avoids accepting late
    responses after callers have already acted on a timeout. Server-initiated requests are exposed
    as events and rejected with method-not-found because this observer doesn't own approval flows.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        request_timeout: float = 10,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        self.endpoint = endpoint
        self.request_timeout = request_timeout
        self.headers = headers
        self.state = ConnectionState.DISCONNECTED
        self.server_info: Mapping[str, Any] | None = None
        self.host_version: CodexHostVersion | None = None
        self.last_error: AppServerError | None = None
        self._socket: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._failure_close: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._closing = False
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        # ponytail: #85 owns bounded backpressure once a real ingestion consumer exists.
        self._events: asyncio.Queue[AppServerEvent | AppServerError] = asyncio.Queue()
        self._capabilities = self._initial_capabilities()

    @staticmethod
    def _initial_capabilities() -> dict[AppServerCapability, CapabilityStatus]:
        capabilities = {capability: CapabilityStatus.UNKNOWN for capability in AppServerCapability}
        capabilities[AppServerCapability.ATOMIC_PRE_TOOL_VETO] = CapabilityStatus.UNAVAILABLE
        return capabilities

    @property
    def capabilities(self) -> AppServerCapabilities:
        return AppServerCapabilities(
            observation=self._capabilities[AppServerCapability.OBSERVATION],
            thread_query=self._capabilities[AppServerCapability.THREAD_QUERY],
            steer=self._capabilities[AppServerCapability.STEER],
            interrupt=self._capabilities[AppServerCapability.INTERRUPT],
            atomic_pre_tool_veto=self._capabilities[AppServerCapability.ATOMIC_PRE_TOOL_VETO],
        )

    async def connect(self) -> None:
        if self._socket is not None:
            raise AppServerTransportError("App Server client is already connected")
        self.state = ConnectionState.CONNECTING
        self._capabilities = self._initial_capabilities()
        self._closing = False
        self._failure_close = None
        self._closed.clear()
        self._events = asyncio.Queue()
        self.last_error = None
        try:
            self._socket = await websocket_connect(
                self.endpoint,
                additional_headers=self.headers,
                open_timeout=self.request_timeout,
                proxy=None,
                max_size=MAX_MESSAGE_BYTES,
            )
            self._reader = asyncio.create_task(self._read_messages())
            result = await self._request(
                "initialize",
                {
                    "clientInfo": {"name": "spotter", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            if not isinstance(result, dict):
                raise AppServerProtocolError("initialize returned a non-object result")
            self.server_info = result
            try:
                self.host_version = validate_codex_host_version(result.get("userAgent"))
            except CodexHostVersionError as error:
                raise AppServerProtocolError(f"incompatible Codex App Server: {error}") from error
            await self._notify("initialized")
            try:
                await self.list_threads(limit=1)
            except UnsupportedAppServerCapability:
                self.state = ConnectionState.DEGRADED
            else:
                if self._closed.is_set():
                    raise self.last_error or AppServerTransportError(
                        "App Server connection closed during initialization"
                    )
                self._capabilities[AppServerCapability.OBSERVATION] = CapabilityStatus.AVAILABLE
                self.state = ConnectionState.CONNECTED
        except AppServerError as error:
            await self._abort_connect()
            self.last_error = error
            raise
        except (OSError, TimeoutError, WebSocketException) as error:
            detail = redact_app_server_error(error, self.endpoint)
            transport_error = AppServerTransportError(
                f"could not connect to {display_app_server_endpoint(self.endpoint)}: {detail}"
            )
            await self._abort_connect()
            self.last_error = transport_error
            raise transport_error from error

    async def _abort_connect(self) -> None:
        await self.disconnect()
        self.state = ConnectionState.UNAVAILABLE

    async def disconnect(self) -> None:
        self._closing = True
        was_closed = self._closed.is_set()
        socket, reader = self._socket, self._reader
        self._socket = None
        self._reader = None
        if socket is not None:
            await socket.close()
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        if self._failure_close is not None:
            await self._failure_close
            self._failure_close = None
        disconnect_error = AppServerTransportError("App Server client disconnected")
        self._fail_pending(disconnect_error)
        if not was_closed:
            self._events.put_nowait(disconnect_error)
        self.server_info = None
        self.host_version = None
        self.last_error = None
        self.state = ConnectionState.DISCONNECTED
        self._closed.set()

    async def next_event(self) -> AppServerEvent:
        """Return the next server message or raise once the connection ends."""

        if self._closed.is_set() and self._events.empty():
            raise self.last_error or AppServerTransportError("App Server client disconnected")
        event = await self._events.get()
        if isinstance(event, AppServerError):
            raise event
        return event

    async def wait_closed(self) -> AppServerError | None:
        """Wait for intentional shutdown or connection loss."""

        await self._closed.wait()
        if self._failure_close is not None:
            await self._failure_close
        return self.last_error

    async def list_threads(
        self, *, limit: int = 100, cursor: str | None = None
    ) -> Mapping[str, Any]:
        params: JsonObject = {"limit": limit, "sortKey": "updated_at"}
        if cursor is not None:
            params["cursor"] = cursor
        return await self._object_request("thread/list", params)

    async def read_thread(
        self, thread_id: str, *, include_turns: bool = False
    ) -> Mapping[str, Any]:
        return await self._object_request(
            "thread/read", {"threadId": thread_id, "includeTurns": include_turns}
        )

    async def resume_thread(self, thread_id: str) -> Mapping[str, Any]:
        return await self._object_request("thread/resume", {"threadId": thread_id})

    async def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        cwd: str | None = None,
        client_user_message_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Start a turn without exposing Codex JSON-RPC shapes to callers."""

        if not text.strip():
            raise ValueError("turn input must be non-empty")
        params: JsonObject = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if cwd is not None:
            if not cwd.strip():
                raise ValueError("cwd must be non-empty")
            params["cwd"] = cwd
            params["runtimeWorkspaceRoots"] = [cwd]
        if client_user_message_id is not None:
            if not client_user_message_id.strip():
                raise ValueError("client_user_message_id must be non-empty")
            params["clientUserMessageId"] = client_user_message_id
        return await self._object_request("turn/start", params)

    async def steer(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
        *,
        client_user_message_id: str | None = None,
    ) -> Mapping[str, Any]:
        params: JsonObject = {
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": text}],
        }
        if client_user_message_id is not None:
            if not client_user_message_id.strip():
                raise ValueError("client_user_message_id must be non-empty")
            params["clientUserMessageId"] = client_user_message_id
        return await self._object_request("turn/steer", params)

    async def interrupt(self, thread_id: str, turn_id: str) -> Mapping[str, Any]:
        return await self._object_request(
            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
        )

    async def _object_request(self, method: str, params: JsonObject) -> Mapping[str, Any]:
        result = await self._request(method, params)
        if not isinstance(result, dict):
            self._raise_protocol_error(f"{method} returned a non-object result")
        return result

    async def _request(self, method: str, params: JsonObject) -> Any:
        socket = self._socket
        if socket is None or self._closed.is_set():
            raise self.last_error or AppServerTransportError("App Server client is not connected")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await socket.send(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            )
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
        except TimeoutError as error:
            self._pending.pop(request_id, None)
            transport_error = AppServerTransportError(f"{method} timed out")
            self._connection_failed(transport_error)
            raise transport_error from error
        except ConnectionClosed as error:
            self._pending.pop(request_id, None)
            transport_error = AppServerTransportError(f"connection lost during {method}")
            self._connection_failed(transport_error)
            raise transport_error from error
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            future.cancel()
            raise

        rpc_error = response.get("error")
        if rpc_error is not None:
            if not isinstance(rpc_error, dict):
                self._raise_protocol_error(f"{method} returned a malformed error")
            code = rpc_error.get("code")
            message = rpc_error.get("message")
            if not isinstance(code, int) or not isinstance(message, str):
                self._raise_protocol_error(f"{method} returned a malformed error")
            capability = _CAPABILITY_BY_METHOD.get(method)
            if code == -32601 and capability is not None:
                self._capabilities[capability] = CapabilityStatus.UNAVAILABLE
                unsupported = UnsupportedAppServerCapability(
                    method, code, message, rpc_error.get("data")
                )
                self.last_error = unsupported
                if self.state == ConnectionState.CONNECTED:
                    self.state = ConnectionState.DEGRADED
                raise unsupported
            data = rpc_error.get("data")
            if (reason := _control_failure_reason(method, message, data)) is not None:
                raise AppServerControlError(method, code, message, reason, data)
            raise AppServerRpcError(method, code, message, data)

        capability = _CAPABILITY_BY_METHOD.get(method)
        if capability is not None:
            self._capabilities[capability] = CapabilityStatus.AVAILABLE
        if "result" not in response:
            self._raise_protocol_error(f"{method} response has no result")
        return response["result"]

    async def _notify(self, method: str, params: JsonObject | None = None) -> None:
        socket = self._socket
        if socket is None:
            raise AppServerTransportError("App Server client is not connected")
        message: JsonObject = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await socket.send(json.dumps(message))

    async def _read_messages(self) -> None:
        socket = self._socket
        if socket is None:
            return
        try:
            async for raw in socket:
                message = self._decode_message(raw)
                request_id = message.get("id")
                method = message.get("method")
                if (
                    isinstance(request_id, int)
                    and not isinstance(request_id, bool)
                    and method is None
                ):
                    future = self._pending.pop(request_id, None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif isinstance(method, str):
                    if request_id is not None:
                        if not self._is_request_id(request_id):
                            raise AppServerProtocolError("server request has an invalid id")
                        await socket.send(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": request_id,
                                    "error": {
                                        "code": -32601,
                                        "message": f"Spotter does not handle {method} requests",
                                    },
                                }
                            )
                        )
                    await self._events.put(AppServerEvent(method, message))
                else:
                    raise AppServerProtocolError("received an invalid JSON-RPC message")
        except asyncio.CancelledError:
            raise
        except ConnectionClosed as error:
            if not self._closing:
                self._connection_failed(
                    AppServerTransportError(f"App Server connection closed: {error}")
                )
        except AppServerProtocolError as error:
            self._connection_failed(error)
        else:
            if not self._closing:
                self._connection_failed(AppServerTransportError("App Server connection closed"))

    @staticmethod
    def _decode_message(raw: str | bytes) -> JsonObject:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise AppServerProtocolError("received invalid JSON from App Server") from error
        if not isinstance(message, dict):
            raise AppServerProtocolError("received a non-object JSON-RPC message")
        return cast(JsonObject, message)

    def _connection_failed(self, error: AppServerError) -> None:
        if self._closed.is_set():
            return
        self.last_error = error
        self.state = ConnectionState.DEGRADED
        self._fail_pending(error)
        self._events.put_nowait(error)
        self._closed.set()
        if self._socket is not None:
            self._failure_close = asyncio.create_task(self._socket.close())

    def _raise_protocol_error(self, message: str) -> NoReturn:
        error = AppServerProtocolError(message)
        self._connection_failed(error)
        raise error

    def _fail_pending(self, error: AppServerError) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    @staticmethod
    def _is_request_id(value: Any) -> bool:
        return isinstance(value, (int, str)) and not isinstance(value, bool)


def _control_failure_reason(method: str, message: str, data: Any) -> ControlFailureReason | None:
    if method != "turn/steer":
        return None
    if isinstance(data, Mapping):
        info = data.get("codexErrorInfo")
        if isinstance(info, Mapping) and "activeTurnNotSteerable" in info:
            return ControlFailureReason.TURN_NOT_STEERABLE
    normalized = " ".join(message.casefold().split())
    if normalized == "no active turn to steer" or (
        normalized.startswith("cannot steer conversation ")
        and normalized.endswith(" because its active turn already ended")
    ):
        return ControlFailureReason.NO_ACTIVE_TURN
    if normalized.startswith("expected active turn id ") and " but found " in normalized:
        return ControlFailureReason.TURN_MISMATCH
    return None
