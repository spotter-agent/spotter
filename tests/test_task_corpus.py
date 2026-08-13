import hashlib
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.task_corpus import (
    PreflightClassification,
    TaskCorpusError,
    file_digest,
    fixture_digest,
    preflight_task_set,
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
