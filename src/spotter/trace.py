"""Small runtime-independent trace representation."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from spotter.identity import RuntimeIdentity


@dataclass(frozen=True)
class TraceProvenance:
    """Reference to the transport event without leaking its wire shape into core policy."""

    source: str
    method: str | None = None


@dataclass(frozen=True)
class TraceEvent:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str | None = None
    occurred_at: float | None = None
    identity: RuntimeIdentity | None = None
    operation_id: str | None = None
    item_id: str | None = None
    provenance: TraceProvenance | None = None
    connection_epoch: int | None = None
    arrival_seq: int | None = None


# Judgment happens on semantic segments, enforcement on tool boundaries (plan Q8).
# Event kinds not listed here are real but carry no segment signal; they fold into
# the surrounding segment instead of breaking it.
_SEGMENT_BY_KIND = {
    "reasoning_summary": "hypothesis",
    "plan": "hypothesis",
    "agent_message": "hypothesis",
    "file_read": "evidence",
    "search": "evidence",
    "command_result": "evidence",
    "tool_result": "evidence",
    "patch": "commit",
    "file_edit": "commit",
    "test_result": "validation",
}


@dataclass(frozen=True)
class Segment:
    """A run of consecutive events with one semantic role."""

    kind: str
    start: int  # first event index, inclusive
    end: int  # last event index, inclusive


def segment_events(events: Sequence[TraceEvent]) -> list[Segment]:
    """Aggregate raw events into semantic segments.

    Unknown kinds do not start or end a segment; a trajectory of only unknown
    kinds yields no segments rather than a fake one.
    """
    segments: list[Segment] = []
    for index, event in enumerate(events):
        kind = _SEGMENT_BY_KIND.get(event.kind)
        if kind is None:
            continue
        if segments and segments[-1].kind == kind:
            segments[-1] = Segment(kind, segments[-1].start, index)
        else:
            segments.append(Segment(kind, index, index))
    return segments
