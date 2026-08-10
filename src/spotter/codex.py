"""Placeholder Codex adapter kept outside the supervision core."""

from dataclasses import dataclass, field

from spotter.trace import TraceEvent


@dataclass
class CodexAdapter:
    """In-memory event sink until Codex event ingestion is implemented."""

    events: list[TraceEvent] = field(default_factory=list)

    def record(self, event: TraceEvent) -> None:
        self.events.append(event)
