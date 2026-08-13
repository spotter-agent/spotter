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

from spotter.identity import (
    AttachmentId,
    IdentityProvenance,
    RuntimeIdentity,
    ThreadId,
    TurnId,
)
from spotter.paths import secure_dir, spotter_home
from spotter.redact import redact
from spotter.trace import TraceEvent, TraceProvenance


class SnapshotError(RuntimeError):
    """Raised when git snapshot/restore plumbing fails."""


@contextmanager
def global_lock(spotter_home_override: Path | None = None) -> Iterator[None]:
    """Serialize snapshot-ref creation+journaling against prune.

    The ref exists before the journal references it; without this lock a
    concurrent prune --apply sees an unreferenced ref in that window and
    deletes a snapshot the journal is about to claim (PR #12 review, P0).
    """
    home = spotter_home_override or spotter_home()
    secure_dir(home)
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
                # Re-pin unconditionally. A pruned snapshot stays resolvable
                # until gc runs, so reusing it without restoring the ref hands
                # the journal a sha that gc will later destroy — the exact
                # guarantee pinning exists to make (PR #19 review, P0).
                _git(repo, "update-ref", f"refs/spotter/steps/{previous}", previous)
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


# Bumped when a field changes *meaning*. Additive fields do not need it, but
# a reader that cannot tell which rules produced a record cannot refuse the
# ones it would misread — and journals are the evidence base for every rate
# this project publishes, read by tools that delete and spend (issue #47).
SCHEMA_VERSION = 1
LEGACY_VERSION = 0  # records written before versioning existed


@dataclass(frozen=True)
class StepRecord:
    step: int
    event: TraceEvent
    snapshot: str | None
    # Wall clock, not monotonic: it has to be comparable across processes and
    # against the Codex rollout, which is the only reconciliation we have.
    # None means the record predates timestamps — reported as unknown, never
    # defaulted to anything (issue #55).
    at: float | None = None
    version: int = LEGACY_VERSION


def _as_version(value: object, offset: int) -> int:
    """Version of one record, refusing anything this reader cannot interpret.

    A newer writer may have changed what a field means, and guessing is how
    old evidence gets silently misread. Absence means the record predates
    versioning, which is readable by definition.
    """
    if value is None:
        return LEGACY_VERSION
    if not isinstance(value, int) or isinstance(value, bool):
        raise SnapshotError(f"journal record at byte {offset} has a non-integer version")
    if value > SCHEMA_VERSION:
        raise SnapshotError(
            f"journal record at byte {offset} was written by schema v{value}; "
            f"this build understands up to v{SCHEMA_VERSION}"
        )
    return value


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
                # Redact before the record exists on disk: the journal is the
                # durable artifact, so filtering afterwards would be too late.
                redacted, fired = redact(dict(event.payload))
                payload = dict(redacted) if isinstance(redacted, dict) else dict(event.payload)
                if fired:
                    payload["redacted"] = sorted(set(fired))
                if event.kind == "tool_proposal":
                    state["proposals"] = int(state["proposals"]) + 1
                    payload["proposal_number"] = state["proposals"]
                stored_event = TraceEvent(
                    event.kind,
                    payload,
                    event_id=event.event_id,
                    occurred_at=event.occurred_at,
                    identity=event.identity,
                    operation_id=event.operation_id,
                    item_id=event.item_id,
                    provenance=event.provenance,
                )
                record = StepRecord(step, stored_event, snapshot, time.time(), SCHEMA_VERSION)
                line = json.dumps(
                    {
                        "v": record.version,
                        "step": record.step,
                        "at": record.at,
                        "kind": stored_event.kind,
                        "payload": stored_event.payload,
                        "trace": _trace_metadata(stored_event),
                        "snapshot": snapshot,
                    },
                    ensure_ascii=False,
                )
                if not self.path.exists():
                    self.path.touch(mode=0o600)
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
                version = _as_version(raw.get("v"), line_start)
                at = raw.get("at")
                records.append(
                    StepRecord(
                        step=step,
                        event=_trace_event(raw),
                        snapshot=raw.get("snapshot"),
                        at=float(at) if isinstance(at, int | float) else None,
                        version=version,
                    )
                )
        return records

    @staticmethod
    def prefix(records: list[StepRecord], upto: int) -> list[StepRecord]:
        """Events before step ``upto`` — the branch point for a future replay."""
        return [r for r in records if r.step < upto]


def _trace_metadata(event: TraceEvent) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for name in ("event_id", "occurred_at", "operation_id", "item_id"):
        value = getattr(event, name)
        if value is not None:
            metadata[name] = value
    if event.provenance is not None:
        metadata["provenance"] = {
            "source": event.provenance.source,
            "method": event.provenance.method,
        }
    if event.identity is not None:
        provenance = event.identity.provenance
        metadata["identity"] = {
            "thread_id": event.identity.thread_id.value if event.identity.thread_id else None,
            "turn_id": event.identity.turn_id.value if event.identity.turn_id else None,
            "attachment_id": (
                event.identity.attachment_id.value if event.identity.attachment_id else None
            ),
            "agent": provenance.agent,
            "agent_thread_id": provenance.agent_thread_id,
            "agent_turn_id": provenance.agent_turn_id,
            "agent_attachment_id": provenance.agent_attachment_id,
            "legacy_session_id": provenance.legacy_session_id,
        }
    return metadata


def _trace_event(raw: dict[str, Any]) -> TraceEvent:
    metadata = raw.get("trace")
    if not isinstance(metadata, dict):
        metadata = {}
    identity_raw = metadata.get("identity")
    identity = None
    if isinstance(identity_raw, dict) and isinstance(identity_raw.get("agent"), str):
        identity = RuntimeIdentity(
            thread_id=(
                ThreadId(identity_raw["thread_id"])
                if isinstance(identity_raw.get("thread_id"), str)
                else None
            ),
            turn_id=(
                TurnId(identity_raw["turn_id"])
                if isinstance(identity_raw.get("turn_id"), str)
                else None
            ),
            attachment_id=(
                AttachmentId(identity_raw["attachment_id"])
                if isinstance(identity_raw.get("attachment_id"), str)
                else None
            ),
            provenance=IdentityProvenance(
                agent=identity_raw["agent"],
                agent_thread_id=_optional_string(identity_raw.get("agent_thread_id")),
                agent_turn_id=_optional_string(identity_raw.get("agent_turn_id")),
                agent_attachment_id=_optional_string(identity_raw.get("agent_attachment_id")),
                legacy_session_id=_optional_string(identity_raw.get("legacy_session_id")),
            ),
        )
    provenance_raw = metadata.get("provenance")
    provenance = None
    if isinstance(provenance_raw, dict) and isinstance(provenance_raw.get("source"), str):
        provenance = TraceProvenance(
            provenance_raw["source"], _optional_string(provenance_raw.get("method"))
        )
    occurred_at = metadata.get("occurred_at")
    return TraceEvent(
        str(raw["kind"]),
        dict(raw.get("payload") or {}),
        event_id=_optional_string(metadata.get("event_id")),
        occurred_at=float(occurred_at) if isinstance(occurred_at, int | float) else None,
        identity=identity,
        operation_id=_optional_string(metadata.get("operation_id")),
        item_id=_optional_string(metadata.get("item_id")),
        provenance=provenance,
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def snapshot_references(
    sessions_dir: Path,
    repo: Path | None = None,
    exclude: list[Path] | None = None,
) -> dict[str, list[tuple[str, int]]]:
    """Map each referenced snapshot to the (session, step) pairs holding it.

    Retention needs more than a set: deleting a snapshot destroys the ability
    to fork specific steps, and the operator is owed their names.
    """
    target = repo.resolve() if repo else None
    skip = {p.resolve() for p in (exclude or [])}
    references: dict[str, list[tuple[str, int]]] = {}
    for journal in sorted(sessions_dir.glob("*.jsonl")):
        if journal.resolve() in skip:
            continue  # about to be deleted: its claims must not preserve refs
        for record in StepJournal.load(journal, strict=True):
            if not record.snapshot:
                continue
            cwd = record.event.payload.get("cwd")
            unknown_provenance = target is None or not isinstance(cwd, str) or not cwd
            if unknown_provenance or Path(str(cwd)).resolve() == target:
                references.setdefault(record.snapshot, []).append((journal.stem, record.step))
    return references


def referenced_snapshots(sessions_dir: Path, repo: Path | None = None) -> set[str]:
    """Every snapshot sha the journals still point at, filtered to ``repo``.

    Strict read: an unreadable journal or torn tail aborts the scan — pruning
    with unknown references could delete a snapshot a future fork needs.
    A record without a cwd is kept regardless of repo (conservative: unknown
    provenance must never enable deletion).
    """
    return set(snapshot_references(sessions_dir, repo))


@dataclass(frozen=True)
class PrunedRef:
    sha: str
    reason: str  # "unreferenced" | "expired"


def stale_journals(sessions_dir: Path, max_age_days: int, now: float | None = None) -> list[Path]:
    """Journals older than the window, by modification time.

    Deleting a journal silently orphans the snapshots it references, so this
    only *reports*; the caller must handle both together or not at all.
    """
    if not sessions_dir.exists():
        return []
    stamp = now if now is not None else time.time()
    cutoff = stamp - max_age_days * 86400
    return sorted(p for p in sessions_dir.glob("*.jsonl") if p.stat().st_mtime < cutoff)


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
    - ``max_age_days`` opts into expiring referenced snapshots too. The age is
      the snapshot's *creation* time, and dedup reuses an unchanged snapshot
      without refreshing it, so a step recorded today can reference a state
      created weeks ago and be expired with it. That is a real trade, not a
      cleanup — bounded disk in exchange for losing fork-ability of old
      *states*, not old steps — so it is never the default and the caller is
      told exactly which refs went that way.
      ponytail: dating reuse would need per-record journal timestamps; add
      them if expiry ever becomes a routine operation rather than a valve.

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
