import hashlib
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.task_corpus import TaskCorpusError, file_digest, fixture_digest, validate_task_set


def _corpus(root: Path) -> Path:
    fixture = root / "fixtures" / "parser"
    fixture.mkdir(parents=True)
    (fixture / "parser.py").write_text("def parse(): return None\n")
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
command = "python -m compileall ."
timeout_s = 30

[precheck]
command = "python check.py"
timeout_s = 30
expected = "failure"

[[checks]]
id = "task-resolution"
command = "python check.py"
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
