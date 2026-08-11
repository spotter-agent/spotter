import json
from pathlib import Path

import pytest

import spotter.experiment as experiment
from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.experiment import ArmResult, results_path, run_experiment, summarize
from spotter.hook import run_hook, spotter_home
from spotter.replay import ForkPlan


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))
    return tmp_path


def _fake_fork(counter: dict[str, int]) -> object:
    def fake(session_id: str, step: int, *, codex_home: object = None) -> ForkPlan:
        counter["n"] = counter.get("n", 0) + 1
        n = counter["n"]
        return ForkPlan(f"fork-{n}", step, f"/wt/{n}", f"/ro/{n}", "codex ...")

    return fake


def test_experiment_without_run_only_prepares(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    results = run_experiment("s1", 5, "check the stack trace", pairs=2)
    assert len(results) == 4  # 2 pairs x 2 arms
    assert {r.arm for r in results} == {"control", "guidance"}
    assert all(r.agent_exit is None for r in results)  # nothing executed


def test_results_carry_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #15 review P1: rows without conditions are not measurements."""
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    results = run_experiment("s1", 5, "look at the trace", check="pytest -q")
    rows = [json.loads(line) for line in results_path("s1", 5).read_text().splitlines()]
    meta = rows[0]
    assert meta["meta"] is True
    assert meta["guidance"] == "look at the trace"
    assert meta["check"] == "pytest -q"
    assert meta["sandbox"] and meta["timeout"] and meta["started_at"]
    # every row is linked to its conditions via the experiment id
    assert all(row["experiment_id"] == meta["experiment_id"] for row in rows[1:])
    assert all(r.experiment_id == meta["experiment_id"] for r in results)
    # a rerun with different conditions is distinguishable
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    run_experiment("s1", 5, "different guidance")
    rows2 = [json.loads(line) for line in results_path("s1", 5).read_text().splitlines()]
    metas = [r for r in rows2 if r.get("meta")]
    assert len(metas) == 2 and metas[0]["experiment_id"] != metas[1]["experiment_id"]


def test_results_path_is_traversal_safe() -> None:
    """PR #15 review P1: session_id is external input."""
    path = results_path("../../outside", 1)
    assert path.parent == spotter_home() / "experiments"
    assert ".." not in path.name


def test_arm_order_is_counterbalanced(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #15 review P1: control-always-first bakes order effects into results."""
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    prompts: list[str] = []

    def record_prompt(s: str, w: str, prompt: str, *, sandbox: str, timeout: int) -> int:
        prompts.append(prompt)
        return 0

    monkeypatch.setattr(experiment, "_run_arm", record_prompt)
    results = run_experiment("s1", 5, "hint", pairs=2, run=True)
    assert [r.arm for r in results] == ["control", "guidance", "guidance", "control"]
    assert prompts[0] == "Continue the task." and "hint" in prompts[1]
    assert "hint" in prompts[2] and prompts[3] == "Continue the task."


def test_empty_experiment_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #15 review P2: --pairs 0 must not succeed with zero data."""
    with pytest.raises(ValueError, match="pairs must be >= 1"):
        run_experiment("s1", 5, "hint", pairs=0)


def test_check_runs_in_each_fork_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    monkeypatch.setattr(experiment, "_run_arm", lambda s, w, p, *, sandbox, timeout: 0)
    checks: list[str] = []

    class FakeCompleted:
        returncode = 0

    def fake_subprocess_run(cmd: object, **kwargs: object) -> FakeCompleted:
        checks.append(str(kwargs.get("cwd")))
        return FakeCompleted()

    monkeypatch.setattr("spotter.experiment.subprocess.run", fake_subprocess_run)
    results = run_experiment("s1", 5, "hint", run=True, check="pytest -q")
    assert checks == ["/wt/1", "/wt/2"]
    assert all(r.check_exit == 0 for r in results)


def test_summarize_refuses_to_call_completion_success() -> None:
    ran_no_check = [ArmResult("e", 0, "control", "s", "/wt", 0, None)]
    assert "completion is not success" in summarize(ran_no_check)
    prepared = [ArmResult("e", 0, "guidance", "s", "/wt", None, None)]
    assert "not run" in summarize(prepared)


def _cadence_payload(kind: str, n: int) -> dict[str, object]:
    return {
        "hook_event_name": kind,
        "session_id": "cadence",
        "cwd": "/nonexistent",
        "tool_name": "Bash",
        "tool_use_id": f"c{n}",
        "tool_input": {"command": "true"},
    }


def test_cadence_counts_proposals_not_journal_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #15 review P0: results/prompts/verdicts consume steps; the cadence
    must key on proposals only. Realistic flow: proposal+result pairs."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "spotter.hook.subprocess.Popen", lambda cmd, **kw: spawned.append(list(cmd))
    )
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig(every_steps=2), GatesConfig())
    for n in range(4):
        run_hook(_cadence_payload("PreToolUse", n), config)  # proposals 1..4
        run_hook(_cadence_payload("PostToolUse", n), config)  # results consume steps too
    # proposals 2 and 4 hit the cadence; journal steps alone would drift
    assert len(spawned) == 2
    assert all("review" in cmd for cmd in spawned)


def test_cadence_forwards_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PR #15 review P0: the child must judge with the user's reviewer config."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "spotter.hook.subprocess.Popen", lambda cmd, **kw: spawned.append(list(cmd))
    )
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig(every_steps=1), GatesConfig())
    config_path = tmp_path / "spotter.toml"
    run_hook(_cadence_payload("PreToolUse", 0), config, config_path)
    assert spawned and "--config" in spawned[0]
    assert str(config_path) in spawned[0]


def test_cadence_recursion_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "spotter.hook.subprocess.Popen", lambda cmd, **kw: spawned.append(list(cmd))
    )
    monkeypatch.setenv("SPOTTER_DISABLE", "1")
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig(every_steps=1), GatesConfig())
    run_hook(_cadence_payload("PreToolUse", 0), config)
    assert spawned == []
