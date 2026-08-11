"""Backend-neutral trajectory events and replay serialization."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    """The small, stable interface between agent adapters and Spotter."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    step: int = 0
    intent: str | None = None
    operation: str | None = None
    files: tuple[str, ...] = ()
    result: str | None = None
    validation: str | None = None
    constraints: tuple[str, ...] = ()

    @property
    def is_result(self) -> bool:
        return self.kind in {"tool_result", "validation"}

    @property
    def is_failed_result(self) -> bool:
        return self.is_result and (
            self.payload.get("success") is False or self.payload.get("exit_code") not in (None, 0)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TraceEvent:
        data = dict(value)
        kind = data.get("kind")
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")

        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        data["payload"] = payload

        step = data.get("step", 0)
        if not isinstance(step, int) or isinstance(step, bool):
            raise ValueError("step must be an integer")
        data["step"] = step

        for name in ("intent", "operation", "result", "validation"):
            field_value = data.get(name)
            if field_value is not None and not isinstance(field_value, str):
                raise ValueError(f"{name} must be a string or null")

        for name in ("files", "constraints"):
            field_value = data.get(name, ())
            if not isinstance(field_value, (list, tuple)) or not all(
                isinstance(item, str) for item in field_value
            ):
                raise ValueError(f"{name} must be an array of strings")
            data[name] = tuple(field_value)

        return cls(**data)


def read_jsonl(path: Path) -> Iterator[TraceEvent]:
    """Read a replayable trace, rejecting malformed records with their line number."""

    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("event must be an object")
                yield TraceEvent.from_dict(value)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(f"invalid trace at line {line_number}: {error}") from error


def append_jsonl(path: Path, event: TraceEvent) -> None:
    """Append one normalized event for later replay."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
