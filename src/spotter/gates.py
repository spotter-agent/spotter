"""Deterministic pre-action gates (plan P3).

Rules only — an LLM verdict must never produce a BLOCK. False positives here
are the most expensive failure, so every pattern errs toward allowing:
ambiguous input passes, but passes are annotated so blindness is measurable.
"""

import posixpath
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatch

from spotter.trace import TraceEvent


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    rule: str | None = None
    reason: str | None = None


ALLOW = GateDecision(allowed=True)

# Only clearly catastrophic commands. "rm -rf build/" is legitimate agent work;
# blocking it would burn the trust budget the plan warns about (양치기 소년).
_DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Keyed on the target, not the flags: `rm ..` without -rf just fails, and
    # long options (--recursive) must not slip through a flag-shape regex.
    ("rm_root", re.compile(r"\brm\s+(?:-{1,2}\S+\s+)*(/|~|\$HOME|\.\.)/?(\s|$)")),
    ("git_reset_hard", re.compile(r"\bgit\s+reset\s+--hard\b")),
    ("git_clean_force", re.compile(r"\bgit\s+clean\b[^&|;]*-[a-zA-Z]*f")),
    ("git_push_force", re.compile(r"\bgit\s+push\b[^&|;]*(--force\b|\s-f\b)")),
    ("pipe_to_shell", re.compile(r"\b(curl|wget)\b[^&|;]*\|\s*(ba|z|da)?sh\b")),
    ("dd_device", re.compile(r"\bdd\b[^&|;]*\bof=/dev/")),
    ("mkfs", re.compile(r"\bmkfs\b")),
)

_DEPENDENCY_MANIFESTS = frozenset(
    {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "pyproject.toml",
        "requirements.txt",
        "uv.lock",
        "poetry.lock",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "Gemfile",
        "Gemfile.lock",
    }
)


@dataclass(frozen=True)
class Gate:
    forbidden_paths: tuple[str, ...] = ()
    block_dependency_changes: bool = False
    root: str | None = None  # absolute workspace root; enables absolute-path judgment

    def check(self, event: TraceEvent) -> GateDecision:
        """Gate a pending action event. Non-action events always pass."""
        if event.kind != "tool_proposal":
            return ALLOW
        command = event.payload.get("command")
        if isinstance(command, str):
            decision = self.check_command(command)
            if not decision.allowed:
                return decision
        files = event.payload.get("files")
        if isinstance(files, list):
            return self.check_paths(str(f) for f in files)
        return ALLOW

    def check_command(self, command: str) -> GateDecision:
        for rule, pattern in _DESTRUCTIVE_PATTERNS:
            if pattern.search(command):
                return GateDecision(False, rule, f"destructive command matched {rule}")
        try:
            shlex.split(command)
        except ValueError:
            # Unparseable — cannot reason about it. Fail open, but say so:
            # this pass is a measured blind spot, not a clean bill of health.
            return GateDecision(True, "unparseable_command", "fail-open: could not parse")
        return ALLOW

    def check_paths(self, paths: Iterable[str]) -> GateDecision:
        for raw in paths:
            path = posixpath.normpath(raw.replace("\\", "/"))
            if posixpath.isabs(path):
                root = posixpath.normpath(self.root.replace("\\", "/")) if self.root else None
                if root is None:
                    # Can't judge an absolute path without knowing the workspace.
                    # Blocking here would be FP-prone; pass, annotated.
                    continue
                if path == root or path.startswith(root + "/"):
                    path = posixpath.relpath(path, root)
                else:
                    return GateDecision(False, "workspace_escape", f"path leaves workspace: {raw}")
            if path.startswith(".."):
                return GateDecision(False, "workspace_escape", f"path leaves workspace: {raw}")
            if self.block_dependency_changes and posixpath.basename(path) in _DEPENDENCY_MANIFESTS:
                return GateDecision(False, "dependency_change", f"manifest edit: {path}")
            for pattern in self.forbidden_paths:
                if fnmatch(path, pattern):
                    return GateDecision(False, "forbidden_path", f"{path} matches {pattern}")
        return ALLOW
