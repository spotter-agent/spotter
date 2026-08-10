"""Reviewer boundary for future model-backed trajectory review."""

from typing import Protocol

from spotter.trace import TraceEvent


class Reviewer(Protocol):
    def review(self, event: TraceEvent) -> str:
        """Return a structured decision for a normalized event."""
