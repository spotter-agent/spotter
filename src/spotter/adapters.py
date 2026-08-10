"""Boundaries between Spotter core and agent runtimes."""

from typing import Protocol

from spotter.trace import TraceEvent


class AgentAdapter(Protocol):
    """Minimal interface supplied by a runtime-specific integration."""

    def record(self, event: TraceEvent) -> None:
        """Record a normalized trajectory event."""
