import subprocess
import sys
from pathlib import Path

import pytest

from spotter.hook import journal_path
from spotter.snapshot import (
    SnapshotError,
    StepJournal,
    prune_snapshots,
    referenced_snapshots,
    snapshot_worktree,
)
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def spotter_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "spotter"
    monkeypatch.setenv("SPOTTER_HOME", str(home))
    return home


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("v1")
    return repo


def _refs(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/spotter/steps"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(out.split())


def test_prune_keeps_referenced_and_drops_orphans(repo: Path) -> None:
    kept = snapshot_worktree(repo)
    (repo / "a.txt").write_text("v2")
    orphan = snapshot_worktree(repo)
    StepJournal(journal_path({"session_id": "s1"})).record(
        TraceEvent("tool_proposal"), snapshot=kept
    )

    referenced = referenced_snapshots(journal_path({"session_id": "s1"}).parent)

    # dry-run reports but deletes nothing
    assert prune_snapshots(repo, referenced) == [orphan]
    assert len(_refs(repo)) == 2

    assert prune_snapshots(repo, referenced, apply=True) == [orphan]
    assert _refs(repo) == {f"refs/spotter/steps/{kept}"}


def test_unreadable_journal_aborts_prune(repo: Path, spotter_home: Path) -> None:
    snapshot_worktree(repo)
    sessions = spotter_home / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "bad.jsonl").write_text('{"step": 7, "kind": "x", "payload": {}}\n')
    with pytest.raises(SnapshotError):
        referenced_snapshots(sessions)  # unknown references → refuse to guess


def test_prune_never_touches_other_refs(repo: Path, spotter_home: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/precious", head], cwd=repo, check=True)
    (spotter_home / "sessions").mkdir(parents=True)

    prune_snapshots(repo, set(), apply=True)

    branches = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/heads"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "refs/heads/precious" in branches


def test_journal_survives_concurrent_processes(tmp_path: Path) -> None:
    """flock is a cross-process guarantee; threads cannot exercise it (issue #6)."""
    journal = tmp_path / "journal.jsonl"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from spotter.snapshot import StepJournal\n"
        "from spotter.trace import TraceEvent\n"
        "j = StepJournal(Path(sys.argv[1]))\n"
        "for i in range(10):\n"
        "    j.record(TraceEvent('event'))\n"
    )
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(journal)],
            cwd=Path(__file__).parent.parent,
            env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
        )
        for _ in range(4)
    ]
    assert all(worker.wait() == 0 for worker in workers)
    records = StepJournal.load(journal)
    assert [r.step for r in records] == list(range(40))  # unique, monotonic, no gaps
