import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from spotter.hook import journal_path
from spotter.snapshot import (
    SnapshotError,
    StepJournal,
    prune_snapshots,
    referenced_snapshots,
    snapshot_references,
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
    assert [p.sha for p in prune_snapshots(repo, referenced)] == [orphan]
    assert len(_refs(repo)) == 2

    assert [p.sha for p in prune_snapshots(repo, referenced, apply=True)] == [orphan]
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
    assert [p.sha for p in pruned] == []
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
    assert sorted(p.sha for p in prune_snapshots(repo, set(), apply=True)) == sorted(shas)
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


# --- issue #7: no-op events must not mint refs; retention must be bounded ---


def test_unchanged_tree_reuses_the_previous_snapshot(repo: Path) -> None:
    first = snapshot_worktree(repo)
    again = snapshot_worktree(repo, first)  # nothing touched the tree
    assert again == first
    assert len(_refs(repo)) == 1  # no second ref for a no-op step

    (repo / "a.txt").write_text("changed")
    third = snapshot_worktree(repo, first)
    assert third != first and len(_refs(repo)) == 2


def test_dedup_falls_back_to_a_new_snapshot_when_previous_is_unusable(repo: Path) -> None:
    """Best-effort: a bogus previous costs a redundant ref, never a wrong one."""
    sha = snapshot_worktree(repo, "0" * 40)
    assert sha and len(_refs(repo)) == 1


def test_journal_reports_its_last_snapshot_for_dedup(repo: Path, tmp_path: Path) -> None:
    journal = StepJournal(tmp_path / "j.jsonl")
    assert journal.last_snapshot() is None
    sha = snapshot_worktree(repo)
    journal.record(TraceEvent("tool_proposal", {}), snapshot=sha)
    journal.record(TraceEvent("tool_result", {}))  # a later unsnapshotted step
    assert journal.last_snapshot() == sha


def test_referenced_snapshots_survive_by_default_but_expire_on_policy(repo: Path) -> None:
    kept = snapshot_worktree(repo)
    referenced = {kept}
    assert prune_snapshots(repo, referenced) == []  # referenced: kept indefinitely

    future = int(time.time()) + 40 * 86400  # pretend 40 days have passed
    pruned = prune_snapshots(repo, referenced, max_age_days=30, now=future)
    assert [(p.sha, p.reason) for p in pruned] == [(kept, "expired")]
    assert len(_refs(repo)) == 1  # dry-run still deletes nothing

    prune_snapshots(repo, referenced, apply=True, max_age_days=30, now=future)
    assert _refs(repo) == set()


def test_expiry_window_keeps_recent_referenced_snapshots(repo: Path) -> None:
    sha = snapshot_worktree(repo)
    assert prune_snapshots(repo, {sha}, max_age_days=30) == []


# --- issue #6: a real crash mid-append, not a simulated one ---


def _crash_child(journal: Path, patch: str) -> int:
    """Run a real record() in a child that SIGKILLs itself inside the append.

    `patch` names where to die, so the crash lands in the production path
    rather than in a hand-written partial line (PR #19 review, P2).
    """
    script = (
        "import os, signal, sys\n"
        "from pathlib import Path\n"
        "from spotter.snapshot import StepJournal\n"
        "from spotter.trace import TraceEvent\n"
        "def die(*args, **kwargs):\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
        f"{patch}\n"
        "StepJournal(Path(sys.argv[1])).record(TraceEvent('crashing'))\n"
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", script, str(journal)],
        cwd=Path(__file__).parent.parent,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    return worker.wait()


def test_sigkill_between_write_and_fsync_keeps_the_journal_usable() -> None:
    """The record is on disk but the sidecar was never updated: the next
    append must notice the stale sidecar and keep numbering correct."""
    with tempfile.TemporaryDirectory() as scratch:
        journal = Path(scratch) / "j.jsonl"
        StepJournal(journal).record(TraceEvent("before_crash"))
        assert _crash_child(journal, "os.fsync = die") == -signal.SIGKILL

        StepJournal(journal).record(TraceEvent("after_crash"))
        recovered = StepJournal.load(journal)
        assert [r.step for r in recovered] == [0, 1, 2]
        assert [r.event.kind for r in recovered] == ["before_crash", "crashing", "after_crash"]


def test_sigkill_between_fsync_and_sidecar_update_keeps_numbering() -> None:
    """Durable record, stale sidecar — the size check must catch it."""
    with tempfile.TemporaryDirectory() as scratch:
        journal = Path(scratch) / "j.jsonl"
        StepJournal(journal).record(TraceEvent("before_crash"))
        assert (
            _crash_child(journal, "from pathlib import Path as _P\n_P.write_text = die")
            == -signal.SIGKILL
        )

        state = journal.with_suffix(journal.suffix + ".state")
        StepJournal(journal).record(TraceEvent("after_crash"))
        assert [r.step for r in StepJournal.load(journal)] == [0, 1, 2]
        assert state.exists()  # rebuilt from the full load


def test_torn_tail_from_a_partial_write_is_repaired(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    StepJournal(journal).record(TraceEvent("before_crash"))
    with journal.open("a") as handle:
        handle.write('{"step": 1, "kind": "torn", "payl')  # half a record
    assert [r.event.kind for r in StepJournal.load(journal)] == ["before_crash"]
    StepJournal(journal).record(TraceEvent("after_crash"))
    recovered = StepJournal.load(journal)
    assert [r.step for r in recovered] == [0, 1]
    assert [r.event.kind for r in recovered] == ["before_crash", "after_crash"]


def test_sigkill_before_any_write_leaves_the_journal_untouched(tmp_path: Path) -> None:
    """A record is complete or absent — never half-applied to step numbering."""
    journal = tmp_path / "j.jsonl"
    StepJournal(journal).record(TraceEvent("before_crash"))
    size = journal.stat().st_size
    script = (
        "import os, signal, sys\n"
        "from pathlib import Path\n"
        "from spotter.snapshot import StepJournal\n"
        "from spotter.trace import TraceEvent\n"
        "journal = StepJournal(Path(sys.argv[1]))\n"
        "os.kill(os.getpid(), signal.SIGKILL)\n"  # dies before any write
    )
    worker = subprocess.Popen(
        [sys.executable, "-c", script, str(journal)],
        cwd=Path(__file__).parent.parent,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin"},
    )
    worker.wait()
    assert journal.stat().st_size == size
    StepJournal(journal).record(TraceEvent("after"))
    assert [r.step for r in StepJournal.load(journal)] == [0, 1]


def test_reused_snapshot_is_repinned_after_retention_prune(repo: Path) -> None:
    """PR #19 review P0: a pruned commit stays resolvable until gc, so dedup
    could hand the journal a sha that gc then destroys."""
    sha = snapshot_worktree(repo)
    prune_snapshots(repo, {sha}, apply=True, max_age_days=30, now=2**31)
    assert _refs(repo) == set()  # ref gone, object still resolvable

    again = snapshot_worktree(repo, sha)  # unchanged tree -> dedup path
    assert again == sha
    assert _refs(repo) == {f"refs/spotter/steps/{sha}"}  # re-pinned, not dangling

    subprocess.run(["git", "gc", "--prune=now", "-q"], cwd=repo, check=True)
    assert subprocess.run(["git", "cat-file", "-e", again], cwd=repo).returncode == 0


def test_expiry_names_the_steps_that_lose_forkability(
    repo: Path, spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PR #19 review P1: a bare count hides which recent steps are affected."""
    sha = snapshot_worktree(repo)
    StepJournal(journal_path({"session_id": "s1"})).record(
        TraceEvent("tool_proposal", {"cwd": str(repo)}), snapshot=sha
    )
    references = snapshot_references(journal_path({"session_id": "s1"}).parent, repo)
    assert references[sha] == [("s1", 0)]

    pruned = prune_snapshots(repo, set(references), max_age_days=30, now=2**31)
    assert [(p.sha, p.reason) for p in pruned] == [(sha, "expired")]
