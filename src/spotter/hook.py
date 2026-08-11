"""Codex hook bridge: stdin JSON in, PreToolUse decision JSON out.

Prime directive: a buggy supervisor must never break the supervised session.
Every failure path here fails open (allow) with a note on stderr — Spotter
losing an observation is recoverable, Codex dying mid-turn is not.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from spotter.config import SpotterConfig
from spotter.core import SpotterRuntime
from spotter.gates import Gate
from spotter.snapshot import SnapshotError, StepJournal, global_lock, snapshot_worktree
from spotter.trace import TraceEvent

_PATCH_PATH = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.MULTILINE)


class JournalAdapter:
    def __init__(self, journal: StepJournal) -> None:
        self.journal = journal
        self.next_snapshot: str | None = None
        self.last_proposal_number = 0

    def record(self, event: TraceEvent) -> None:
        record = self.journal.record(event, snapshot=self.next_snapshot)
        if event.kind == "tool_proposal":
            self.last_proposal_number = int(record.event.payload["proposal_number"])
        self.next_snapshot = None  # attaches to the first (proposal) event only


def event_from_hook(payload: dict[str, Any]) -> TraceEvent:
    name = payload.get("hook_event_name")
    if name == "PreToolUse":
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        files = [
            str(value)
            for key, value in tool_input.items()
            if key in ("path", "file_path") and isinstance(value, str)
        ]
        listed = tool_input.get("files")
        if isinstance(listed, list):
            files.extend(str(item) for item in listed)
        command = tool_input.get("command")
        patch: str | None = None
        if payload.get("tool_name") == "apply_patch" and isinstance(command, str):
            files.extend(_PATCH_PATH.findall(command))
            # A patch body is not a shell command; judging it as one produced
            # real FPs (a patch editing gates.py tripped the gate it edits).
            patch, command = command, None
        return TraceEvent(
            "tool_proposal",
            {
                "tool": payload.get("tool_name"),
                "command": command,
                "patch": patch,
                "files": files,
                # Correlation keys for P0 fork: tool_use_id matches the rollout's
                # call_id; cwd locates the repo to snapshot/restore.
                "tool_use_id": payload.get("tool_use_id"),
                "cwd": payload.get("cwd"),
            },
        )
    if name == "PostToolUse":
        return TraceEvent(
            "tool_result",
            {
                "tool": payload.get("tool_name"),
                "tool_use_id": payload.get("tool_use_id"),
                "tool_input": payload.get("tool_input"),
                "tool_response": payload.get("tool_response"),
            },
        )
    if name == "UserPromptSubmit":
        # The goal is the reviewer's anchor for spec-drift judgment; without it
        # the digest has actions but no intent.
        return TraceEvent("user_prompt", {"prompt": payload.get("prompt")})
    return TraceEvent(str(name or "unknown").lower())


def sanitize_session(session_id: object) -> str:
    """External input headed into a filename — never let it carry a path."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or "unknown"))


def spotter_home() -> Path:
    return Path(os.environ.get("SPOTTER_HOME", Path.home() / ".spotter"))


def journal_path(payload: dict[str, Any]) -> Path:
    base = spotter_home() / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{sanitize_session(payload.get('session_id'))}.jsonl"


def _maybe_spawn_shadow_review(
    config: SpotterConfig,
    payload: dict[str, Any],
    journal_file: Path,
    proposal_number: int,
) -> None:
    """Fire-and-forget shadow review every N *proposals* (Wink-style cadence).

    The cadence counts tool proposals, not journal steps — results, prompts,
    gate flags and the reviewer's own verdicts also consume step numbers, and
    a cadence keyed on those drifts (and is even perturbed by the verdicts it
    writes). Detached: the hook must never wait on a model call.
    SPOTTER_DISABLE guards recursion. Child output goes to a per-session log —
    a silently vanishing reviewer is not "accumulating samples".
    """
    every = config.reviewer.every_steps
    if not every or os.environ.get("SPOTTER_DISABLE"):
        return
    if proposal_number == 0 or proposal_number % every:
        return
    session = str(payload.get("session_id") or "")
    if not session:
        return
    args = [
        sys.executable,
        "-m",
        "spotter",
        "review",
        "--session",
        session,
        "--model",
        config.reviewer.model,
    ]
    logs = spotter_home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f"review-{sanitize_session(session)}.log").open("ab") as log:
        try:
            subprocess.Popen(
                args,
                env={**os.environ, "SPOTTER_DISABLE": "1"},
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        except OSError as error:
            StepJournal(journal_file).record(
                TraceEvent("reviewer_error", {"error": f"review process failed: {error}"[:300]})
            )


def run_hook(
    payload: dict[str, Any], config: SpotterConfig, config_path: Path | None = None
) -> str | None:
    """Process one hook invocation. Returns stdout JSON, or None to allow."""
    cwd = payload.get("cwd")
    gate = Gate(
        forbidden_paths=config.gates.forbidden_paths,
        block_dependency_changes=config.gates.block_dependency_changes,
        root=str(cwd) if isinstance(cwd, str) else None,
    )
    journal_file = journal_path(payload)
    journal = StepJournal(journal_file)
    adapter = JournalAdapter(journal)
    runtime = SpotterRuntime(config, adapter, gate)
    event = event_from_hook(payload)
    if (
        config.snapshot_on_patch
        and event.kind in ("tool_proposal", "tool_result")
        and event.payload.get("tool") == "apply_patch"
        and isinstance(cwd, str)
    ):
        # PreToolUse captures the state before this patch; PostToolUse captures
        # the state after it, keeping later rollout prefixes aligned with disk.
        # The global lock closes the ref-created-but-not-yet-journaled window
        # a concurrent prune --apply could otherwise exploit.
        with global_lock():
            try:
                # Reuse the previous snapshot when the tree is unchanged, so a
                # tool call that touched nothing does not mint a ref (#7).
                adapter.next_snapshot = snapshot_worktree(Path(cwd), journal.last_snapshot())
            except SnapshotError:
                adapter.next_snapshot = None
            decision = runtime.observe(event)
    else:
        decision = runtime.observe(event)
    if event.kind == "tool_proposal":
        _maybe_spawn_shadow_review(config, payload, journal_file, adapter.last_proposal_number)
    if decision.allowed:
        return None  # implicit allow; stay silent on the happy path
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"[spotter:{decision.rule}] {decision.reason}",
            }
        }
    )
