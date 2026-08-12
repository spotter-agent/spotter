"""Tier-0 probe for attaching to a running Codex App Server."""

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
from pathlib import Path
from typing import Any, Protocol


class AppServerError(RuntimeError):
    pass


class _Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes | None: ...


def _read_exact(stream: _Readable, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise AppServerError("app-server connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _client_frame(payload: bytes, opcode: int = 1) -> bytes:
    mask = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = bytes((0x80 | opcode, 0x80 | size))
    elif size < 65536:
        header = bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", size)
    else:
        header = bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", size)
    return header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


class AppServerClient:
    def __init__(self, socket_path: Path, timeout: float = 10.0) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        try:
            connection.connect(str(socket_path))
        except OSError as error:
            connection.close()
            raise AppServerError(f"cannot connect to {socket_path}: {error}") from error
        self._stream = connection.makefile("rwb", buffering=0)
        self._next_id = 1
        self.notifications: list[dict[str, Any]] = []
        self._handshake()

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._stream.write(request.encode())
        status = self._stream.readline().decode(errors="replace").strip()
        headers: dict[str, str] = {}
        while line := self._stream.readline().decode(errors="replace").strip():
            name, _, value = line.partition(":")
            headers[name.lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if " 101 " not in f" {status} " or headers.get("sec-websocket-accept") != expected:
            raise AppServerError(f"websocket upgrade failed: {status}")

    def _send(self, message: dict[str, Any]) -> None:
        message = {"jsonrpc": "2.0", **message}
        self._stream.write(_client_frame(json.dumps(message, separators=(",", ":")).encode()))

    def _receive(self) -> dict[str, Any]:
        message = bytearray()
        while True:
            first, second = _read_exact(self._stream, 2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", _read_exact(self._stream, 2))[0]
            elif size == 127:
                size = struct.unpack("!Q", _read_exact(self._stream, 8))[0]
            mask = _read_exact(self._stream, 4) if second & 0x80 else None
            payload = _read_exact(self._stream, size)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 8:
                raise AppServerError("app-server closed the websocket")
            if opcode == 9:
                self._stream.write(_client_frame(payload, opcode=10))
                continue
            if opcode == 1:
                if message:
                    raise AppServerError("new text frame before fragmented message completed")
                message.extend(payload)
            elif opcode == 0:
                if not message:
                    raise AppServerError("continuation frame without an initial text frame")
                message.extend(payload)
            else:
                continue
            if final:
                value = json.loads(message)
                if isinstance(value, dict):
                    return value
                raise AppServerError("app-server returned a non-object message")

    def request(self, method: str, params: dict[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            message = self._receive()
            if message.get("id") == request_id:
                if "error" in message:
                    raise AppServerError(str(message["error"]))
                return message.get("result")
            self.notifications.append(message)

    def initialize(self) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "spotter",
                    "title": "Spotter Tier-0 PoC",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        if not isinstance(result, dict):
            raise AppServerError("initialize returned a non-object result")
        self._send({"method": "initialized", "params": {}})
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--thread-id")
    parser.add_argument("--turn-id")
    parser.add_argument("--steer", help="send text to an active turn (never done implicitly)")
    args = parser.parse_args()
    if args.steer and not (args.thread_id and args.turn_id):
        parser.error("--steer requires --thread-id and --turn-id")
    try:
        client = AppServerClient(args.socket, args.timeout)
        server = client.initialize()
        threads = client.request("thread/list", {"limit": args.limit, "sortKey": "updated_at"})
        result: dict[str, Any] = {"server": server, "threads": threads.get("data", [])}
        if args.steer:
            result["steer"] = client.request(
                "turn/steer",
                {
                    "threadId": args.thread_id,
                    "expectedTurnId": args.turn_id,
                    "input": [{"type": "text", "text": args.steer}],
                },
            )
        print(json.dumps(result, indent=2))
        return 0
    except (AppServerError, OSError, ValueError) as error:
        print(f"app-server PoC failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
