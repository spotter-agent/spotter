from spotter.codex import CodexAdapter
from spotter.config import MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.core import SpotterRuntime
from spotter.trace import TraceEvent


def test_runtime_forwards_normalized_events() -> None:
    adapter = CodexAdapter()
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig("reviewer"))
    runtime = SpotterRuntime(config, adapter)
    event = TraceEvent("tool_result", {"exit_code": 0})

    runtime.observe(event)

    assert adapter.events == [event]
