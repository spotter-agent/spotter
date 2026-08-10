from spotter.codex import CodexAdapter
from spotter.config import MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.core import SpotterRuntime
from spotter.reviewer import DecisionType, HeuristicReviewer
from spotter.trace import TraceEvent


def test_runtime_forwards_normalized_events() -> None:
    adapter = CodexAdapter()
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig("reviewer"))
    runtime = SpotterRuntime(config, adapter)
    event = TraceEvent("tool_result", {"exit_code": 0})

    runtime.observe(event)

    assert adapter.events == [event]


def test_detects_unsupported_assumption_without_controlling_agent() -> None:
    adapter = CodexAdapter()
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig("independent-reviewer"))
    runtime = SpotterRuntime(config, adapter, HeuristicReviewer(config.reviewer.model))
    event = TraceEvent(
        "tool_proposal", {"unsupported_assumption": True}, step=3, operation="edit config"
    )

    decision = runtime.observe(event)

    assert decision is not None and decision.decision is DecisionType.VERIFY
    assert adapter.events == [event]
    assert runtime.findings[0].step == 3


def test_detects_repeated_failures_and_tracks_telemetry() -> None:
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig("reviewer"))
    runtime = SpotterRuntime(config, CodexAdapter(), HeuristicReviewer("reviewer"))

    runtime.observe(TraceEvent("tool_result", {"exit_code": 1}, step=1, result="failed"))
    decision = runtime.observe(TraceEvent("tool_result", {"exit_code": 1}, step=2, result="failed"))

    assert decision is not None and decision.decision is DecisionType.NUDGE
    assert runtime.telemetry()["reviewer_invocations"] == 2
    assert runtime.telemetry()["findings"] == 1
    assert runtime.telemetry()["decisions"] == {"CONTINUE": 1, "VERIFY": 0, "NUDGE": 1}


def test_audit_state_tracks_goal_constraints_files_and_validation() -> None:
    runtime = SpotterRuntime(
        SpotterConfig(MainAgentConfig("codex"), ReviewerConfig("reviewer")), CodexAdapter()
    )
    runtime.observe(TraceEvent("user_goal", intent="fix bug", constraints=("no deps",)))
    runtime.observe(TraceEvent("file_edit", files=("src/app.py",), validation="missing"))

    assert runtime.state.user_goal == "fix bug"
    assert runtime.state.constraints == {"no deps"}
    assert runtime.state.touched_files == {"src/app.py"}
    assert runtime.state.validation_status == "missing"


def test_reviewer_receives_an_immutable_audit_snapshot() -> None:
    runtime = SpotterRuntime(
        SpotterConfig(MainAgentConfig("codex"), ReviewerConfig("reviewer")), CodexAdapter()
    )
    runtime.observe(TraceEvent("user_goal", intent="fix bug", constraints=("no deps",)))

    snapshot = runtime.state.snapshot()

    assert snapshot.user_goal == "fix bug"
    assert snapshot.constraints == frozenset({"no deps"})
