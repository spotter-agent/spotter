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
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
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
    assert main(["status"]) == 1
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
    assert main(["status"]) == 1
    assert "match credential patterns" in capsys.readouterr().out


def test_snapshots_are_pruned_once_after_journals_go(
    repo: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #58 review, P0: the previous version pruned snapshots before
    deleting journals and again after — the opposite of its own comment."""
    import os

    from spotter import cli

    sha = snapshot_worktree(repo)
    journal = journal_path({"session_id": "old"})
    StepJournal(journal).record(TraceEvent("tool_proposal", {"cwd": str(repo)}), snapshot=sha)
    old = 40 * 86400
    os.utime(journal, (journal.stat().st_atime - old, journal.stat().st_mtime - old))

    calls: list[set[str]] = []
    from spotter.snapshot import prune_snapshots as real_prune

    def counted(repo_path: Path, referenced: set[str], **kwargs: object) -> object:
        calls.append(set(referenced))
        return real_prune(repo_path, referenced, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "prune_snapshots", counted)
    assert (
        main(["prune", "--repo", str(repo), "--journals", "--max-age-days", "30", "--apply"]) == 0
    )

    assert len(calls) == 1, "snapshots were pruned more than once"
    assert calls[0] == set(), "the deleted journal's references were still counted"


def test_journal_deletion_holds_the_journal_lock(repo: Path, home: Path) -> None:
    """A hook appending under the session lock must not have the file deleted
    from under it (PR #58 review, P0)."""
    import os
    from fcntl import LOCK_EX, LOCK_NB, flock

    journal = journal_path({"session_id": "busy"})
    StepJournal(journal).record(TraceEvent("x"))
    old = 40 * 86400
    os.utime(journal, (journal.stat().st_atime - old, journal.stat().st_mtime - old))

    lock_path = journal.with_suffix(journal.suffix + ".lock")
    with lock_path.open("a") as held:
        flock(held, LOCK_EX)
        # The deleter must block on this lock; prove it cannot take it now.
        with lock_path.open("a") as probe, pytest.raises(OSError):
            flock(probe, LOCK_EX | LOCK_NB)

    # Once released, deletion proceeds — and the lock file deliberately stays.
    assert (
        main(["prune", "--repo", str(repo), "--journals", "--max-age-days", "30", "--apply"]) == 0
    )
    assert not journal.exists()
    assert lock_path.exists(), "removing the lock lets the next writer create a second inode"


def test_active_review_keeps_a_stale_journal(repo: Path, home: Path) -> None:
    """A reviewer loads the journal before its model call, so pruning it during
    that call would let the eventual verdict recreate only stale data."""
    import os
    from fcntl import LOCK_EX, flock

    journal = journal_path({"session_id": "reviewing"})
    StepJournal(journal).record(TraceEvent("x"))
    old = 40 * 86400
    os.utime(journal, (journal.stat().st_atime - old, journal.stat().st_mtime - old))

    review_lock = journal.with_suffix(".review.lock")
    with review_lock.open("a") as held:
        flock(held, LOCK_EX)
        assert (
            main(["prune", "--repo", str(repo), "--journals", "--max-age-days", "30", "--apply"])
            == 0
        )
        assert journal.exists()

    assert (
        main(["prune", "--repo", str(repo), "--journals", "--max-age-days", "30", "--apply"]) == 0
    )
    assert not journal.exists()
    assert review_lock.exists()


def test_worktree_removal_failure_does_not_orphan_git_metadata(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """PR #58 review, P1: deleting the directory anyway leaves the parent
    repository registering a worktree that is gone — the original defect."""
    from spotter.cli import _remove_worktree
    from spotter.experiment import forks_dir

    orphan = forks_dir() / "not-a-worktree"
    orphan.mkdir(parents=True)
    (orphan / "keep.txt").write_text("still here")

    _remove_worktree(orphan)

    assert orphan.exists(), "directory was deleted despite git refusing"
    assert "could not remove" in capsys.readouterr().err


def test_a_blocked_writer_keeps_the_same_lock_inode(repo: Path, home: Path) -> None:
    """PR #58 review, P0: a writer already blocked on the old inode wakes up
    after deletion; if the lock path is then removed, the next writer creates
    a fresh inode and the two are serialised by nothing."""
    import os
    import sys as _sys
    import time as _time

    journal = journal_path({"session_id": "waited"})
    StepJournal(journal).record(TraceEvent("x"))
    lock_path = journal.with_suffix(journal.suffix + ".lock")
    old = 40 * 86400
    os.utime(journal, (journal.stat().st_atime - old, journal.stat().st_mtime - old))

    # A writer that opens the current lock inode, then blocks on it.
    waiter = subprocess.Popen(
        [
            _sys.executable,
            "-c",
            (
                "import sys, time\n"
                "from fcntl import LOCK_EX, flock\n"
                "handle = open(sys.argv[1], 'a')\n"  # holds the OLD inode
                "print('opened', flush=True)\n"
                "time.sleep(0.6)\n"
                "flock(handle, LOCK_EX)\n"
                "import os\n"
                "print(os.fstat(handle.fileno()).st_ino, flush=True)\n"
            ),
            str(lock_path),
        ],
        cwd=Path(__file__).parent.parent,
        stdout=subprocess.PIPE,
        text=True,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin", "SPOTTER_HOME": str(home)},
    )
    assert waiter.stdout is not None
    waiter.stdout.readline()  # wait until it has the old inode open
    inode_before = lock_path.stat().st_ino

    assert (
        main(["prune", "--repo", str(repo), "--journals", "--max-age-days", "30", "--apply"]) == 0
    )
    waiter_inode = int(waiter.stdout.readline().strip())
    waiter.wait(timeout=10)
    _time.sleep(0.05)

    # The path still resolves to the inode the blocked writer holds, so a new
    # writer arriving now contends with it rather than beside it.
    assert lock_path.exists()
    assert lock_path.stat().st_ino == inode_before == waiter_inode


def test_a_journal_written_while_prune_waits_is_kept(repo: Path, home: Path) -> None:
    """PR #58 review, P0: holding the session lock proves nobody is writing
    now; it does not prove the staleness decision taken before the wait is
    still true."""
    import os
    import sys as _sys

    journal = journal_path({"session_id": "revived"})
    StepJournal(journal).record(TraceEvent("x"))
    old = 40 * 86400
    os.utime(journal, (journal.stat().st_atime - old, journal.stat().st_mtime - old))

    # A writer that takes the session lock, appends, and only then releases —
    # exactly the window the deleter blocks in.
    writer = subprocess.Popen(
        [
            _sys.executable,
            "-c",
            (
                "import sys, time\n"
                "from pathlib import Path\n"
                "from fcntl import LOCK_EX, LOCK_UN, flock\n"
                "from spotter.snapshot import StepJournal\n"
                "from spotter.trace import TraceEvent\n"
                "lock = open(sys.argv[1] + '.lock', 'a')\n"
                "flock(lock, LOCK_EX)\n"
                "print('locked', flush=True)\n"
                "time.sleep(1.0)\n"
                'Path(sys.argv[1]).write_text(\'{"step": 0, "kind": "live",'
                ' "payload": {}, "snapshot": null}\\n\')\n'
                "flock(lock, LOCK_UN)\n"
            ),
            str(journal),
        ],
        cwd=Path(__file__).parent.parent,
        stdout=subprocess.PIPE,
        text=True,
        env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin", "SPOTTER_HOME": str(home)},
    )
    assert writer.stdout is not None
    writer.stdout.readline()  # the writer now holds the lock

    assert (
        main(["prune", "--repo", str(repo), "--journals", "--max-age-days", "30", "--apply"]) == 0
    )
    writer.wait(timeout=20)

    assert journal.exists(), "a journal written during the wait was deleted anyway"
    assert [r.event.kind for r in StepJournal.load(journal)] == ["live"]


def test_a_surviving_journal_keeps_its_snapshots(
    repo: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the staleness re-check spares a journal, excluding it from the
    reference set would prune snapshots it still points at."""
    import os

    sha = snapshot_worktree(repo)
    journal = journal_path({"session_id": "revived"})
    StepJournal(journal).record(TraceEvent("tool_proposal", {"cwd": str(repo)}), snapshot=sha)
    old = 40 * 86400
    os.utime(journal, (journal.stat().st_atime - old, journal.stat().st_mtime - old))

    def refuse(target: Path, cutoff: float) -> bool:
        os.utime(target, None)  # someone wrote to it while we waited
        return False

    monkeypatch.setattr("spotter.cli._delete_journal", refuse)
    assert (
        main(["prune", "--repo", str(repo), "--journals", "--max-age-days", "30", "--apply"]) == 0
    )

    assert journal.exists()
    assert sha in snapshot_references(journal.parent, repo)
    assert prune_snapshots(repo, {sha}) == []
