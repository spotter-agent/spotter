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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

from spotter.trace import TraceEvent


class SnapshotError(RuntimeError):
    """Raised when git snapshot/restore plumbing fails."""


@contextmanager
def global_lock(spotter_home: Path | None = None) -> Iterator[None]:
    """Serialize snapshot-ref creation+journaling against prune.

    The ref exists before the journal references it; without this lock a
    concurrent prune --apply sees an unreferenced ref in that window and
    deletes a snapshot the journal is about to claim (PR #12 review, P0).
    """
    home = spotter_home or Path(os.environ.get("SPOTTER_HOME", Path.home() / ".spotter"))
    home.mkdir(parents=True, exist_ok=True)
    with (home / "lock").open("w") as handle:
        flock(handle, LOCK_EX)
        try:
            yield
        finally:
            flock(handle, LOCK_UN)


def _git(
    repo: Path, *args: str, env: dict[str, str] | None = None, input: str | None = None
) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            input=input,
        )
    except OSError as error:  # missing cwd, missing git binary — same contract
        raise SnapshotError(f"git {' '.join(args)} failed to start: {error}") from error
    if result.returncode != 0:
        raise SnapshotError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def snapshot_worktree(repo: Path, previous: str | None = None) -> str:
    """Snapshot the full worktree (untracked included) without touching the
    user's index or HEAD. Returns the commit sha, pinned under refs/spotter/
    so gc cannot silently delete it out from under a later restore.

    When ``previous`` names a snapshot with an identical tree, that sha is
    returned unchanged: a tool call that touched nothing must not mint a new
    ref, or an idle session grows the ref namespace for free (issue #7).
    Dedup is best-effort — an unreadable ``previous`` just means a new commit.
    """
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
        if previous:
            unchanged = subprocess.run(
                ["git", "rev-parse", "--verify", "-q", f"{previous}^{{tree}}"],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            if unchanged.returncode == 0 and unchanged.stdout.strip() == tree:
                return previous
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
        state_path = self.path.with_suffix(self.path.suffix + ".state")
        with lock_path.open("w") as lock:
            flock(lock, LOCK_EX)
            try:
                state: dict[str, Any] | None = None
                if state_path.exists() and self.path.exists():
                    try:
                        candidate = json.loads(state_path.read_text())
                        if candidate.get("size") == self.path.stat().st_size:
                            state = candidate
                    except (json.JSONDecodeError, OSError):
                        pass
                if state is None:
                    records = self.load(self.path, repair_tail=True) if self.path.exists() else []
                    state = {
                        "steps": len(records),
                        "proposals": sum(r.event.kind == "tool_proposal" for r in records),
                        "last_snapshot": next(
                            (r.snapshot for r in reversed(records) if r.snapshot), None
                        ),
                    }
                step = int(state["steps"])
                payload = dict(event.payload)
                if event.kind == "tool_proposal":
                    state["proposals"] = int(state["proposals"]) + 1
                    payload["proposal_number"] = state["proposals"]
                stored_event = TraceEvent(event.kind, payload)
                record = StepRecord(step, stored_event, snapshot)
                line = json.dumps(
                    {
                        "step": record.step,
                        "kind": stored_event.kind,
                        "payload": stored_event.payload,
                        "snapshot": snapshot,
                    },
                    ensure_ascii=False,
                )
                with self.path.open("a", encoding="utf-8") as journal:
                    journal.write(line + "\n")
                    journal.flush()
                    os.fsync(journal.fileno())
                state["steps"] = int(state["steps"]) + 1
                if snapshot:
                    state["last_snapshot"] = snapshot
                state["size"] = self.path.stat().st_size
                temporary = state_path.with_suffix(state_path.suffix + ".tmp")
                temporary.write_text(json.dumps(state))
                os.replace(temporary, state_path)
                return record
            finally:
                flock(lock, LOCK_UN)

    def last_snapshot(self) -> str | None:
        """Most recent snapshot sha, from the sidecar when it is fresh.

        Best-effort: a missing or stale sidecar returns None, which costs a
        redundant snapshot, never a wrong one.
        """
        state_path = self.path.with_suffix(self.path.suffix + ".state")
        if not (state_path.exists() and self.path.exists()):
            return None
        try:
            state = json.loads(state_path.read_text())
            if state.get("size") != self.path.stat().st_size:
                return None
        except (json.JSONDecodeError, OSError):
            return None
        sha = state.get("last_snapshot")
        return str(sha) if sha else None

    @staticmethod
    def load(path: Path, *, repair_tail: bool = False, strict: bool = False) -> list[StepRecord]:
        """Load records. strict=True refuses any undecodable content instead of
        keeping the valid prefix — destructive readers (prune) must not treat a
        torn tail as proof of absence (PR #12 review, P0)."""
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
                    if strict:
                        raise SnapshotError(
                            f"undecodable journal content at byte {line_start} (torn tail?)"
                        ) from None
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


def referenced_snapshots(sessions_dir: Path, repo: Path | None = None) -> set[str]:
    """Every snapshot sha the journals still point at, filtered to ``repo``.

    Strict read: an unreadable journal or torn tail aborts the scan — pruning
    with unknown references could delete a snapshot a future fork needs.
    A record without a cwd is kept regardless of repo (conservative: unknown
    provenance must never enable deletion).
    """
    target = repo.resolve() if repo else None
    shas: set[str] = set()
    for journal in sorted(sessions_dir.glob("*.jsonl")):
        for record in StepJournal.load(journal, strict=True):
            if not record.snapshot:
                continue
            cwd = record.event.payload.get("cwd")
            unknown_provenance = target is None or not isinstance(cwd, str) or not cwd
            if unknown_provenance or Path(str(cwd)).resolve() == target:
                shas.add(record.snapshot)
    return shas


@dataclass(frozen=True)
class PrunedRef:
    sha: str
    reason: str  # "unreferenced" | "expired"


def prune_snapshots(
    repo: Path,
    referenced: set[str],
    *,
    apply: bool = False,
    max_age_days: int | None = None,
    now: int | None = None,
) -> list[PrunedRef]:
    """List (and with apply=True, delete) prunable refs/spotter/steps/*.

    Retention policy (issue #7):
    - unreferenced snapshots are always prunable — nothing can fork from them;
    - referenced snapshots are kept indefinitely by default, because deleting
      one destroys the ability to fork that step;
    - ``max_age_days`` opts into expiring referenced snapshots too. That is a
      real trade, not a cleanup: bounded disk in exchange for losing
      fork-ability of steps older than the window, so it is never the default
      and the caller is told which refs went that way.

    Touches nothing outside refs/spotter/steps — user refs and worktrees are
    structurally out of reach.
    """
    output = _git(
        repo,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(committerdate:unix)",
        "refs/spotter/steps",
    )
    cutoff = None
    if max_age_days is not None:
        stamp = now if now is not None else int(time.time())
        cutoff = stamp - max_age_days * 86400
    doomed: list[tuple[str, PrunedRef]] = []
    for line in output.splitlines():
        refname, _, rest = line.partition("\x00")
        sha, _, committed = rest.partition("\x00")
        if not refname.startswith("refs/spotter/steps/") or not sha:
            continue  # paranoia: never consider anything else deletable
        if sha not in referenced:
            doomed.append((refname, PrunedRef(sha, "unreferenced")))
        elif cutoff is not None and committed.isdigit() and int(committed) < cutoff:
            doomed.append((refname, PrunedRef(sha, "expired")))
    if apply and doomed:
        # One update-ref --stdin transaction: all deletions or none — a
        # mid-loop failure must not leave a half-pruned ref namespace.
        script = "".join(f"delete {refname} {pruned.sha}\n" for refname, pruned in doomed)
        _git(repo, "update-ref", "--stdin", input=script)
    return [pruned for _, pruned in doomed]
