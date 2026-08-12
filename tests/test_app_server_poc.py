import struct

from spotter.app_server_poc import _client_frame


def test_client_frame_is_masked_and_round_trips() -> None:
    payload = b"x" * 130
    frame = _client_frame(payload)
    assert frame[0] == 0x81
    assert frame[1] == 0xFE
    assert struct.unpack("!H", frame[2:4])[0] == len(payload)
    mask = frame[4:8]
    assert bytes(byte ^ mask[index % 4] for index, byte in enumerate(frame[8:])) == payload
