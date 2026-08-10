"""Small runtime-independent trace representation."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
