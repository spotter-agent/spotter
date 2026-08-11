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
from spotter.snapshot import StepJournal
from spotter.trace import TraceEvent


class JournalAdapter:
    def __init__(self, journal: StepJournal) -> None:
        self.journal = journal

    def record(self, event: TraceEvent) -> None:
        self.journal.record(event)


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
        return TraceEvent(
            "tool_proposal",
            {
                "tool": payload.get("tool_name"),
                "command": tool_input.get("command"),
                "files": files,
            },
        )
    if name == "PostToolUse":
        return TraceEvent("tool_result", {"tool": payload.get("tool_name")})
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
    runtime = SpotterRuntime(config, JournalAdapter(StepJournal(journal_path(payload))), gate)
    decision = runtime.observe(event_from_hook(payload))
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
