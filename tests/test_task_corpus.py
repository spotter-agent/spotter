import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import spotter.cli as cli
import spotter.task_corpus as task_corpus
from spotter.cli import main
from spotter.task_corpus import (
    PreflightClassification,
    TaskCorpusError,
    file_digest,
    fixture_digest,
    preflight_task_set,
    run_task_batch,
    summarize_task_batch,
    validate_task_set,
)


def _corpus(root: Path) -> Path:
    fixture = root / "fixtures" / "parser"
    fixture.mkdir(parents=True)
    (fixture / "parser.py").write_text("def parse(): return 0\n")
    (fixture / "parser.good").write_text("def parse(): return 1\n")
    (fixture / "check.py").write_text("from parser import parse\nassert parse() == 1\n")
    task = root / "tasks" / "parser.toml"
    task.parent.mkdir()
    task.write_text(
        f'''task_schema_version = 1
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
        f'''task_set_schema_version = 1
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
    assert main(["tasks", "validate", str(path)]) == 0
    assert "validated spotter-dev v1 (dev): 1 task(s)" in capsys.readouterr().out


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


@pytest.mark.parametrize("name", ["dev-v1.toml", "validation-v1.toml"])
def test_repo_corpus_is_frozen_and_preflight_ready(name: str) -> None:
    path = Path(__file__).parents[1] / "corpus" / name

    task_set, results = preflight_task_set(path)

    assert task_set.tasks
    assert all(result.classification == PreflightClassification.READY for result in results)


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
        sandbox: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        prompts.append(prompt)
        (workspace / "parser.py").write_text("def parse(): return 1\n")
        shutil.rmtree(workspace / "__pycache__", ignore_errors=True)
        return subprocess.CompletedProcess([], 0, "agent output", "")

    monkeypatch.setattr(task_corpus, "_run_task_agent", solve)

    output, results = run_task_batch(path, "Inspect the failing check first.")

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
    assert rows[-1]["complete"] is True


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
    output.write_text("\n".join(rows[:2]) + "\n")
    calls = 0

    resumed_output, results = run_task_batch(path, "Verify first.", resume=output)

    assert resumed_output == output
    assert calls == 1
    assert len(results) == 2
    assert len({(result.task_id, result.arm) for result in results}) == 2


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
        sandbox: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "", "agent failed")

    monkeypatch.setattr(task_corpus, "_run_task_agent", fail)
    output, _ = run_task_batch(path, "Original guidance.")

    with pytest.raises(TaskCorpusError, match="guidance does not match"):
        run_task_batch(path, "Changed guidance.", resume=output)


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
    monkeypatch.setattr(cli, "run_task_batch", lambda *args, **kwargs: (output, ()))

    assert main(["tasks", "run", str(path), "--guidance", "Verify first.", "--run"]) == 0
    assert f"results written to {output}" in capsys.readouterr().out
