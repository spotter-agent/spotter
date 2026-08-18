import json
import tempfile
from pathlib import Path

from ledger import LedgerError, ReadResult, read_ledger


def payload(record_id: str, value: int, schema_version: int = 1) -> bytes:
    return json.dumps(
        {"schema_version": schema_version, "record_id": record_id, "value": value},
        separators=(",", ":"),
    ).encode()


def read(data: bytes) -> ReadResult:
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "ledger.jsonl"
        path.write_bytes(data)
        return read_ledger(path)


def rejects(data: bytes, message: str) -> None:
    try:
        read(data)
    except LedgerError as error:
        assert message in str(error)
    else:
        raise AssertionError("invalid ledger should be rejected")


first = payload("one", 1)
second = payload("two", 2)

complete = read(first + b"\n" + second + b"\n")
assert [record["record_id"] for record in complete.records] == ["one", "two"]
assert complete.repair_offset == len(first) + len(second) + 2

valid_without_newline = read(first + b"\n" + second)
assert len(valid_without_newline.records) == 2
assert valid_without_newline.repair_offset == len(first) + len(second) + 1

torn = read(first + b"\n" + b'{"schema_version":1,"record_id":"two"')
assert [record["record_id"] for record in torn.records] == ["one"]
assert torn.repair_offset == len(first) + 1

invalid_utf8_tail = read(first + b"\n" + b"\xff")
assert [record["record_id"] for record in invalid_utf8_tail.records] == ["one"]
assert invalid_utf8_tail.repair_offset == len(first) + 1

rejects(first + b"\nnot-json\n" + second + b"\n", "corrupt record")
rejects(first + b"\nnot-json\n", "corrupt record")
rejects(first + b"\n\n", "blank record")
rejects(payload("future", 1, schema_version=2), "unsupported schema")
rejects(b'{"schema_version":1,"record_id":"","value":1}', "record_id")
