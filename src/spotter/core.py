"""Runtime-neutral supervision orchestration."""

from dataclasses import dataclass, field
from uuid import uuid4

from spotter.adapters import AgentAdapter
from spotter.config import SpotterConfig
from spotter.effects import effect_event
from spotter.gates import ALLOW, GATE_RULE_VERSION, Gate, GateDecision
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
            "rule_version": GATE_RULE_VERSION,
            "reason": decision.reason,
            "tool_use_id": event.payload.get("tool_use_id"),
            "tool": event.payload.get("tool"),
            "reversibility_class": event.payload.get("reversibility_class"),
            "effect_kind": event.payload.get("effect_kind"),
            "resource": event.payload.get("resource"),
            "effect_classifier": event.payload.get("effect_classifier"),
            "effect_reason": event.payload.get("effect_reason"),
            "effect_confidence": event.payload.get("effect_confidence"),
            "semantic_operation": event.payload.get("semantic_operation"),
        }
        if decision.allowed:
            if decision.rule:
                self.adapter.record(TraceEvent("gate_fail_open", gate_payload))
            return decision
        if self.config.observation_only:
            self.adapter.record(_block_event("gate_shadow_block", gate_payload))
            return ALLOW
        self.adapter.record(_block_event("gate_block", gate_payload))
        return decision


def _block_event(kind: str, payload: dict[str, object]) -> TraceEvent:
    supervision_event_id = f"spt-block-{uuid4().hex[:12]}"
    return TraceEvent(
        kind,
        {**payload, "supervision_event_id": supervision_event_id},
        event_id=f"spotter:supervision:{supervision_event_id}",
    )
