import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from spotter.snapshot import (
    SnapshotError,
    StepJournal,
    restore_snapshot,
    snapshot_worktree,
)
from spotter.trace import TraceEvent


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def test_snapshot_captures_untracked_and_restore_does_not_touch_worktree(
    repo: Path, tmp_path: Path
) -> None:
    (repo / "tracked.txt").write_text("v1")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    (repo / "untracked.txt").write_text("never committed")
    (repo / "tracked.txt").write_text("v2 dirty")

    sha = snapshot_worktree(repo)

    # snapshotting must not mutate the user's state
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert "untracked.txt" in status and "tracked.txt" in status

    dest = tmp_path / "restore"
    restore_snapshot(repo, sha, dest)
    assert (dest / "untracked.txt").read_text() == "never committed"
    assert (dest / "tracked.txt").read_text() == "v2 dirty"


def test_snapshot_works_on_empty_repo_without_head(repo: Path) -> None:
    (repo / "only.txt").write_text("x")
    assert snapshot_worktree(repo)


def test_snapshot_outside_git_repo_fails_loudly(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SnapshotError):
        snapshot_worktree(plain)


def test_restore_refuses_existing_destination(repo: Path, tmp_path: Path) -> None:
    (repo / "a.txt").write_text("a")
    sha = snapshot_worktree(repo)
    dest = tmp_path / "exists"
    dest.mkdir()
    with pytest.raises(SnapshotError, match="already exists"):
        restore_snapshot(repo, sha, dest)


def test_journal_roundtrip_prefix_and_resume(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = StepJournal(path)
    journal.record(TraceEvent("session_start"))
    journal.record(TraceEvent("tool_proposal", {"command": "pytest"}), snapshot="abc123")

    records = StepJournal.load(path)
    assert [r.step for r in records] == [0, 1]
    assert records[1].snapshot == "abc123"
    assert StepJournal.prefix(records, 1)[-1].event.kind == "session_start"

    # resuming appends with continued numbering instead of restarting at 0
    resumed = StepJournal(path)
    resumed.record(TraceEvent("tool_result"))
    assert [r.step for r in StepJournal.load(path)] == [0, 1, 2]


def test_journal_roundtrips_connection_epoch(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    StepJournal(path).record(TraceEvent("thread_started", connection_epoch=7, arrival_seq=3))

    event = StepJournal.load(path)[0].event
    assert event.connection_epoch == 7
    assert event.arrival_seq == 3


def test_journal_stamps_monotonic_receipt_clock_domain(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = StepJournal(path)
    first = journal.record(TraceEvent("first"))
    second = journal.record(TraceEvent("second"))

    loaded = StepJournal.load(path)

    assert first.event.observed_monotonic_ns is not None
    assert second.event.observed_monotonic_ns is not None
    assert first.event.observed_monotonic_ns <= second.event.observed_monotonic_ns
    assert first.event.monotonic_clock_id
    assert first.event.monotonic_clock_id == second.event.monotonic_clock_id
    assert loaded[0].event.observed_monotonic_ns == first.event.observed_monotonic_ns
    assert loaded[0].event.monotonic_clock_id == first.event.monotonic_clock_id


def test_journal_preserves_preobserved_timing_for_async_writes(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    event = TraceEvent(
        "control_dispatch_started",
        observed_monotonic_ns=123,
        monotonic_clock_id="runtime-1",
    )

    record = StepJournal(path).record(event, observed_at=456.0)
    loaded = StepJournal.load(path)[0]

    assert record.at == loaded.at == 456.0
    assert record.event.observed_monotonic_ns == 123
    assert loaded.event.observed_monotonic_ns == 123
    assert record.event.monotonic_clock_id == "runtime-1"
    assert loaded.event.monotonic_clock_id == "runtime-1"


def test_proposal_number_does_not_mutate_input_event(tmp_path: Path) -> None:
    journal = StepJournal(tmp_path / "journal.jsonl")
    event = TraceEvent("tool_proposal", {"tool": "Bash"})
    record = journal.record(event)
    assert "proposal_number" not in event.payload
    assert record.event.payload["proposal_number"] == 1


def test_journal_tolerates_torn_tail_but_rejects_reordering(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = StepJournal(path)
    journal.record(TraceEvent("session_start"))
    with path.open("a") as f:
        f.write('{"step": 1, "kind": "torn"')  # crash mid-write
    assert len(StepJournal.load(path)) == 1  # valid prefix survives
    resumed = StepJournal(path)
    resumed.record(TraceEvent("after_resume"))
    assert [r.event.kind for r in StepJournal.load(path)] == ["session_start", "after_resume"]

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"step": 5, "kind": "x", "payload": {}, "snapshot": null}\n')
    with pytest.raises(SnapshotError, match="mismatch"):
        StepJournal.load(bad)

    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text('{"step": 0, bad}\n')
    with pytest.raises(SnapshotError, match="invalid journal record"):
        StepJournal(corrupt).record(TraceEvent("later"))


def test_concurrent_journal_writes_keep_unique_steps(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: StepJournal(path).record(TraceEvent("event", {"i": i})), range(20)))
    assert [r.step for r in StepJournal.load(path)] == list(range(20))
