"""Runtime-neutral supervision orchestration."""

from dataclasses import dataclass, field

from spotter.adapters import AgentAdapter
from spotter.config import SpotterConfig
from spotter.effects import effect_event
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

    def observe(self, event: TraceEvent, decision: GateDecision | None = None) -> GateDecision:
        if decision is None:
            decision = self.gate.check(event)
        self.adapter.record(event)
        effect = effect_event(event)
        if effect is not None:
            self.adapter.record(effect)
        # Concurrent hook processes can interleave records between the proposal
        # and its gate event, so gate events carry the trigger identity instead
        # of relying on journal adjacency (PR #12 review, P1).
        gate_payload = {
            "rule": decision.rule,
            "reason": decision.reason,
            "tool_use_id": event.payload.get("tool_use_id"),
            "tool": event.payload.get("tool"),
        }
        if decision.allowed:
            if decision.rule:
                self.adapter.record(TraceEvent("gate_fail_open", gate_payload))
            return decision
        if self.config.observation_only:
            self.adapter.record(TraceEvent("gate_shadow_block", gate_payload))
            return ALLOW
        self.adapter.record(TraceEvent("gate_block", gate_payload))
        return decision
