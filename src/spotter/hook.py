"""Codex hook bridge: stdin JSON in, PreToolUse decision JSON out.

Prime directive: a buggy supervisor must never break the supervised session.
Every failure path here fails open (allow) with a note on stderr — Spotter
losing an observation is recoverable, Codex dying mid-turn is not.
"""

import json
import os
import re
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

    def record(self, event: TraceEvent) -> None:
        self.journal.record(event, snapshot=self.next_snapshot)
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


def journal_path(payload: dict[str, Any]) -> Path:
    base = Path(os.environ.get("SPOTTER_HOME", Path.home() / ".spotter")) / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    # session_id is external input headed into a filename — sanitize it.
    session = re.sub(r"[^A-Za-z0-9_-]", "_", str(payload.get("session_id") or "unknown"))
    return base / f"{session}.jsonl"


def run_hook(payload: dict[str, Any], config: SpotterConfig) -> str | None:
    """Process one hook invocation. Returns stdout JSON, or None to allow."""
    cwd = payload.get("cwd")
    gate = Gate(
        forbidden_paths=config.gates.forbidden_paths,
        block_dependency_changes=config.gates.block_dependency_changes,
        root=str(cwd) if isinstance(cwd, str) else None,
    )
    adapter = JournalAdapter(StepJournal(journal_path(payload)))
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
                adapter.next_snapshot = snapshot_worktree(Path(cwd))
            except SnapshotError:
                adapter.next_snapshot = None
            decision = runtime.observe(event)
    else:
        decision = runtime.observe(event)
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
