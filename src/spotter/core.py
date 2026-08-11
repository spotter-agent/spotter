"""Runtime-neutral supervision orchestration."""

from dataclasses import dataclass, field

from spotter.adapters import AgentAdapter
from spotter.config import SpotterConfig
from spotter.gates import ALLOW, Gate, GateDecision
from spotter.trace import TraceEvent


@dataclass
class SpotterRuntime:
    """Receive normalized events; enforce deterministic gates.

    In observation-only mode a would-be block is recorded as a shadow event
    instead of enforced — the gate's false-positive rate must be measured
    before it is allowed to stop anyone's work (plan Q7).
    """

    config: SpotterConfig
    adapter: AgentAdapter
    gate: Gate = field(default_factory=Gate)

    def observe(self, event: TraceEvent) -> GateDecision:
        decision = self.gate.check(event)
        self.adapter.record(event)
        if decision.allowed:
            if decision.rule:
                self.adapter.record(
                    TraceEvent(
                        "gate_fail_open",
                        {"rule": decision.rule, "reason": decision.reason},
                    )
                )
            return decision
        if self.config.observation_only:
            self.adapter.record(
                TraceEvent(
                    "gate_shadow_block",
                    {"rule": decision.rule, "reason": decision.reason},
                )
            )
            return ALLOW
        self.adapter.record(
            TraceEvent("gate_block", {"rule": decision.rule, "reason": decision.reason})
        )
        return decision
