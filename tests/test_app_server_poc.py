import io
import json
import struct

from spotter.app_server_poc import AppServerClient, _client_frame, _read_exact


class ChunkedReader:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = iter(chunks)

    def read(self, size: int = -1) -> bytes:
        return next(self.chunks, b"")


def _server_frame(payload: bytes, *, opcode: int, final: bool) -> bytes:
    assert len(payload) < 126
    return bytes(((0x80 if final else 0) | opcode, len(payload))) + payload


def test_client_frame_is_masked_and_round_trips() -> None:
    payload = b"x" * 130
    frame = _client_frame(payload)
    assert frame[0] == 0x81
    assert frame[1] == 0xFE
    assert struct.unpack("!H", frame[2:4])[0] == len(payload)
    mask = frame[4:8]
    assert bytes(byte ^ mask[index % 4] for index, byte in enumerate(frame[8:])) == payload


def test_read_exact_collects_short_reads() -> None:
    assert _read_exact(ChunkedReader([b"ab", b"c", b"de"]), 5) == b"abcde"


def test_receive_reassembles_fragmented_text_frames() -> None:
    payload = json.dumps({"id": 1, "result": {"data": ["x"]}}).encode()
    stream = io.BytesIO(
        _server_frame(payload[:8], opcode=1, final=False)
        + _server_frame(payload[8:], opcode=0, final=True)
    )
    client = object.__new__(AppServerClient)
    client._stream = stream  # type: ignore[assignment]

    assert client._receive() == {"id": 1, "result": {"data": ["x"]}}
