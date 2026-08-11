"""Fork a supervised Codex session at a journal step — the replay half of P0.

Mechanism (approximate replay, the plan's fallback path):
1. Codex persists every session as a rollout JSONL under ~/.codex/sessions.
2. The Spotter journal records tool_use_id per proposal, which matches the
   rollout's call_id — that is the branch-point correlation key.
3. Fork = copy the rollout truncated strictly before the branch call, rewrite
   its session id to a fresh one, restore the nearest repo snapshot into a
   detached worktree, and hand back a ready `codex exec resume` invocation.
4. Run the same fork twice — once with injected guidance, once without — and
   the pair is a same-prefix counterfactual (plan Q3).

Honest limits, stated rather than papered over:
- Whether `codex exec resume` accepts a truncated rollout is UNVERIFIED until
  the first real run; that experiment IS the P0 exit criterion.
- Sessions journaled before snapshots/tool_use_id existed cannot be forked;
  the errors below say exactly which ingredient is missing.
- This does not execute anything itself: launching costs money and runs an
  agent, so the human stays on the trigger.
"""

import json
import shlex
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from spotter.hook import journal_path
from spotter.snapshot import StepJournal, StepRecord, restore_snapshot


class ReplayError(RuntimeError):
    """Raised when a fork cannot be assembled from the recorded ingredients."""


@dataclass(frozen=True)
class ForkPlan:
    session_id: str  # fresh id of the forked session
    branch_step: int
    worktree: str
    rollout: str
    command: str  # suggested invocation; the human runs it


def find_rollout(session_id: str, codex_home: Path | None = None) -> Path:
    home = codex_home or Path.home() / ".codex"
    matches = sorted(
        path
        for path in (home / "sessions").rglob("rollout-*.jsonl")
        if path.stem.endswith(session_id)
    )
    if not matches:
        raise ReplayError(f"no rollout found for session {session_id} under {home}/sessions")
    return matches[-1]


def fork_rollout(rollout: Path, call_id: str, new_id: str) -> Path:
    """Write a truncated, re-identified copy of a rollout next to the original.

    The copy ends strictly before the branch call so the resumed agent decides
    that step fresh. The original file is never touched.
    """
    lines = rollout.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ReplayError(f"rollout is empty: {rollout}")
    meta = _rollout_record(lines[0], 1)
    payload = meta.get("payload")
    if not isinstance(payload, dict):
        raise ReplayError("rollout has no session_meta payload on line 1")
    old_id = str(payload.get("session_id") or "")
    if not old_id:
        raise ReplayError("rollout has no session_meta session_id on line 1")
    cut = None
    for index, line in enumerate(lines[1:], 1):
        if _call_id(_rollout_record(line, index + 1)) == call_id:
            cut = index
            break
    if cut is None:
        raise ReplayError(f"call_id {call_id} not found in rollout {rollout.name}")
    if cut == 0:
        raise ReplayError("branch point is the first record; nothing to resume from")
    payload["session_id"] = new_id
    if payload.get("id") == old_id:
        payload["id"] = new_id
    forked = [json.dumps(meta), *lines[1:cut]]
    dest = rollout.with_name(rollout.name.replace(old_id, new_id))
    if dest.exists():
        raise ReplayError(f"forked rollout already exists: {dest}")
    dest.write_text("\n".join(forked) + "\n", encoding="utf-8")
    return dest


def fork(
    session_id: str,
    step: int,
    *,
    repo: Path | None = None,
    codex_home: Path | None = None,
    dest: Path | None = None,
    guidance: str | None = None,
) -> ForkPlan:
    journal_file = journal_path({"session_id": session_id})
    records = StepJournal.load(journal_file)
    if not 0 <= step < len(records):
        raise ReplayError(f"step {step} out of range (journal has {len(records)} steps)")
    target = records[step]
    if target.event.kind != "tool_proposal":
        raise ReplayError(f"step {step} is {target.event.kind}; fork at a tool_proposal")
    call_id = target.event.payload.get("tool_use_id")
    if not isinstance(call_id, str) or not call_id:
        raise ReplayError(f"step {step} has no tool_use_id (journaled before it was recorded)")

    snapshot = _nearest_snapshot(records, step)
    if snapshot is None:
        raise ReplayError(
            f"no snapshot at or before step {step} "
            "(snapshots are taken at apply_patch boundaries; none happened yet)"
        )
    repo_path = repo or _recorded_repo(records, step)
    if repo_path is None:
        raise ReplayError("journal has no cwd for this step; pass repo explicitly")

    new_id = str(uuid.uuid4())
    worktree = dest or journal_file.parent.parent / "forks" / new_id
    forked_rollout = fork_rollout(find_rollout(session_id, codex_home), call_id, new_id)
    try:
        restore_snapshot(repo_path, snapshot, worktree)
    except Exception:
        forked_rollout.unlink(missing_ok=True)
        raise

    argv = ["codex", "exec", "-C", str(worktree), "resume", "--json", new_id]
    if guidance:
        argv.append(guidance)
    command = shlex.join(argv)
    return ForkPlan(
        session_id=new_id,
        branch_step=step,
        worktree=str(worktree),
        rollout=str(forked_rollout),
        command=command,
    )


def plan_to_json(plan: ForkPlan) -> str:
    return json.dumps(asdict(plan), indent=2)


def _rollout_record(line: str, number: int) -> dict[str, object]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        raise ReplayError(f"invalid rollout JSON on line {number}: {error.msg}") from error
    if not isinstance(record, dict):
        raise ReplayError(f"rollout line {number} is not a JSON object")
    return record


def _call_id(record: dict[str, object]) -> object:
    payload = record.get("payload")
    return payload.get("call_id") if isinstance(payload, dict) else None


def _nearest_snapshot(records: list[StepRecord], step: int) -> str | None:
    for record in reversed(records[: step + 1]):
        if record.snapshot:
            return record.snapshot
    return None


def _recorded_repo(records: list[StepRecord], step: int) -> Path | None:
    for record in reversed(records[: step + 1]):
        cwd = record.event.payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            return Path(cwd)
    return None
