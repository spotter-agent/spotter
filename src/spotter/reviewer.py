"""Independent reviewer boundary and passive structured decisions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from spotter.audit import AuditSnapshot
from spotter.trace import TraceEvent


class DecisionType(StrEnum):
    CONTINUE = "CONTINUE"
    VERIFY = "VERIFY"
    NUDGE = "NUDGE"


@dataclass(frozen=True)
class ReviewDecision:
    decision: DecisionType
    target: str
    reason: str
    probe: str | None
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class Reviewer(Protocol):
    """A separately configured implementation may call any reviewer model."""

    def review(self, event: TraceEvent, state: AuditSnapshot) -> ReviewDecision:
        """Review the execution path without controlling the main agent."""


@dataclass
class HeuristicReviewer:
    """Deterministic reference reviewer useful for local runs and replay tests."""

    model: str

    def review(self, event: TraceEvent, state: AuditSnapshot) -> ReviewDecision:
        payload = event.payload
        if state.consecutive_failures >= 2:
            return ReviewDecision(
                DecisionType.NUDGE,
                "recent tool failures",
                "The same operation is failing repeatedly without new evidence.",
                "Change the diagnostic approach or inspect the first failure's root cause.",
                0.95,
            )
        if payload.get("unsupported_assumption") is True:
            return ReviewDecision(
                DecisionType.VERIFY,
                "current hypothesis",
                "A consequential assumption has not been supported by external evidence.",
                "Run the smallest check that can falsify the assumption before editing.",
                0.92,
            )
        if payload.get("scope_drift") is True:
            return ReviewDecision(
                DecisionType.NUDGE,
                "current task scope",
                "The operation is outside the stated goal or known constraints.",
                "Re-read the user goal and limit the next action to required files.",
                0.94,
            )
        if event.kind in {"file_edit", "diff"} and event.validation == "missing":
            return ReviewDecision(
                DecisionType.VERIFY,
                "edited files",
                "The edit has no adequate validation evidence.",
                "Run the narrowest relevant automated check.",
                0.9,
            )
        return ReviewDecision(
            DecisionType.CONTINUE, "trajectory", "No high-confidence issue.", None, 0.8
        )
