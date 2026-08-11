"""Step journal and repo snapshots — the recording half of fork/replay (plan P0).

What this deliberately does NOT do: re-run the agent from a prefix. That needs
Codex event ingestion first. Pretending otherwise with a stub would hide the
plan's biggest open risk, so replay stays an explicit gap until the adapter
exists. What we can guarantee today: every step is journaled, snapshots capture
the full worktree (including untracked files), and restore never mutates the
user's checkout.
"""

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

from spotter.trace import TraceEvent


class SnapshotError(RuntimeError):
    """Raised when git snapshot/restore plumbing fails."""


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SnapshotError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def snapshot_worktree(repo: Path) -> str:
    """Snapshot the full worktree (untracked included) without touching the
    user's index or HEAD. Returns the commit sha, pinned under refs/spotter/
    so gc cannot silently delete it out from under a later restore."""
    _git(repo, "rev-parse", "--git-dir")  # fail early if not a git repo
    index_fd, index_path = tempfile.mkstemp(prefix="spotter-index-")
    os.close(index_fd)
    os.unlink(index_path)  # git wants to create the index file itself
    try:
        env = {"GIT_INDEX_FILE": index_path}
        _git(repo, "add", "-A", env=env)
        tree = _git(repo, "write-tree", env=env)
        parent_args: list[str] = []
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if head.returncode == 0:  # unborn HEAD (empty repo) has no parent
            parent_args = ["-p", head.stdout.strip()]
        sha = _git(repo, "commit-tree", tree, *parent_args, "-m", "spotter step snapshot")
        _git(repo, "update-ref", f"refs/spotter/steps/{sha}", sha)
        return sha
    finally:
        if os.path.exists(index_path):
            os.unlink(index_path)


def restore_snapshot(repo: Path, sha: str, dest: Path) -> Path:
    """Materialize a snapshot in a detached worktree at ``dest``.

    Never mutates the user's checkout — a wrong restore into the live tree is
    an unrecoverable failure mode, a separate worktree is merely disk.
    """
    if dest.exists():
        raise SnapshotError(f"restore destination already exists: {dest}")
    _git(repo, "worktree", "add", "--detach", str(dest), sha)
    return dest


@dataclass(frozen=True)
class StepRecord:
    step: int
    event: TraceEvent
    snapshot: str | None


class StepJournal:
    """Append-only JSONL journal of trajectory steps."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, event: TraceEvent, snapshot: str | None = None) -> StepRecord:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("w") as lock:
            flock(lock, LOCK_EX)
            try:
                step = len(self.load(self.path, repair_tail=True)) if self.path.exists() else 0
                record = StepRecord(step, event, snapshot)
                line = json.dumps(
                    {
                        "step": record.step,
                        "kind": event.kind,
                        "payload": event.payload,
                        "snapshot": snapshot,
                    },
                    ensure_ascii=False,
                )
                with self.path.open("a", encoding="utf-8") as journal:
                    journal.write(line + "\n")
                    journal.flush()
                    os.fsync(journal.fileno())
                return record
            finally:
                flock(lock, LOCK_UN)

    @staticmethod
    def load(path: Path, *, repair_tail: bool = False) -> list[StepRecord]:
        records: list[StepRecord] = []
        with path.open("r+" if repair_tail else "r", encoding="utf-8") as journal:
            while True:
                line_start = journal.tell()
                line = journal.readline()
                if not line:
                    break
                if not line.strip():
                    continue  # tolerate a torn trailing write; keep the prefix
                try:
                    raw: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    if line.endswith("\n"):
                        raise SnapshotError(
                            f"invalid journal record at byte {line_start}"
                        ) from None
                    if repair_tail:
                        journal.seek(line_start)
                        journal.truncate()
                    break  # crash mid-write leaves a bad tail; the prefix is still valid
                step = len(records)
                if raw.get("step") != step:
                    raise SnapshotError(
                        f"journal step mismatch: expected {step}, got {raw.get('step')!r}"
                    )
                records.append(
                    StepRecord(
                        step=step,
                        event=TraceEvent(str(raw["kind"]), dict(raw.get("payload") or {})),
                        snapshot=raw.get("snapshot"),
                    )
                )
        return records

    @staticmethod
    def prefix(records: list[StepRecord], upto: int) -> list[StepRecord]:
        """Events before step ``upto`` — the branch point for a future replay."""
        return [r for r in records if r.step < upto]
