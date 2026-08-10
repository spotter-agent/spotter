"""Runtime-neutral passive supervision orchestration."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import asdict, dataclass, field

from spotter.adapters import AgentAdapter
from spotter.audit import AuditState
from spotter.config import SpotterConfig
from spotter.reviewer import DecisionType, ReviewDecision, Reviewer
from spotter.trace import TraceEvent


@dataclass
class Finding:
    step: int
    decision: ReviewDecision
    latency_ms: float
    token_usage: int | None = None
    human_judgment: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class SpotterRuntime:
    """Observe events and log advice; never block or mutate the main session."""

    config: SpotterConfig
    adapter: AgentAdapter
    reviewer: Reviewer | None = None
    state: AuditState = field(default_factory=AuditState)
    invocations: int = 0
    decision_counts: Counter[DecisionType] = field(default_factory=Counter)
    findings: list[Finding] = field(default_factory=list)

    def observe(self, event: TraceEvent) -> ReviewDecision | None:
        self.adapter.record(event)
        self.state.update(event)
        if self.reviewer is None:
            return None
        started = time.perf_counter()
        decision = self.reviewer.review(event, self.state.snapshot())
        latency = (time.perf_counter() - started) * 1000
        self.invocations += 1
        self.decision_counts[decision.decision] += 1
        if decision.decision is not DecisionType.CONTINUE:
            self.findings.append(Finding(event.step, decision, latency))
        return decision

    def telemetry(self) -> dict[str, object]:
        return {
            "reviewer_invocations": self.invocations,
            "decisions": {kind.value: self.decision_counts[kind] for kind in DecisionType},
            "findings": len(self.findings),
        }
