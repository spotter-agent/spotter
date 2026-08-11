from pathlib import Path

import pytest

from spotter.codex import CodexAdapter
from spotter.trace import TraceEvent, append_jsonl, read_jsonl


def test_trace_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    event = TraceEvent("file_edit", step=4, files=("src/a.py",), constraints=("small",))

    append_jsonl(path, event)

    assert list(read_jsonl(path)) == [event]


def test_trace_reader_reports_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"kind": "session_start"}\n{"broken"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 2"):
        list(read_jsonl(path))


def test_trace_reader_rejects_missing_kind(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="kind must be a non-empty string"):
        list(read_jsonl(path))


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ('{"kind": "tool_result", "payload": null}', "payload must be an object"),
        ('{"kind": "tool_result", "step": true}', "step must be an integer"),
        ('{"kind": "tool_result", "operation": 3}', "operation must be a string or null"),
        ('{"kind": "file_edit", "files": "src/app.py"}', "files must be an array"),
        ('{"kind": "file_edit", "constraints": ["small", 3]}', "constraints must be an array"),
    ],
)
def test_trace_reader_rejects_invalid_typed_fields(
    tmp_path: Path, record: str, message: str
) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(f'{record}\n', encoding="utf-8")

    with pytest.raises(ValueError, match=f"line 1: {message}"):
        list(read_jsonl(path))


def test_codex_adapter_normalizes_public_event_fields() -> None:
    event = CodexAdapter().normalize(
        {
            "type": "tool_result",
            "command": "pytest",
            "files": ["tests/test_app.py"],
            "output_summary": "passed",
            "validation": "passed",
            "payload": {"exit_code": 0},
        },
        7,
    )

    assert event == TraceEvent(
        "tool_result",
        {"exit_code": 0},
        step=7,
        operation="pytest",
        files=("tests/test_app.py",),
        result="passed",
        validation="passed",
    )
