import json
from pathlib import Path

import pytest

import spotter.experiment as experiment
from spotter.experiment import ArmResult, results_path, run_experiment, summarize
from spotter.hook import run_hook
from spotter.replay import ForkPlan


@pytest.fixture(autouse=True)
def spotter_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    rows = [json.loads(line) for line in results_path("s1", 5).read_text().splitlines()]
    assert len(rows) == 4  # every fork journaled immediately


def test_experiment_run_executes_both_arms_and_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    prompts: list[str] = []

    def fake_run_arm(
        session: str, worktree: str, prompt: str, *, sandbox: str, timeout: int
    ) -> int:
        prompts.append(prompt)
        return 0

    checks: list[str] = []

    class FakeCompleted:
        returncode = 0

    def fake_subprocess_run(cmd: object, **kwargs: object) -> FakeCompleted:
        checks.append(str(kwargs.get("cwd")))
        return FakeCompleted()

    monkeypatch.setattr(experiment, "_run_arm", fake_run_arm)
    monkeypatch.setattr("spotter.experiment.subprocess.run", fake_subprocess_run)

    results = run_experiment("s1", 5, "look at the trace", run=True, check="pytest -q")
    assert prompts == ["Continue the task.", "Continue the task. look at the trace"]
    assert checks == ["/wt/1", "/wt/2"]  # check ran in each fork worktree
    assert all(r.check_exit == 0 for r in results)


def test_summarize_refuses_to_call_completion_success() -> None:
    ran_no_check = [ArmResult(0, "control", "s", "/wt", 0, None)]
    assert "completion is not success" in summarize(ran_no_check)
    prepared = [ArmResult(0, "guidance", "s", "/wt", None, None)]
    assert "not run" in summarize(prepared)


def test_hook_spawns_shadow_review_on_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig

    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "spotter.hook.subprocess.Popen", lambda cmd, **kw: spawned.append(list(cmd))
    )
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig(every_steps=2), GatesConfig())

    def payload(n: int) -> dict[str, object]:
        return {
            "hook_event_name": "PreToolUse",
            "session_id": "cadence",
            "cwd": "/nonexistent",
            "tool_name": "Bash",
            "tool_use_id": f"c{n}",
            "tool_input": {"command": "true"},
        }

    for n in range(4):  # steps 0..3
        run_hook(payload(n), config)
    assert len(spawned) == 1  # step 2 only (0 excluded, cadence 2)
    assert "review" in spawned[0]

    # recursion guard: a review's own codex session must not spawn reviews
    monkeypatch.setenv("SPOTTER_DISABLE", "1")
    run_hook(payload(4), config)
    assert len(spawned) == 1
