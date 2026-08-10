"""Runtime-neutral supervision orchestration."""

from dataclasses import dataclass

from spotter.adapters import AgentAdapter
from spotter.config import SpotterConfig
from spotter.trace import TraceEvent


@dataclass
class SpotterRuntime:
    """Receive normalized events without depending on a specific agent runtime."""

    config: SpotterConfig
    adapter: AgentAdapter

    def observe(self, event: TraceEvent) -> None:
        """Accept an event; active intervention is intentionally not implemented yet."""
        self.adapter.record(event)
