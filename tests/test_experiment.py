import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import spotter.experiment as experiment
from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.experiment import ArmResult, results_path, run_experiment, summarize
from spotter.hook import run_hook
from spotter.paths import spotter_home
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
    assert meta["pairs"] == 1
    assert rows[-1]["complete"] is True and rows[-1]["finished_at"]
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
    monkeypatch.setattr("spotter.experiment.uuid.uuid4", lambda: uuid.UUID(int=0))
    prompts: list[str] = []

    def record_prompt(
        s: str, w: str, prompt: str, *, sandbox: str, timeout: int, **kwargs: object
    ) -> int:
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
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: 0)
    checks: list[str] = []

    class FakeCompleted:
        returncode = 0

    def fake_subprocess_run(cmd: object, **kwargs: object) -> FakeCompleted:
        if kwargs.get("cwd"):
            checks.append(str(kwargs["cwd"]))
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


def test_cadence_is_atomic_under_concurrent_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "spotter.hook.subprocess.Popen", lambda cmd, **kw: spawned.append(list(cmd))
    )
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig(every_steps=2), GatesConfig())
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda n: run_hook(_cadence_payload("PreToolUse", n), config), range(2)))
    assert len(spawned) == 1


def test_cadence_forwards_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PR #15 review P0: the child must judge with the user's reviewer config."""
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "spotter.hook.subprocess.Popen", lambda cmd, **kw: spawned.append(list(cmd))
    )
    config = SpotterConfig(
        MainAgentConfig("codex"), ReviewerConfig("custom-model", every_steps=1), GatesConfig()
    )
    config_path = tmp_path / "spotter.toml"
    run_hook(_cadence_payload("PreToolUse", 0), config, config_path)
    assert spawned
    argv = spawned[0]
    assert "--model" in argv and "custom-model" in argv
    # Without --config the child builds a default config, so the constraints
    # the user configured never reach the reviewer (PR #58 review, P1).
    assert "--config" in argv and str(config_path) in argv
    # the slot was taken before spawning and is identified by a token, so the
    # child cannot claim a reservation it never received
    assert "--reservation" in argv and len(argv[argv.index("--reservation") + 1]) == 32


def test_failed_agent_is_not_checked_or_counted_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: 1)
    checks: list[tuple[object, ...]] = []

    def record_check(*args: object, **kwargs: object) -> object:
        checks.append(args)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("spotter.experiment.subprocess.run", record_check)
    results = run_experiment("s1", 5, "hint", run=True, check="pytest -q")
    assert all(result.check_exit is None for result in results)
    assert "invalid agent run" in summarize(results)
    assert not any(args and args[0] == "pytest -q" for args in checks)


def test_timeout_does_not_abort_experiment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    calls = 0

    def run(*args: object, **kwargs: object) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise subprocess.TimeoutExpired("codex", 1)
        return 0

    monkeypatch.setattr(experiment, "_run_arm", run)
    monkeypatch.setattr(experiment, "_cleanup", lambda worktree: None)
    results = run_experiment("s1", 5, "hint", pairs=3, run=True)
    assert len(results) == 6
    assert [result.agent_exit for result in results].count(124) == 1


def test_cleanup_is_best_effort_and_preserves_rollout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("transcript")
    monkeypatch.setattr(
        "spotter.experiment.subprocess.run",
        lambda *args, **kwargs: type("Completed", (), {"returncode": 1})(),
    )
    experiment._cleanup(str(worktree))
    assert rollout.read_text() == "transcript"


def test_summary_compares_complete_pairs() -> None:
    rows = [
        ArmResult("e", 0, "control", "s", "/wt", 0, 1),
        ArmResult("e", 0, "guidance", "s", "/wt", 0, 0),
        ArmResult("e", 1, "control", "s", "/wt", 124, None),
        ArmResult("e", 1, "guidance", "s", "/wt", 0, 0),
    ]
    assert "n=1/2 complete; guidance better=1" in summarize(rows)


def test_run_arm_forwards_model_and_codex_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Completed:
        returncode = 0

    def record_call(args: list[str], **kwargs: object) -> Completed:
        calls.append((args, kwargs))
        return Completed()

    monkeypatch.setattr("spotter.experiment.subprocess.run", record_call)
    experiment._run_arm(
        "fork",
        "/wt",
        "go",
        sandbox="workspace-write",
        timeout=1,
        model="gpt-test",
        codex_home=tmp_path,
    )
    assert calls[0][0][4:6] == ["--model", "gpt-test"]
    assert calls[0][1]["env"]["CODEX_HOME"] == str(tmp_path)  # type: ignore[index]


def test_cadence_recursion_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "spotter.hook.subprocess.Popen", lambda cmd, **kw: spawned.append(list(cmd))
    )
    monkeypatch.setenv("SPOTTER_DISABLE", "1")
    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig(every_steps=1), GatesConfig())
    run_hook(_cadence_payload("PreToolUse", 0), config)
    assert spawned == []
