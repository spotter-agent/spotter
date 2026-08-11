"""Storage hygiene: fork leaks, journal retention, and honest status (#46, #41)."""

import json
import subprocess
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.experiment import list_forks
from spotter.hook import journal_path
from spotter.snapshot import (
    StepJournal,
    prune_snapshots,
    snapshot_references,
    snapshot_worktree,
    stale_journals,
)
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "spotter"))
    return tmp_path / "spotter"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    (repo / "a.txt").write_text("v1")
    return repo


def test_prepare_only_forks_are_listed_and_removable(repo: Path, home: Path) -> None:
    """A fork made without --run was never cleaned up by anything."""
    from spotter.experiment import forks_dir

    sha = snapshot_worktree(repo)
    dest = forks_dir() / "abc123"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(dest), sha],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert [p.name for p in list_forks()] == ["abc123"]

    assert main(["prune", "--repo", str(repo), "--forks"]) == 0  # dry run
    assert list_forks()
    assert main(["prune", "--repo", str(repo), "--forks", "--apply"]) == 0
    assert list_forks() == []
    # git no longer lists a worktree at that path, so the path is reusable
    listed = subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True)
    assert "abc123" not in listed.stdout


def test_journal_retention_reports_before_it_deletes(home: Path) -> None:
    journal = journal_path({"session_id": "old"})
    StepJournal(journal).record(TraceEvent("tool_proposal", {"command": "ls"}))
    import os

    old = 40 * 86400
    os.utime(journal, (journal.stat().st_atime - old, journal.stat().st_mtime - old))

    assert [p.name for p in stale_journals(journal.parent, 30)] == ["old.jsonl"]
    assert stale_journals(journal.parent, 90) == []


def test_deleting_a_journal_and_its_snapshots_happens_in_one_pass(repo: Path, home: Path) -> None:
    """Deleting a journal is what orphans its snapshots, so the order matters:
    journals first, then a recomputed snapshot prune."""
    import os

    sha = snapshot_worktree(repo)
    journal = journal_path({"session_id": "old"})
    StepJournal(journal).record(TraceEvent("tool_proposal", {"cwd": str(repo)}), snapshot=sha)
    old = 40 * 86400
    os.utime(journal, (journal.stat().st_atime - old, journal.stat().st_mtime - old))

    references = snapshot_references(journal.parent, repo)
    assert sha in references  # still referenced while the journal lives

    assert (
        main(["prune", "--repo", str(repo), "--journals", "--max-age-days", "30", "--apply"]) == 0
    )
    assert not journal.exists()
    assert prune_snapshots(repo, set(snapshot_references(journal.parent, repo))) == []


def test_journals_flag_requires_an_age_window(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["prune", "--repo", str(repo), "--journals"]) == 1
    assert "requires --max-age-days" in capsys.readouterr().err


def test_status_reports_never_observed(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status"]) == 1  # non-zero: nothing has ever been recorded
    assert "nothing has ever been recorded" in capsys.readouterr().err


def test_status_tightens_permissions_it_finds_loose(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    StepJournal(journal_path({"session_id": "s"})).record(TraceEvent("x"))
    home.chmod(0o755)
    assert main(["status"]) == 0
    assert (home.stat().st_mode & 0o077) == 0
    assert "tightened from" in capsys.readouterr().out


def test_status_warns_about_pre_redaction_credentials(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = journal_path({"session_id": "legacy"})
    record = {
        "step": 0,
        "kind": "tool_proposal",
        "payload": {"command": "export API_KEY=leaked12345"},
        "snapshot": None,
    }
    journal.write_text(json.dumps(record) + "\n")
    assert main(["status"]) == 0
    assert "match credential patterns" in capsys.readouterr().out
