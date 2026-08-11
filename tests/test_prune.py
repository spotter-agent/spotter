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


# --- Regression tests for the PR #12 review findings: each one hits the exact
# --- window the review said the previous tests avoided.


def test_race_hook_snapshot_vs_prune_is_serialized(repo: Path, spotter_home: Path) -> None:
    """P0: a ref pinned but not yet journaled must survive a concurrent prune."""
    (spotter_home / "sessions").mkdir(parents=True, exist_ok=True)
    ready = spotter_home / "ready"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from spotter.snapshot import StepJournal, global_lock, snapshot_worktree\n"
        "from spotter.trace import TraceEvent\n"
        "repo, home = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "with global_lock(home):\n"
        "    sha = snapshot_worktree(repo)  # ref exists, journal not yet written\n"
        "    (home / 'ready').write_text(sha)\n"
        "    time.sleep(1.0)  # the window prune must NOT be able to enter\n"
        "    journal = StepJournal(home / 'sessions' / 's-race.jsonl')\n"
        "    journal.record(TraceEvent('tool_proposal', {'cwd': str(repo)}), snapshot=sha)\n"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", script, str(repo), str(spotter_home)],
        cwd=Path(__file__).parent.parent,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    try:
        deadline = 50
        while not ready.exists() and deadline:
            deadline -= 1
            import time

            time.sleep(0.1)
        assert ready.exists(), "worker never reached the race window"
        from spotter.snapshot import global_lock

        with global_lock(spotter_home):  # blocks until the worker journals
            referenced = referenced_snapshots(spotter_home / "sessions", repo)
            pruned = prune_snapshots(repo, referenced, apply=True)
    finally:
        assert worker.wait(timeout=30) == 0
    assert pruned == []
    assert _refs(repo) == {f"refs/spotter/steps/{ready.read_text()}"}


def test_torn_tail_with_snapshot_aborts_prune(repo: Path, spotter_home: Path) -> None:
    """P0: a torn tail may hold the newest sha; strict read refuses to guess."""
    sha = snapshot_worktree(repo)
    sessions = spotter_home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    torn = f'{{"step": 0, "kind": "tool_proposal", "payload": {{}}, "snapshot": "{sha}"'
    (sessions / "s-torn.jsonl").write_text(torn)  # no newline: crash mid-write
    with pytest.raises(SnapshotError, match="torn tail"):
        referenced_snapshots(sessions, repo)


def test_apply_deletes_all_doomed_refs_in_one_transaction(repo: Path) -> None:
    """P1: multiple deletions go through one update-ref --stdin transaction."""
    shas = []
    for n in range(3):
        (repo / "a.txt").write_text(f"v{n}")
        shas.append(snapshot_worktree(repo))
    assert sorted(prune_snapshots(repo, set(), apply=True)) == sorted(shas)
    assert _refs(repo) == set()


def test_trigger_resolution_survives_interleaved_journals() -> None:
    """P1: gate events resolve their proposal by tool_use_id, not adjacency."""
    from spotter.cli import _trigger_for
    from spotter.snapshot import StepRecord

    records = [
        StepRecord(
            0, TraceEvent("tool_proposal", {"tool_use_id": "A", "command": "rm -rf /"}), None
        ),
        StepRecord(1, TraceEvent("tool_proposal", {"tool_use_id": "B", "command": "pytest"}), None),
        StepRecord(
            2, TraceEvent("gate_shadow_block", {"rule": "rm_root", "tool_use_id": "A"}), None
        ),
    ]
    assert _trigger_for(records, records[2])["command"] == "rm -rf /"


def test_referenced_snapshots_filters_by_repo(
    repo: Path, tmp_path: Path, spotter_home: Path
) -> None:
    """P2: a sha referenced only by another repo's journal is prunable here;
    a record with unknown provenance is conservatively kept everywhere."""
    sha = snapshot_worktree(repo)
    sessions = spotter_home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    other_repo = tmp_path / "elsewhere"
    other_repo.mkdir()
    StepJournal(sessions / "s-other.jsonl").record(
        TraceEvent("tool_proposal", {"cwd": str(other_repo)}), snapshot=sha
    )
    assert referenced_snapshots(sessions, repo) == set()  # other repo's claim doesn't count here
    assert referenced_snapshots(sessions, other_repo) == {sha}

    StepJournal(sessions / "s-nocwd.jsonl").record(TraceEvent("tool_proposal"), snapshot=sha)
    assert referenced_snapshots(sessions, repo) == {sha}  # unknown provenance → keep
