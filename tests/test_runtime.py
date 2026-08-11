from spotter.codex import CodexAdapter
from spotter.config import MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.core import SpotterRuntime
from spotter.gates import Gate
from spotter.trace import TraceEvent


def _runtime(observation_only: bool) -> tuple[SpotterRuntime, CodexAdapter]:
    adapter = CodexAdapter()
    config = SpotterConfig(
        MainAgentConfig("codex"), ReviewerConfig("reviewer"), observation_only=observation_only
    )
    return SpotterRuntime(config, adapter, gate=Gate()), adapter


def test_runtime_forwards_normalized_events() -> None:
    runtime, adapter = _runtime(observation_only=True)
    event = TraceEvent("tool_result", {"exit_code": 0})

    decision = runtime.observe(event)

    assert decision.allowed
    assert adapter.events == [event]


def test_observation_mode_shadows_blocks_instead_of_enforcing() -> None:
    runtime, adapter = _runtime(observation_only=True)
    proposal = TraceEvent("tool_proposal", {"command": "git push --force"})

    decision = runtime.observe(proposal)

    assert decision.allowed  # never enforced in shadow mode
    assert [e.kind for e in adapter.events] == ["tool_proposal", "gate_shadow_block"]
    assert adapter.events[1].payload["rule"] == "git_push_force"


def test_active_mode_enforces_gate_block() -> None:
    runtime, adapter = _runtime(observation_only=False)
    proposal = TraceEvent("tool_proposal", {"command": "rm -rf /"})

    decision = runtime.observe(proposal)

    assert not decision.allowed
    assert [e.kind for e in adapter.events] == ["tool_proposal", "gate_block"]
