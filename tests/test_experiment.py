import json
import subprocess
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import spotter.experiment as experiment
from spotter.cli import main
from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.experiment import (
    EXPERIMENT_RESULT_SCHEMA,
    EXPERIMENT_RESULT_SCHEMA_VERSION,
    ArmClassification,
    ArmResult,
    ExperimentResultError,
    initialize_experiment_result,
    results_path,
    run_experiment,
    summarize,
)
from spotter.hook import journal_path, run_hook
from spotter.paths import spotter_home
from spotter.replay import ForkPlan
from spotter.snapshot import StepJournal

_real_locked_worktree = experiment._locked_worktree


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def valid_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def locked(worktree: str) -> Iterator[None]:
        yield None

    monkeypatch.setattr(experiment, "_locked_worktree", locked)
    monkeypatch.setattr(experiment, "_worktree_error", lambda worktree: None)


def _fake_fork(counter: dict[str, int]) -> Callable[..., ForkPlan]:
    def fake(session_id: str, step: int, *, codex_home: object = None) -> ForkPlan:
        counter["n"] = counter.get("n", 0) + 1
        n = counter["n"]
        return ForkPlan(
            f"fork-{n}",
            step,
            f"/wt/{n}",
            f"/ro/{n}",
            "codex ...",
            prefix_id="prefix",
            environment_fingerprint="environment",
        )

    return fake


def test_concurrent_result_initializers_write_one_header(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    row = {
        "schema": EXPERIMENT_RESULT_SCHEMA,
        "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
        "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
        "meta": True,
    }

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: initialize_experiment_result(path, row), range(20)))

    assert path.read_text().splitlines() == [json.dumps(row)]


def _agent_run(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, "", stderr)


def test_experiment_without_run_only_prepares(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    results = run_experiment("s1", 5, "check the stack trace", pairs=2)
    assert len(results) == 4  # 2 pairs x 2 arms
    assert {r.arm for r in results} == {"control", "guidance"}
    assert all(r.agent_exit is None for r in results)  # nothing executed


def test_results_carry_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #15 review P1: rows without conditions are not measurements."""
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    results = run_experiment(
        "s1", 5, "look at the trace", check="pytest -q", reasoning_effort="low"
    )
    rows = [json.loads(line) for line in results_path("s1", 5).read_text().splitlines()]
    meta = rows[0]
    assert meta["meta"] is True
    assert meta["guidance"] == "look at the trace"
    assert meta["check"] == "pytest -q"
    assert meta["sandbox"] and meta["timeout"] and meta["started_at"]
    assert meta["pairs"] == 1
    assert meta["reasoning_effort"] == "low"
    assert meta["result_schema_version"] == 3
    assert rows[-1]["complete"] is True and rows[-1]["finished_at"]
    assert all(
        row["schema"] == EXPERIMENT_RESULT_SCHEMA
        and row["schema_version"] == EXPERIMENT_RESULT_SCHEMA_VERSION
        and row["result_schema_version"] == EXPERIMENT_RESULT_SCHEMA_VERSION
        for row in rows
    )
    # every row is linked to its conditions via the experiment id
    assert all(row["experiment_id"] == meta["experiment_id"] for row in rows[1:])
    assert all(row.get("classification") == "UNJUDGEABLE" for row in rows[1:-1])
    assert all(row.get("prefix_id") == "prefix" for row in rows[1:-1])
    assert all(row.get("environment_fingerprint") == "environment" for row in rows[1:-1])
    assert all(row.get("environment_preflight") == "MATCHED" for row in rows[1:-1])
    assert all(r.experiment_id == meta["experiment_id"] for r in results)
    # a rerun with different conditions is distinguishable
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    run_experiment("s1", 5, "different guidance")
    rows2 = [json.loads(line) for line in results_path("s1", 5).read_text().splitlines()]
    metas = [r for r in rows2 if r.get("meta")]
    assert len(metas) == 2 and metas[0]["experiment_id"] != metas[1]["experiment_id"]


def test_legacy_result_history_is_read_before_current_rows_are_appended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    run_experiment("s1", 5, "first")
    path = results_path("s1", 5)
    legacy = []
    for line in path.read_text().splitlines():
        row = json.loads(line)
        row.pop("schema")
        row.pop("schema_version")
        if row.get("complete") is True:
            row.pop("result_schema_version")
        legacy.append(row)
    path.write_text("".join(json.dumps(row) + "\n" for row in legacy))

    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    run_experiment("s1", 5, "second")
    rows = [json.loads(line) for line in path.read_text().splitlines()]

    assert all("schema" not in row for row in rows[:4])
    assert all(row["schema"] == EXPERIMENT_RESULT_SCHEMA for row in rows[4:])


@pytest.mark.parametrize(
    ("schema", "version", "message"),
    [
        (EXPERIMENT_RESULT_SCHEMA, EXPERIMENT_RESULT_SCHEMA_VERSION + 1, "understands up to"),
        ("someone.else", EXPERIMENT_RESULT_SCHEMA_VERSION, "unsupported schema"),
    ],
)
def test_incompatible_result_history_is_refused_before_fork_or_append(
    monkeypatch: pytest.MonkeyPatch, schema: str, version: int, message: str
) -> None:
    path = results_path("s1", 5)
    path.write_text(
        json.dumps(
            {
                "schema": schema,
                "schema_version": version,
                "result_schema_version": version,
                "meta": True,
                "experiment_id": "existing",
            }
        )
        + "\n"
    )
    before = path.read_bytes()

    def unexpected_fork(*args: object, **kwargs: object) -> ForkPlan:
        raise AssertionError("incompatible history must be refused before forking")

    monkeypatch.setattr(experiment, "fork", unexpected_fork)
    with pytest.raises(ExperimentResultError, match=message):
        run_experiment("s1", 5, "hint")
    assert path.read_bytes() == before


def test_experiment_persists_minimal_agent_cost_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    monkeypatch.setattr(experiment, "_cleanup", lambda worktree: None)
    ticks = iter((1_000_000_000, 1_250_000_000, 2_000_000_000, 2_500_000_000))
    monkeypatch.setattr("spotter.experiment.time.monotonic_ns", lambda: next(ticks))
    stderr = "x" * 5000 + "\ntokens used\n1,234\n"
    monkeypatch.setattr(
        experiment,
        "_run_arm",
        lambda *args, **kwargs: _agent_run(stderr=stderr),
    )

    results = run_experiment("s1", 5, "hint", run=True)

    assert [result.agent_elapsed_ms for result in results] == [250.0, 500.0]
    assert [result.agent_reported_tokens for result in results] == [1234, 1234]
    rows = [json.loads(line) for line in results_path("s1", 5).read_text().splitlines()]
    assert [row["agent_elapsed_ms"] for row in rows[1:3]] == [250.0, 500.0]
    assert [row["agent_reported_tokens"] for row in rows[1:3]] == [1234, 1234]
    assert all("agent_stderr" not in row for row in rows[1:3])


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
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(prompt)
        return _agent_run()

    monkeypatch.setattr(experiment, "_run_arm", record_prompt)
    results = run_experiment("s1", 5, "hint", pairs=2, run=True)
    assert [r.arm for r in results] == ["control", "guidance", "guidance", "control"]
    assert prompts[0] == "Continue the task." and "hint" in prompts[1]
    assert "hint" in prompts[2] and prompts[3] == "Continue the task."


def test_pair_forks_are_both_preflighted_before_either_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    counter: dict[str, int] = {}
    fake = _fake_fork(counter)

    def record_fork(session_id: str, step: int, *, codex_home: object = None) -> ForkPlan:
        events.append("fork")
        return fake(session_id, step, codex_home=codex_home)

    def record_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        events.append("run")
        return _agent_run()

    monkeypatch.setattr(experiment, "fork", record_fork)
    monkeypatch.setattr(experiment, "_run_arm", record_run)

    run_experiment("s1", 5, "hint", run=True)

    assert events == ["fork", "fork", "run", "run"]


def test_neutral_noise_runs_identical_prompts_and_reports_outcome_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    monkeypatch.setattr(experiment, "_codex_version", lambda: None)
    prompts: list[str] = []

    def record_prompt(
        session: str, worktree: str, prompt: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(prompt)
        return _agent_run()

    check_exits = iter([0, 1, 0, 0])

    def check(*args: object, **kwargs: object) -> object:
        return type(
            "Completed",
            (),
            {"returncode": next(check_exits), "stdout": "", "stderr": ""},
        )()

    monkeypatch.setattr(experiment, "_run_arm", record_prompt)
    monkeypatch.setattr("spotter.experiment.subprocess.run", check)

    results = run_experiment("s1", 5, None, pairs=2, check="score", run=True, neutral=True)

    assert prompts == [experiment.CONTROL_PROMPT] * 4
    assert {result.arm for result in results} == {"neutral_a", "neutral_b"}
    assert all(result.experiment_mode == "neutral-noise" for result in results)
    summary = summarize(results)
    assert "mechanical outcome disagreements=1/2 (50.0%)" in summary
    assert "preflight failures=0/2" in summary
    rows = [json.loads(line) for line in results_path("s1", 5).read_text().splitlines()]
    assert rows[0]["experiment_mode"] == "neutral-noise"
    assert rows[0]["guidance"] is None


def test_environment_mismatch_prevents_both_agent_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def mismatched_fork(*args: object, **kwargs: object) -> ForkPlan:
        nonlocal calls
        calls += 1
        return ForkPlan(
            f"fork-{calls}",
            5,
            f"/wt/{calls}",
            f"/ro/{calls}",
            "codex ...",
            prefix_id="same-prefix",
            environment_fingerprint=f"environment-{calls}",
        )

    ran: list[str] = []
    monkeypatch.setattr(experiment, "fork", mismatched_fork)

    def record_run(*args: object, **kwargs: object) -> int:
        ran.append("run")
        return 0

    monkeypatch.setattr(experiment, "_run_arm", record_run)
    monkeypatch.setattr(experiment, "_cleanup", lambda worktree: None)

    results = run_experiment("s1", 5, None, run=True, neutral=True)

    assert ran == []
    assert all(result.classification == ArmClassification.INFRA_FAIL for result in results)
    assert all(
        result.environment_preflight == "ENVIRONMENT_FINGERPRINT_MISMATCH" for result in results
    )
    assert all(result.agent_exit is None for result in results)
    assert "preflight failures=1/1" in summarize(results)
    assert "infrastructure failures=2/2" in summarize(results)


def test_shared_arm_worktree_prevents_both_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = 0

    def shared_fork(*args: object, **kwargs: object) -> ForkPlan:
        nonlocal counter
        counter += 1
        return ForkPlan(
            f"fork-{counter}",
            5,
            "/same/worktree",
            f"/rollout/{counter}",
            "codex ...",
            prefix_id="same-prefix",
            environment_fingerprint="same-environment",
        )

    ran: list[str] = []
    monkeypatch.setattr(experiment, "fork", shared_fork)
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: ran.append("run"))
    monkeypatch.setattr(experiment, "_cleanup", lambda worktree: None)

    results = run_experiment("s1", 5, None, run=True, neutral=True)

    assert ran == []
    assert all(result.classification == ArmClassification.INFRA_FAIL for result in results)
    assert all(result.environment_preflight == "SHARED_ARM_WORKTREE" for result in results)


@pytest.mark.parametrize(
    ("observation_gaps", "external_effects", "expected"),
    (
        (1, [], "PREFIX_OBSERVATION_GAP"),
        (
            0,
            [{"kind": "git_remote_write", "resource": "origin", "reversible": False}],
            "PREFIX_EXTERNAL_EFFECT",
        ),
    ),
)
def test_prefix_contamination_prevents_both_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
    observation_gaps: int,
    external_effects: list[dict[str, object]],
    expected: str,
) -> None:
    counter = 0

    def contaminated_fork(*args: object, **kwargs: object) -> ForkPlan:
        nonlocal counter
        counter += 1
        return ForkPlan(
            f"fork-{counter}",
            5,
            f"/wt/{counter}",
            f"/ro/{counter}",
            "codex ...",
            external_effects=external_effects if counter == 2 else [],
            prefix_id="same-prefix",
            environment_fingerprint="same-environment",
            observation_gaps=observation_gaps if counter == 2 else 0,
        )

    ran: list[str] = []
    monkeypatch.setattr(experiment, "fork", contaminated_fork)
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: ran.append("run"))
    monkeypatch.setattr(experiment, "_cleanup", lambda worktree: None)

    results = run_experiment("s1", 5, None, run=True, neutral=True)

    assert ran == []
    assert all(result.classification == ArmClassification.INFRA_FAIL for result in results)
    assert all(result.environment_preflight == expected for result in results)
    assert all(result.infra_diagnostic == expected for result in results)
    assert all(result.agent_exit is None for result in results)
    rows = [
        row
        for line in results_path("s1", 5).read_text().splitlines()
        if "arm" in (row := json.loads(line))
    ]
    assert len(rows) == 2
    assert all(row["classification"] == "INFRA_FAIL" for row in rows)
    assert all(row["environment_preflight"] == expected for row in rows)
    assert all(row["infra_diagnostic"] == expected for row in rows)
    assert "preflight failures=1/1" in summarize(results)


def test_arm_rechecks_environment_immediately_before_agent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ForkPlan(
        "fork",
        5,
        "/wt",
        "/rollout",
        "codex ...",
        manifest="/manifest.json",
        prefix_id="prefix",
        environment_fingerprint="expected",
    )
    monkeypatch.setattr(
        experiment,
        "fingerprint_environment",
        lambda path, resources, variables, venv_or_cache: SimpleNamespace(
            fingerprint_sha256="drifted"
        ),
    )
    monkeypatch.setattr(
        experiment,
        "load_fork_manifest",
        lambda path: SimpleNamespace(environment=object()),
    )
    monkeypatch.setattr(
        experiment,
        "compare_environments",
        lambda expected, current: SimpleNamespace(
            drift=("TRACKED_STATE_MISMATCH",), equivalent=False
        ),
    )
    ran: list[str] = []
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: ran.append("run"))

    result = experiment._execute_arm(
        plan,
        "Continue the task.",
        "MATCHED",
        check=None,
        sandbox="workspace-write",
        timeout=1,
        model=None,
        reasoning_effort=None,
        codex_home=None,
    )

    assert ran == []
    assert result[2] == ArmClassification.INFRA_FAIL
    assert result[5] == "ENVIRONMENT_MISMATCH:TRACKED_STATE_MISMATCH"


def test_unchanged_declared_inputs_recheck_reaches_agent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = ForkPlan(
        "fork",
        5,
        "/wt",
        "/rollout",
        "codex ...",
        manifest="/manifest.json",
        prefix_id="prefix",
        environment_fingerprint="expected",
    )
    expected = SimpleNamespace(
        fingerprint_sha256="expected",
        declared_resources=(
            SimpleNamespace(path=".fixture-config", purpose="resource"),
            SimpleNamespace(path=".venv", purpose="venv_or_cache"),
        ),
        declared_environment_variables=(SimpleNamespace(name="SPOTTER_FIXTURE_MODE"),),
    )

    def fingerprint(
        path: Path,
        resources: tuple[str, ...],
        variables: tuple[str, ...],
        venv_or_cache: tuple[str, ...],
    ) -> object:
        assert resources == (".fixture-config",)
        assert variables == ("SPOTTER_FIXTURE_MODE",)
        assert venv_or_cache == (".venv",)
        return expected

    monkeypatch.setattr(
        experiment,
        "fingerprint_environment",
        fingerprint,
    )
    monkeypatch.setattr(
        experiment,
        "load_fork_manifest",
        lambda path: SimpleNamespace(environment=expected),
    )
    ran: list[str] = []

    def run_arm(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        ran.append("run")
        return _agent_run()

    monkeypatch.setattr(experiment, "_run_arm", run_arm)

    result = experiment._execute_arm(
        plan,
        "Continue the task.",
        "MATCHED",
        check=None,
        sandbox="workspace-write",
        timeout=1,
        model=None,
        reasoning_effort=None,
        codex_home=None,
    )

    assert ran == ["run"]
    assert result[0] == 0
    assert result[2] == ArmClassification.UNJUDGEABLE
    assert result[5] is None


def test_arm_environment_drift_is_persisted_and_summarized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter: dict[str, int] = {}

    def manifested_fork(session_id: str, step: int, *, codex_home: object = None) -> ForkPlan:
        plan = _fake_fork(counter)(session_id, step, codex_home=codex_home)
        return ForkPlan(
            plan.session_id,
            plan.branch_step,
            plan.worktree,
            plan.rollout,
            plan.command,
            manifest=f"/manifest-{plan.session_id}.json",
            prefix_id=plan.prefix_id,
            environment_fingerprint=plan.environment_fingerprint,
        )

    monkeypatch.setattr(experiment, "fork", manifested_fork)
    monkeypatch.setattr(
        experiment,
        "_arm_environment_preflight",
        lambda plan: "ENVIRONMENT_MISMATCH:TRACKED_STATE_MISMATCH",
    )
    monkeypatch.setattr(experiment, "_cleanup", lambda worktree: None)

    results = run_experiment("s1", 5, None, run=True, neutral=True)

    assert all(
        result.environment_preflight == "ENVIRONMENT_MISMATCH:TRACKED_STATE_MISMATCH"
        for result in results
    )
    assert "preflight failures=1/1" in summarize(results)


def test_source_environment_drift_blocks_both_arms_before_agent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter: dict[str, int] = {}

    def mismatched_fork(*args: object, **kwargs: object) -> ForkPlan:
        plan = _fake_fork(counter)("s1", 5)
        return ForkPlan(
            plan.session_id,
            plan.branch_step,
            plan.worktree,
            plan.rollout,
            plan.command,
            prefix_id=plan.prefix_id,
            environment_fingerprint=plan.environment_fingerprint,
            source_environment_preflight=("SOURCE_ENVIRONMENT_MISMATCH:MISSING_IGNORED_FILE"),
        )

    ran: list[str] = []
    monkeypatch.setattr(experiment, "fork", mismatched_fork)
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: ran.append("run"))
    monkeypatch.setattr(experiment, "_cleanup", lambda worktree: None)

    results = run_experiment(
        "s1",
        5,
        None,
        run=True,
        neutral=True,
        environment_resources=(".env",),
    )

    assert ran == []
    assert all(result.classification == ArmClassification.INFRA_FAIL for result in results)
    assert all(
        result.environment_preflight == "SOURCE_ENVIRONMENT_MISMATCH:MISSING_IGNORED_FILE"
        for result in results
    )


def test_experiment_passes_declared_venv_or_cache_to_each_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, ...]] = []
    counter: dict[str, int] = {}

    def record_fork(*args: object, **kwargs: object) -> ForkPlan:
        captured.append(cast(tuple[str, ...], kwargs["environment_venv_or_cache"]))
        return _fake_fork(counter)("s1", 5)

    monkeypatch.setattr(experiment, "fork", record_fork)

    run_experiment(
        "s1",
        5,
        "hint",
        environment_venv_or_cache=(".venv", ".cache"),
    )

    assert captured == [(".venv", ".cache"), (".venv", ".cache")]
    meta = json.loads(results_path("s1", 5).read_text().splitlines()[0])
    assert meta["environment_venv_or_cache"] == [".venv", ".cache"]


def test_explicit_source_config_mismatch_prevents_agent_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter: dict[str, int] = {}

    def manifested_fork(session_id: str, step: int, *, codex_home: object = None) -> ForkPlan:
        plan = _fake_fork(counter)(session_id, step, codex_home=codex_home)
        return ForkPlan(
            plan.session_id,
            plan.branch_step,
            plan.worktree,
            plan.rollout,
            plan.command,
            manifest=f"/manifest-{plan.session_id}.json",
            prefix_id=plan.prefix_id,
            environment_fingerprint=plan.environment_fingerprint,
        )

    monkeypatch.setattr(experiment, "fork", manifested_fork)
    monkeypatch.setattr(
        experiment,
        "load_fork_manifest",
        lambda path: SimpleNamespace(
            prefix=SimpleNamespace(model="gpt-test", agent_config='{"effort":"high"}')
        ),
    )
    monkeypatch.setattr(experiment, "_cleanup", lambda worktree: None)
    ran: list[str] = []
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: ran.append("run"))

    results = run_experiment(
        "s1",
        5,
        None,
        run=True,
        neutral=True,
        model="gpt-test",
        reasoning_effort="low",
    )

    assert ran == []
    assert all(result.classification == ArmClassification.SETUP_FAIL for result in results)
    assert all(result.infra_diagnostic == "SOURCE_REASONING_EFFORT_MISMATCH" for result in results)
    assert "preflight failures=1/1" in summarize(results)


def test_empty_experiment_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #15 review P2: --pairs 0 must not succeed with zero data."""
    with pytest.raises(ValueError, match="pairs must be >= 1"):
        run_experiment("s1", 5, "hint", pairs=0)


def test_guidance_is_required_outside_neutral_mode() -> None:
    with pytest.raises(ValueError, match="guidance is required"):
        run_experiment("s1", 5, None)


def test_neutral_mode_rejects_guidance_provenance() -> None:
    with pytest.raises(ValueError, match="cannot include guidance"):
        run_experiment("s1", 5, "do something different", neutral=True)


def test_cli_accepts_neutral_mode_without_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def record_run(
        session: str, step: int, guidance: str | None, **kwargs: object
    ) -> list[ArmResult]:
        captured.update(session=session, step=step, guidance=guidance, **kwargs)
        return []

    monkeypatch.setattr("spotter.cli.run_experiment", record_run)
    monkeypatch.setattr("spotter.cli.summarize", lambda results: "neutral summary")

    assert (
        main(
            [
                "experiment",
                "--session",
                "s1",
                "--step",
                "5",
                "--neutral",
                "--reasoning-effort",
                "low",
                "--environment-resource",
                ".fixture-config",
                "--environment-variable",
                "SPOTTER_FIXTURE_MODE",
                "--environment-venv-or-cache",
                ".venv",
            ]
        )
        == 0
    )
    assert captured["guidance"] is None
    assert captured["neutral"] is True
    assert captured["reasoning_effort"] == "low"
    assert captured["environment_resources"] == (".fixture-config",)
    assert captured["environment_variables"] == ("SPOTTER_FIXTURE_MODE",)
    assert captured["environment_venv_or_cache"] == (".venv",)


def test_cli_rejects_neutral_mode_with_guidance() -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "experiment",
                "--session",
                "s1",
                "--step",
                "5",
                "--neutral",
                "--guidance",
                "different",
            ]
        )


def test_check_runs_in_each_fork_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: _agent_run())
    checks: list[str] = []

    class FakeCompleted:
        returncode = 0
        stdout = "check output"
        stderr = ""

    def fake_subprocess_run(cmd: object, **kwargs: object) -> FakeCompleted:
        if kwargs.get("cwd"):
            checks.append(str(kwargs["cwd"]))
        return FakeCompleted()

    monkeypatch.setattr("spotter.experiment.subprocess.run", fake_subprocess_run)
    results = run_experiment("s1", 5, "hint", run=True, check="pytest -q")
    assert checks == ["/wt/1", "/wt/2"]
    assert all(r.check_exit == 0 for r in results)
    assert all(r.classification == ArmClassification.PASS for r in results)
    assert all(r.check_stdout == "check output" for r in results)


def test_experiment_locks_worktree_until_arm_finishes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "tracked").write_text("fixture")
    subprocess.run(["git", "add", "tracked"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(worktree)], cwd=repo, check=True)

    with _real_locked_worktree(str(worktree)) as lock_error:
        assert lock_error is None
        listing = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "locked spotter-experiment" in listing

    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "locked spotter-experiment" not in listing


def test_missing_worktree_before_check_is_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: _agent_run())
    monkeypatch.setattr(
        experiment,
        "_worktree_error",
        lambda worktree: "WORKTREE_UNAVAILABLE_BEFORE_CHECK:missing metadata",
    )

    results = run_experiment(
        "s1",
        5,
        None,
        run=True,
        neutral=True,
        check="pytest -q",
    )

    assert all(result.check_exit is None for result in results)
    assert all(result.classification == ArmClassification.INFRA_FAIL for result in results)
    assert all(
        result.infra_diagnostic == "WORKTREE_UNAVAILABLE_BEFORE_CHECK:missing metadata"
        for result in results
    )
    assert "noise pairs: n=0/1 judgeable" in summarize(results)
    assert "infrastructure failures=2/2" in summarize(results)


def test_summarize_refuses_to_call_completion_success() -> None:
    ran_no_check = [
        ArmResult("e", 0, "control", "s", "/wt", 0, None, ArmClassification.UNJUDGEABLE)
    ]
    assert "completion is not success" in summarize(ran_no_check)
    prepared = [
        ArmResult("e", 0, "guidance", "s", "/wt", None, None, ArmClassification.UNJUDGEABLE)
    ]
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
    assert argv[argv.index("--review-job-id") + 1] == "proposal:1"
    records = StepJournal.load(journal_path({"session_id": "cadence"}))
    queued = next(record.event for record in records if record.event.kind == "review_job_queued")
    assert queued.payload["review_trigger"] == "periodic"


def test_failed_agent_is_not_checked_or_counted_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: _agent_run(1))
    checks: list[tuple[object, ...]] = []

    def record_check(*args: object, **kwargs: object) -> object:
        checks.append(args)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("spotter.experiment.subprocess.run", record_check)
    results = run_experiment("s1", 5, "hint", run=True, check="pytest -q")
    assert all(result.check_exit is None for result in results)
    assert all(result.classification == ArmClassification.INFRA_FAIL for result in results)
    assert "INFRA_FAIL=1" in summarize(results)
    assert not any(args and args[0] == "pytest -q" for args in checks)


def test_timeout_does_not_abort_experiment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    calls = 0

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise subprocess.TimeoutExpired("codex", 1)
        return _agent_run()

    monkeypatch.setattr(experiment, "_run_arm", run)
    monkeypatch.setattr(experiment, "_cleanup", lambda worktree: None)
    results = run_experiment("s1", 5, "hint", pairs=3, run=True)
    assert len(results) == 6
    assert [result.classification for result in results].count(ArmClassification.TIMEOUT_AGENT) == 1
    assert "TIMEOUT_AGENT=1" in summarize(results)


def test_failed_check_is_task_failure_with_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: _agent_run())
    monkeypatch.setattr(experiment, "_codex_version", lambda: None)

    def fail_check(*args: object, **kwargs: object) -> object:
        return type(
            "Completed",
            (),
            {"returncode": 1, "stdout": "x" * 5000, "stderr": "assertion failed"},
        )()

    monkeypatch.setattr("spotter.experiment.subprocess.run", fail_check)

    results = run_experiment("s1", 5, "hint", run=True, check="python3 check.py")

    assert all(result.classification == ArmClassification.TASK_FAIL for result in results)
    assert all(result.check_exit == 1 for result in results)
    assert all(len(result.check_stdout) == 4000 for result in results)
    assert all(result.check_stderr == "assertion failed" for result in results)


def test_check_timeout_is_not_counted_as_task_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "fork", _fake_fork({}))
    monkeypatch.setattr(experiment, "_run_arm", lambda *args, **kwargs: _agent_run())
    monkeypatch.setattr(experiment, "_codex_version", lambda: None)

    def timeout_check(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("check", 1, output="partial", stderr="hung")

    monkeypatch.setattr("spotter.experiment.subprocess.run", timeout_check)

    results = run_experiment("s1", 5, "hint", run=True, check="python3 check.py")

    assert all(result.classification == ArmClassification.TIMEOUT_CHECK for result in results)
    assert all(result.check_exit is None for result in results)
    assert all(result.check_stdout == "partial" for result in results)


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
        ArmResult("e", 0, "control", "s", "/wt", 0, 1, ArmClassification.TASK_FAIL),
        ArmResult("e", 0, "guidance", "s", "/wt", 0, 0, ArmClassification.PASS),
        ArmResult("e", 1, "control", "s", "/wt", None, None, ArmClassification.TIMEOUT_AGENT),
        ArmResult("e", 1, "guidance", "s", "/wt", 0, 0, ArmClassification.PASS),
    ]
    assert "n=1/2 complete; guidance better=1" in summarize(rows)


def test_run_arm_forwards_model_reasoning_effort_and_codex_home(
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
        reasoning_effort="low",
        codex_home=tmp_path,
    )
    assert calls[0][0][4:6] == ["--model", "gpt-test"]
    assert calls[0][0][6:8] == ["--config", 'model_reasoning_effort="low"']
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
