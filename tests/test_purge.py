"""Conservative repository-aware purge preview (#89)."""

import json
import subprocess
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.repository_registry import RepositoryRegistry
from spotter.snapshot import restore_snapshot, snapshot_worktree


@pytest.fixture(autouse=True)
def spotter_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "spotter"
    monkeypatch.setenv("SPOTTER_HOME", str(home))
    return home


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(command, cwd=repository, check=True)
    (repository / "tracked.txt").write_text("one")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    return repository


def test_purge_preview_reports_exact_ref_and_worktree_without_deleting(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    worktree = restore_snapshot(repo, sha, tmp_path / "restored")

    assert main(["purge", "--all", "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "SAFE_OWNED (2)" in output
    assert f"refs/spotter/steps/{sha}" in output
    assert str(worktree) in output
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/spotter/steps/{sha}"],
            cwd=repo,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    assert worktree.exists()


def test_purge_preview_json_is_machine_readable(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)

    assert main(["purge", "--all", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["deletion_supported"] is False
    assert payload["summary"] == {"AMBIGUOUS": 0, "INACCESSIBLE": 0, "SAFE_OWNED": 1}
    assert payload["resources"][0]["resource_id"] == f"refs/spotter/steps/{sha}"


def test_missing_repository_is_inaccessible(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_worktree(repo)
    repo.rename(tmp_path / "moved-without-rediscovery")

    assert main(["purge", "--all", "--dry-run"]) == 1

    output = capsys.readouterr().out
    assert "INACCESSIBLE (1)" in output
    assert "path is unavailable" in output


def test_recreated_repository_path_is_ambiguous(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_worktree(repo)
    repo.rename(tmp_path / "original")
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    assert main(["purge", "--all", "--dry-run"]) == 1

    output = capsys.readouterr().out
    assert "AMBIGUOUS (1)" in output
    assert "different Git repository" in output


def test_changed_ref_target_is_ambiguous(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = snapshot_worktree(repo)
    subprocess.run(
        ["git", "update-ref", f"refs/spotter/steps/{sha}", "HEAD"],
        cwd=repo,
        check=True,
    )

    assert main(["purge", "--all", "--dry-run"]) == 1

    output = capsys.readouterr().out
    assert "AMBIGUOUS (1)" in output
    assert "ref target changed" in output


def test_purge_refuses_non_preview_invocation() -> None:
    with pytest.raises(SystemExit):
        main(["purge", "--all"])
    with pytest.raises(SystemExit):
        main(["purge", "codex", "--all", "--dry-run"])


def test_missing_worktree_with_disagreeing_metadata_is_ambiguous(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    worktree = restore_snapshot(repo, sha, tmp_path / "restored")
    [entry] = RepositoryRegistry().load()
    recorded = next(
        resource for resource in entry.resources if resource.resource_type == "worktree"
    )
    assert recorded.expected_git_dir is not None
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=repo,
        check=True,
    )
    Path(recorded.expected_git_dir).mkdir(parents=True)

    assert main(["purge", "--all", "--dry-run"]) == 1

    output = capsys.readouterr().out
    assert "worktree path and Git administrative metadata disagree" in output
