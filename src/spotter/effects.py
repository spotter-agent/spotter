"""Bounded reversibility classification and external-effect reconstruction.

Classification runs in the synchronous hook, so it deliberately uses only
tool names and a small, auditable command vocabulary.  Unknown MCP tools are
Class C: understating an external write is worse than an extra shadow flag.
"""

import shlex
from dataclasses import dataclass
from typing import Any, Literal

from spotter.trace import TraceEvent

ReversibilityClass = Literal["A", "B", "C"]


@dataclass(frozen=True)
class Classification:
    reversibility_class: ReversibilityClass
    kind: str
    resource: str
    reversible: bool


_READ_COMMANDS = frozenset(
    {
        "cat",
        "find",
        "git status",
        "git diff",
        "git log",
        "git show",
        "ls",
        "pwd",
        "rg",
        "sed",
        "pytest",
        "ruff",
        "mypy",
        "pyright",
    }
)
_LOCAL_WRITES = frozenset(
    {"cp", "git add", "git checkout", "git commit", "git restore", "mkdir", "mv", "rm", "touch"}
)
_REMOTE_COMMANDS = frozenset({"git push", "gh", "kubectl", "terraform", "psql", "mysql"})
_READ_VERBS = ("get", "list", "read", "search", "find", "fetch", "open", "view", "describe")


def classify(tool: object, tool_input: object) -> Classification:
    """Classify an action without I/O, model calls, or unbounded inspection."""
    name = str(tool or "")
    values = tool_input if isinstance(tool_input, dict) else {}
    if name == "apply_patch":
        return Classification("B", "workspace_write", _first_path(values), True)
    lowered = name.lower()
    if lowered.startswith("mcp__") or lowered.startswith("mcp_"):
        operation = lowered.rsplit("__", 1)[-1]
        resource = _external_resource(values, name)
        if operation.startswith(_READ_VERBS):
            return Classification("A", "external_read", resource, True)
        return Classification("C", "external_tool_write", resource, False)

    command = values.get("command")
    if not isinstance(command, str):
        # Built-in read tools are observational; an unknown tool is treated as
        # external because adapters cannot prove where it writes.
        if lowered in {"read", "glob", "grep", "websearch", "webfetch"}:
            return Classification("A", "observation", _first_path(values), True)
        return Classification("C", "unknown_tool_effect", _external_resource(values, name), False)
    words = _words(command)
    head = " ".join(words[:2]) if words and words[0] == "git" else (words[0] if words else "")
    if head == "git push":
        return Classification("C", "git_remote_write", _git_push_resource(words), False)
    if head in _REMOTE_COMMANDS or any(head.startswith(item + " ") for item in _REMOTE_COMMANDS):
        return Classification("C", "external_command_write", " ".join(words[:3]), False)
    if head in _READ_COMMANDS:
        return Classification("A", "observation", _first_path(values), True)
    if head in _LOCAL_WRITES:
        return Classification("B", "local_write", _first_path(values), True)
    # Shell commands can contain wrappers, scripts, and network clients. Keep
    # the fallback conservative instead of claiming a write is recoverable.
    return Classification("C", "unclassified_command_effect", head or name, False)


def effect_event(result: TraceEvent) -> TraceEvent | None:
    """Turn a completed Class C call into a durable, compact ledger entry."""
    if result.kind != "tool_result" or result.payload.get("reversibility_class") != "C":
        return None
    response = result.payload.get("tool_response")
    if isinstance(response, dict):
        code = response.get("exit_code")
        outcome = (
            "succeeded" if code == 0 else (f"exit {code}" if isinstance(code, int) else "reported")
        )
    else:
        outcome = "reported" if response is not None else "unknown"
    return TraceEvent(
        "external_effect",
        {
            "kind": result.payload.get("effect_kind"),
            "resource": result.payload.get("resource"),
            "result": outcome,
            "reversible": False,
            "checkpoint": result.payload.get("checkpoint"),
            "turn_id": result.payload.get("turn_id"),
            "tool_use_id": result.payload.get("tool_use_id"),
        },
    )


def external_effects(records: list[Any], through_step: int | None = None) -> list[dict[str, Any]]:
    """Enumerate effects a local restore cannot claim to have undone."""
    return [
        dict(record.event.payload)
        for record in records
        if record.event.kind == "external_effect"
        and (through_step is None or record.step <= through_step)
    ]


def _words(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=True)[:32]
    except ValueError:
        return command.split()[:32]


def _first_path(values: dict[str, Any]) -> str:
    for key in ("path", "file_path", "url", "uri"):
        if isinstance(values.get(key), str):
            return str(values[key])
    return "workspace"


def _external_resource(values: dict[str, Any], fallback: str) -> str:
    for key in ("resource", "url", "uri", "repository", "project", "name", "path"):
        if isinstance(values.get(key), str):
            return str(values[key])[:300]
    return fallback


def _git_push_resource(words: list[str]) -> str:
    args = [word for word in words[2:] if not word.startswith("-")]
    return args[0] if args else "default git remote"
