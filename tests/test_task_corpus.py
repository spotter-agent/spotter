import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import spotter.cli as cli
import spotter.task_corpus as task_corpus
from spotter.cli import main
from spotter.doctor import OK
from spotter.paths import RuntimeLayout
from spotter.replay import fork
from spotter.snapshot import StepJournal, snapshot_worktree
from spotter.task_corpus import (
    TASK_BATCH_SCHEMA,
    TASK_BATCH_SCHEMA_VERSION,
    TASK_SCHEMA,
    TASK_SET_SCHEMA,
    PreflightClassification,
    TaskCorpusError,
    file_digest,
    fixture_digest,
    preflight_task_set,
    run_task_batch,
    summarize_task_batch,
    validate_task_set,
)
from spotter.trace import TraceEvent


def _corpus(root: Path) -> Path:
    fixture = root / "fixtures" / "parser"
    fixture.mkdir(parents=True)
    (fixture / "parser.py").write_text("def parse(): return 0\n")
    (fixture / "parser.good").write_text("def parse(): return 1\n")
    (fixture / "check.py").write_text("from parser import parse\nassert parse() == 1\n")
    task = root / "tasks" / "parser.toml"
    task.parent.mkdir()
    task.write_text(
        f'''task_schema = "spotter.task"
task_schema_version = 1
task_id = "fixture/parser-001"
prompt = "Fix the parser regression."

[source]
kind = "fixture"
path = "fixtures/parser"
sha256 = "{fixture_digest(fixture)}"

[setup]
command = "python3 -m compileall ."
timeout_s = 30

[precheck]
command = "python3 check.py"
timeout_s = 30
expected = "failure"

[known_good]
command = "cp parser.good parser.py && rm -rf __pycache__"
timeout_s = 30

[[checks]]
id = "task-resolution"
command = "python3 check.py"
timeout_s = 30
required = true

[budget]
wall_time_s = 600
max_turns = 20

[metadata]
family = "localized-fix"
difficulty = "dev"
provenance = "spotter synthetic fixture"
'''
    )
    task_set = root / "dev.toml"
    task_set.write_text(
        f'''task_set_schema = "spotter.task_set"
task_set_schema_version = 1
task_set_id = "spotter-dev"
version = 1
split = "dev"

[[tasks]]
task_id = "fixture/parser-001"
manifest = "tasks/parser.toml"
sha256 = "{file_digest(task)}"
'''
    )
    return task_set


def _refreeze_task(root: Path, task_set: Path) -> None:
    task = root / "tasks" / "parser.toml"
    set_text = task_set.read_text()
    old_hash = set_text.split('sha256 = "', 1)[1].split('"', 1)[0]
    task_set.write_text(set_text.replace(old_hash, file_digest(task)))


def test_validates_versioned_task_set_and_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _corpus(tmp_path)

    task_set = validate_task_set(path)

    assert task_set.task_set_id == "spotter-dev"
    assert task_set.split == "dev"
    assert task_set.tasks[0].task_id == "fixture/parser-001"
    assert TASK_SCHEMA == "spotter.task" and TASK_SET_SCHEMA == "spotter.task_set"
    assert main(["tasks", "validate", str(path)]) == 0
    assert "validated spotter-dev v1 (dev): 1 task(s)" in capsys.readouterr().out


def test_legacy_schema_name_less_task_manifests_remain_readable(tmp_path: Path) -> None:
    path = _corpus(tmp_path)
    task = tmp_path / "tasks" / "parser.toml"
    task.write_text(task.read_text().replace('task_schema = "spotter.task"\n', ""))
    _refreeze_task(tmp_path, path)
    path.write_text(path.read_text().replace('task_set_schema = "spotter.task_set"\n', ""))

    assert validate_task_set(path).task_set_id == "spotter-dev"


@pytest.mark.parametrize("target", ["task", "task_set"])
def test_foreign_task_manifest_schema_is_refused(tmp_path: Path, target: str) -> None:
    path = _corpus(tmp_path)
    if target == "task":
        task = tmp_path / "tasks" / "parser.toml"
        task.write_text(task.read_text().replace(TASK_SCHEMA, "someone.else"))
        _refreeze_task(tmp_path, path)
    else:
        path.write_text(path.read_text().replace(TASK_SET_SCHEMA, "someone.else"))

    with pytest.raises(TaskCorpusError, match="schema"):
        validate_task_set(path)


def test_manifest_hash_detects_task_set_drift(tmp_path: Path) -> None:
    path = _corpus(tmp_path)
    task = tmp_path / "tasks" / "parser.toml"
    task.write_text(task.read_text() + "\n# changed after set freeze\n")

    with pytest.raises(TaskCorpusError, match="manifest sha256 mismatch"):
        validate_task_set(path)


def test_fixture_hash_detects_environment_drift(tmp_path: Path) -> None:
    path = _corpus(tmp_path)
    (tmp_path / "fixtures" / "parser" / "parser.py").write_text("changed\n")

    with pytest.raises(TaskCorpusError, match="fixture sha256 mismatch"):
        validate_task_set(path)


def test_rejects_duplicate_task_ids(tmp_path: Path) -> None:
    path = _corpus(tmp_path)
    first = path.read_text().split("[[tasks]]", 1)[1]
    path.write_text(path.read_text() + "\n[[tasks]]" + first)

    with pytest.raises(TaskCorpusError, match="duplicate task_id"):
        validate_task_set(path)


def test_fixture_digest_includes_relative_paths(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "a").write_text("same")
    first = fixture_digest(fixture)
    (fixture / "a").rename(fixture / "b")

    assert fixture_digest(fixture) != first
    assert len(first) == hashlib.sha256().digest_size * 2


def test_preflight_proves_negative_and_positive_scorer_states(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _corpus(tmp_path)

    _, results = preflight_task_set(path)

    assert results[0].classification == PreflightClassification.READY
    assert [result.phase for result in results[0].commands] == [
        "setup",
        "precheck",
        "negative:task-resolution",
        "known_good",
        "positive:task-resolution",
    ]
    assert main(["tasks", "preflight", str(path)]) == 0
    assert "fixture/parser-001: READY" in capsys.readouterr().out


def test_rejects_directory_symlinks_in_fixtures(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    outside = tmp_path / "outside"
    fixture.mkdir()
    outside.mkdir()
    (fixture / "owned").write_text("inside")
    (outside / "foreign").write_text("outside")
    (fixture / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(TaskCorpusError, match="symlinks are unsupported"):
        fixture_digest(fixture)


def test_rejects_task_set_without_a_required_check(tmp_path: Path) -> None:
    path = _corpus(tmp_path)
    task = tmp_path / "tasks" / "parser.toml"
    task.write_text(task.read_text().replace("required = true", "required = false"))
    _refreeze_task(tmp_path, path)

    with pytest.raises(TaskCorpusError, match="at least one check must be required"):
        validate_task_set(path)


def test_preflight_classifies_required_check_timeout(tmp_path: Path) -> None:
    path = _corpus(tmp_path)
    task = tmp_path / "tasks" / "parser.toml"
    text = task.read_text()
    text = text.replace(
        '[[checks]]\nid = "task-resolution"\ncommand = "python3 check.py"\ntimeout_s = 30',
        '[[checks]]\nid = "task-resolution"\n'
        "command = \"python3 -c 'import time; time.sleep(2)'\"\ntimeout_s = 1",
    )
    task.write_text(text)
    _refreeze_task(tmp_path, path)

    _, results = preflight_task_set(path)

    assert results[0].classification == PreflightClassification.TIMEOUT_CHECK
    assert results[0].commands[-1].timed_out is True


@pytest.mark.parametrize(
    ("name", "expected_tasks"),
    [
        ("dev-v1.toml", 1),
        ("validation-v1.toml", 1),
        ("dev-v2.toml", 3),
        ("validation-v2.toml", 3),
    ],
)
def test_repo_corpus_is_frozen_and_preflight_ready(name: str, expected_tasks: int) -> None:
    path = Path(__file__).parents[1] / "corpus" / name

    task_set, results = preflight_task_set(path)

    assert len(task_set.tasks) == expected_tasks
    assert all(result.classification == PreflightClassification.READY for result in results)


def test_repo_v2_dev_and_validation_tasks_are_disjoint() -> None:
    corpus = Path(__file__).parents[1] / "corpus"
    dev = validate_task_set(corpus / "dev-v2.toml")
    validation = validate_task_set(corpus / "validation-v2.toml")

    assert {task.task_id for task in dev.tasks}.isdisjoint(
        task.task_id for task in validation.tasks
    )


def test_task_batch_runs_clean_control_and_guidance_arms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(task_corpus, "_codex_version", lambda: "codex-test")
    prompts: list[str] = []

    def solve(
        workspace: Path,
        prompt: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
        sandbox: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        assert model == "gpt-test"
        assert reasoning_effort == "low"
        prompts.append(prompt)
        (workspace / "parser.py").write_text("def parse(): return 1\n")
        shutil.rmtree(workspace / "__pycache__", ignore_errors=True)
        return subprocess.CompletedProcess([], 0, "agent output", "")

    monkeypatch.setattr(task_corpus, "_run_task_agent", solve)

    output, results = run_task_batch(
        path,
        "Inspect the failing check first.",
        model="gpt-test",
        reasoning_effort="low",
    )

    assert {result.arm for result in results} == {"control", "guidance"}
    assert all(result.classification == "PASS" for result in results)
    assert all(result.setup.returncode == 0 for result in results)
    assert all(result.checks[0].returncode == 0 for result in results)
    assert all(result.wall_time_s == 600 and result.max_turns == 20 for result in results)
    assert len({result.experiment_pair_id for result in results}) == 1
    assert "pairs: n=1/1 mechanically judgeable" in summarize_task_batch(results)
    assert all(result.workspace is None for result in results)
    assert any("Inspect the failing check first." in prompt for prompt in prompts)
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["task_set_sha256"] == file_digest(path)
    assert rows[0]["model"] == "gpt-test"
    assert rows[0]["reasoning_effort"] == "low"
    assert rows[-1]["complete"] is True
    assert all(
        row["schema"] == TASK_BATCH_SCHEMA
        and row["schema_version"] == TASK_BATCH_SCHEMA_VERSION
        and row["result_schema_version"] == TASK_BATCH_SCHEMA_VERSION
        for row in rows
    )


def test_task_batch_captures_replay_source_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    home = tmp_path / "home"
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("SPOTTER_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(task_corpus, "_codex_version", lambda: "codex-test")
    readiness = {
        "integration_generation": "generation-a",
        "setup_build_id": "build-a",
        "spotter_home": str(home),
        "codex_home": str(codex_home),
        "hook_command_sha256": "hook-a",
        "config_generation": "config-a",
    }
    monkeypatch.setattr(task_corpus, "_capture_readiness", lambda _: readiness, raising=False)
    sessions = iter(("source-control", "source-guidance"))

    def solve(
        workspace: Path,
        prompt: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
        sandbox: str,
        timeout: int,
        capture_replay_source: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_replay_source is True
        assert reasoning_effort == "low"
        assert (
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            == "true"
        )
        session = next(sessions)
        journal = home / "sessions" / f"{session}.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        snapshot = snapshot_worktree(workspace)
        StepJournal(journal).record(TraceEvent("sessionstart"), snapshot=snapshot)
        call_id = f"call-{session}"
        StepJournal(journal).record(
            TraceEvent(
                "tool_proposal",
                {"tool_use_id": call_id, "cwd": str(workspace)},
            ),
            snapshot=snapshot,
        )
        rollout = codex_home / "sessions" / f"rollout-{session}.jsonl"
        rollout.parent.mkdir(parents=True, exist_ok=True)
        rollout.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {"session_id": session, "id": session},
                        }
                    ),
                    json.dumps({"type": "response_item", "payload": {"call_id": call_id}}),
                )
            )
            + "\n"
        )
        (workspace / "parser.py").write_text("def parse(): return 1\n")
        shutil.rmtree(workspace / "__pycache__", ignore_errors=True)
        stdout = json.dumps({"type": "thread.started", "thread_id": session})
        assert task_corpus._replay_source(stdout) == (session, None)
        return subprocess.CompletedProcess([], 0, stdout, "")

    monkeypatch.setattr(task_corpus, "_run_task_agent", solve)

    output, results = run_task_batch(
        path,
        "Inspect first.",
        reasoning_effort="low",
        capture_replay_sources=True,
    )

    assert {result.replay_source_session_id for result in results} == {
        "source-control",
        "source-guidance",
    }, [result.replay_source_error for result in results]
    assert all(result.replay_source_requested for result in results)
    assert all(result.replay_source_error is None for result in results)
    assert all(result.workspace and Path(result.workspace).is_dir() for result in results)
    assert all(
        Path(result.workspace or "").parent.parent == home / "task-sources" for result in results
    )
    assert "replay sources: 2/2 captured" in summarize_task_batch(results)
    for result in results:
        plan = fork(str(result.replay_source_session_id), 1, codex_home=codex_home)
        assert Path(plan.worktree, "parser.py").read_text() == "def parse(): return 0\n"
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["capture_replay_sources"] is True
    assert rows[0]["capture_readiness"] == readiness
    assert all(row["replay_source_session_id"] for row in rows if "task_id" in row)

    output.write_text("\n".join(json.dumps(row) for row in rows[:2]) + "\n")
    for key in ("integration_generation", "hook_command_sha256", "config_generation"):
        changed = {**readiness, key: f"changed-{key}"}
        monkeypatch.setattr(task_corpus, "_capture_readiness", lambda _, row=changed: row)
        with pytest.raises(TaskCorpusError, match="capture_readiness does not match"):
            run_task_batch(
                path,
                "Inspect first.",
                resume=output,
                reasoning_effort="low",
                capture_replay_sources=True,
            )

    legacy_header = {key: value for key, value in rows[0].items() if key != "capture_readiness"}
    output.write_text("\n".join((json.dumps(legacy_header), json.dumps(rows[1]))) + "\n")
    sessions = iter(("source-legacy-resume",))
    monkeypatch.setattr(task_corpus, "_capture_readiness", lambda _: readiness)

    _, resumed = run_task_batch(
        path,
        "Inspect first.",
        resume=output,
        reasoning_effort="low",
        capture_replay_sources=True,
    )

    assert len(resumed) == 2
    assert {result.replay_source_session_id for result in resumed} >= {"source-legacy-resume"}


def test_capture_readiness_failure_starts_no_agent_or_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    home = tmp_path / "home"
    monkeypatch.setenv("SPOTTER_HOME", str(home))

    def unavailable(_: object) -> dict[str, str]:
        raise TaskCorpusError("replay-source capture unavailable: Spotter Hook is missing")

    def unexpected(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("capture readiness must fail before the paid agent")

    monkeypatch.setattr(task_corpus, "_capture_readiness", unavailable, raising=False)
    monkeypatch.setattr(task_corpus, "_run_task_agent", unexpected)

    with pytest.raises(TaskCorpusError, match="capture unavailable"):
        run_task_batch(path, "Inspect first.", capture_replay_sources=True)

    batches = home / "experiments" / "task-batches"
    assert not batches.exists() or not list(batches.glob("*.jsonl"))


def test_capture_readiness_receipt_binds_integration_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    home = tmp_path / "home"
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("SPOTTER_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    command = f"SPOTTER_HOME={home} /bin/spotter hook || true"
    layout = SimpleNamespace(user_data_dir=home, bridge_command=("/trusted/spotter", "hook"))
    monkeypatch.setattr(RuntimeLayout, "discover", lambda: layout)
    manifest = SimpleNamespace(
        codex_home=str(codex_home),
        runtime_layout={"user_data_dir": str(home)},
        owned_hooks=[
            {"event": event, "hook": {"command": command}}
            for event in ("SessionStart", "PreToolUse", "PostToolUse")
        ],
        config_path=None,
        integration_generation="generation-a",
        setup_build_id="build-a",
    )
    inspection = SimpleNamespace(
        manifest=manifest,
        check=SimpleNamespace(status=OK, detail="ready"),
        hook_ready=True,
    )
    monkeypatch.setattr(task_corpus, "check_integration", lambda: inspection)
    monkeypatch.setattr(
        task_corpus,
        "resolve_config",
        lambda **kwargs: SimpleNamespace(
            config=SimpleNamespace(snapshot_on_patch=True),
            resolved_config_generation="config-a",
        ),
    )

    def roundtrip(config_path: object, *, command: list[str], capture_only: bool) -> object:
        assert config_path is None
        assert command == [
            "/trusted/spotter",
            "hook",
            "--integration-generation",
            "generation-a",
        ]
        assert capture_only is True
        return SimpleNamespace(status=OK, detail="recorded")

    monkeypatch.setattr(task_corpus, "check_roundtrip", roundtrip)

    receipt = task_corpus._capture_readiness(validate_task_set(path))

    assert receipt == {
        "integration_generation": "generation-a",
        "setup_build_id": "build-a",
        "spotter_home": str(home),
        "codex_home": str(codex_home),
        "hook_command_sha256": hashlib.sha256(command.encode()).hexdigest(),
        "config_generation": "config-a",
        "task_set_id": "spotter-dev",
    }

    manifest.owned_hooks[1]["hook"]["command"] += " --stale"
    with pytest.raises(TaskCorpusError, match="commands are missing or inconsistent"):
        task_corpus._capture_readiness(validate_task_set(path))


def test_capture_readiness_refuses_unready_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    inspection = SimpleNamespace(
        manifest=None,
        check=SimpleNamespace(status="fail", detail="owned Hook is missing"),
        hook_ready=False,
    )
    monkeypatch.setattr(task_corpus, "check_integration", lambda: inspection)

    with pytest.raises(TaskCorpusError, match="owned Hook is missing"):
        task_corpus._capture_readiness(validate_task_set(path))


@pytest.mark.parametrize("mismatch", ("spotter", "codex"))
def test_capture_readiness_refuses_home_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    path = _corpus(tmp_path / "corpus")
    home = tmp_path / "home"
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("SPOTTER_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    manifest = SimpleNamespace(
        codex_home=str(codex_home if mismatch != "codex" else tmp_path / "other-codex"),
        runtime_layout={
            "user_data_dir": str(home if mismatch != "spotter" else tmp_path / "other-home")
        },
    )
    inspection = SimpleNamespace(
        manifest=manifest,
        check=SimpleNamespace(status=OK, detail="ready"),
        hook_ready=True,
    )
    monkeypatch.setattr(task_corpus, "check_integration", lambda: inspection)

    with pytest.raises(TaskCorpusError, match=f"different {mismatch.upper()}_HOME"):
        task_corpus._capture_readiness(validate_task_set(path))


def test_task_agent_capture_mode_enables_json_hooks_without_supervision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    class Process:
        pid = 1
        returncode = 0
        stdout = None
        stderr = None

        def communicate(self, timeout: int) -> tuple[str, str]:
            return "", ""

    def popen(args: list[str], **kwargs: object) -> Process:
        env = kwargs["env"]
        assert isinstance(env, dict)
        calls.append((args, env))
        return Process()

    monkeypatch.setattr("spotter.task_corpus.subprocess.Popen", popen)
    monkeypatch.setenv("SPOTTER_DISABLE", "inherited")
    monkeypatch.setenv("SPOTTER_CAPTURE_ONLY", "stale")

    task_corpus._run_task_agent(
        tmp_path,
        "task",
        model=None,
        sandbox="workspace-write",
        timeout=30,
        reasoning_effort="low",
        capture_replay_source=True,
    )
    task_corpus._run_task_agent(tmp_path, "task", model=None, sandbox="workspace-write", timeout=30)

    capture_args, capture_env = calls[0]
    assert "--json" in capture_args
    assert "--dangerously-bypass-hook-trust" in capture_args
    assert capture_args[capture_args.index("--config") + 1] == 'model_reasoning_effort="low"'
    assert capture_env["SPOTTER_CAPTURE_ONLY"] == "1"
    assert "SPOTTER_DISABLE" not in capture_env
    normal_args, normal_env = calls[1]
    assert "--json" not in normal_args
    assert "--dangerously-bypass-hook-trust" not in normal_args
    assert normal_env["SPOTTER_DISABLE"] == "1"
    assert "SPOTTER_CAPTURE_ONLY" not in normal_env


def test_replay_source_reports_missing_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    stdout = json.dumps({"type": "thread.started", "thread_id": "missing-source"})

    session_id, error = task_corpus._replay_source(stdout)

    assert session_id is None
    assert error == "Spotter journal missing or unreadable for session missing-source"


def test_task_batch_resumes_without_rerunning_completed_arms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(task_corpus, "_codex_version", lambda: None)
    calls = 0

    def solve(
        workspace: Path,
        prompt: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
        sandbox: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        (workspace / "parser.py").write_text("def parse(): return 1\n")
        shutil.rmtree(workspace / "__pycache__", ignore_errors=True)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(task_corpus, "_run_task_agent", solve)
    output, _ = run_task_batch(path, "Verify first.")
    rows = output.read_text().splitlines()
    header = json.loads(rows[0])
    header.pop("reasoning_effort")
    header.pop("schema")
    header.pop("schema_version")
    result = json.loads(rows[1])
    result.pop("schema")
    result.pop("schema_version")
    output.write_text(json.dumps(header) + "\n" + json.dumps(result) + "\n")
    calls = 0

    resumed_output, results = run_task_batch(path, "Verify first.", resume=output)

    assert resumed_output == output
    assert calls == 1
    assert len(results) == 2
    assert len({(result.task_id, result.arm) for result in results}) == 2
    persisted = [json.loads(line) for line in output.read_text().splitlines()]
    assert "schema" not in persisted[0] and "schema" not in persisted[1]
    assert all(row["schema"] == TASK_BATCH_SCHEMA for row in persisted[2:])


@pytest.mark.parametrize(
    ("schema", "version", "message"),
    [
        (TASK_BATCH_SCHEMA, TASK_BATCH_SCHEMA_VERSION + 1, "understands"),
        ("someone.else", TASK_BATCH_SCHEMA_VERSION, "unsupported schema"),
    ],
)
def test_incompatible_task_batch_is_refused_before_preflight_or_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema: str,
    version: int,
    message: str,
) -> None:
    task_set = _corpus(tmp_path / "corpus")
    batch = tmp_path / "batch.jsonl"
    batch.write_text(
        json.dumps(
            {
                "schema": schema,
                "schema_version": version,
                "result_schema_version": version,
                "meta": True,
                "run_id": "future",
            }
        )
        + "\n"
    )
    before = batch.read_bytes()
    monkeypatch.setattr(
        task_corpus,
        "_preflight_task",
        lambda task: (_ for _ in ()).throw(AssertionError("preflight must not run")),
    )

    with pytest.raises(TaskCorpusError, match=message):
        run_task_batch(task_set, "guidance", resume=batch)
    assert batch.read_bytes() == before


def test_task_batch_repairs_a_torn_final_row_before_resuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(task_corpus, "_codex_version", lambda: None)

    def solve(
        workspace: Path,
        prompt: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
        sandbox: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        (workspace / "parser.py").write_text("def parse(): return 1\n")
        shutil.rmtree(workspace / "__pycache__", ignore_errors=True)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(task_corpus, "_run_task_agent", solve)
    output, _ = run_task_batch(path, "Verify first.")
    rows = output.read_text().splitlines()
    output.write_text("\n".join(rows[:2]) + '\n{"task_id":')

    _, results = run_task_batch(path, "Verify first.", resume=output)

    assert len(results) == 2
    assert all(json.loads(line) for line in output.read_text().splitlines())


def test_task_batch_refuses_resume_with_changed_conditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(task_corpus, "_codex_version", lambda: None)

    def fail(
        workspace: Path,
        prompt: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
        sandbox: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", "agent failed")

    monkeypatch.setattr(task_corpus, "_run_task_agent", fail)
    output, _ = run_task_batch(path, "Original guidance.")

    with pytest.raises(TaskCorpusError, match="guidance does not match"):
        run_task_batch(path, "Changed guidance.", resume=output)
    with pytest.raises(TaskCorpusError, match="capture_replay_sources does not match"):
        run_task_batch(path, "Original guidance.", resume=output, capture_replay_sources=True)
    with pytest.raises(TaskCorpusError, match="reasoning_effort does not match"):
        run_task_batch(path, "Original guidance.", resume=output, reasoning_effort="low")


def test_task_batch_classifies_agent_timeout_without_running_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(task_corpus, "_codex_version", lambda: None)

    def timeout(
        workspace: Path,
        prompt: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
        sandbox: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("codex", timeout, output="partial", stderr="timed out")

    monkeypatch.setattr(task_corpus, "_run_task_agent", timeout)

    _, results = run_task_batch(path, "Verify first.")

    assert all(result.classification == "TIMEOUT_AGENT" for result in results)
    assert all(result.checks == () for result in results)
    assert all(result.agent_stdout == "partial" for result in results)


def test_task_batch_classifies_per_arm_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _corpus(tmp_path / "corpus")
    task = tmp_path / "corpus" / "tasks" / "parser.toml"
    task.write_text(task.read_text().replace("python3 -m compileall .", "exit 2"))
    _refreeze_task(tmp_path / "corpus", path)
    task_set = validate_task_set(path)
    ready = tuple(
        task_corpus.TaskPreflight(manifest.task_id, PreflightClassification.READY, ())
        for manifest in task_set.tasks
    )
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(task_corpus, "preflight_task_set", lambda _: (task_set, ready))
    monkeypatch.setattr(task_corpus, "_codex_version", lambda: None)

    _, results = run_task_batch(path, "Verify first.")

    assert all(result.classification == "SETUP_FAIL" for result in results)
    assert all(result.setup.returncode == 2 for result in results)


def test_task_batch_cli_requires_paid_run_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _corpus(tmp_path / "corpus")
    with pytest.raises(SystemExit):
        main(["tasks", "run", str(path), "--guidance", "Verify first."])

    output = tmp_path / "batch.jsonl"
    captured: dict[str, object] = {}

    def run(*args: object, **kwargs: object) -> tuple[Path, tuple[()]]:
        captured.update(kwargs)
        return output, ()

    monkeypatch.setattr(cli, "run_task_batch", run)

    assert (
        main(
            [
                "tasks",
                "run",
                str(path),
                "--guidance",
                "Verify first.",
                "--capture-replay-sources",
                "--reasoning-effort",
                "low",
                "--run",
            ]
        )
        == 0
    )
    assert captured["capture_replay_sources"] is True
    assert captured["reasoning_effort"] == "low"
    assert f"results written to {output}" in capsys.readouterr().out
