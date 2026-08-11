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
        uncertain: GateDecision | None = None
        command = event.payload.get("command")
        if isinstance(command, str):
            decision = self.check_command(command)
            if not decision.allowed:
                return decision
            if decision.rule:
                uncertain = decision
        files = event.payload.get("files")
        if isinstance(files, list):
            decision = self.check_paths(str(f) for f in files)
            if not decision.allowed or decision.rule:
                return decision
        return uncertain or ALLOW

    def check_command(self, command: str) -> GateDecision:
        for rule, pattern in _DESTRUCTIVE_PATTERNS:
            if pattern.search(command):
                return GateDecision(False, rule, f"destructive command matched {rule}")
        try:
            tokens = shlex.split(command)
        except ValueError:
            # Unparseable — cannot reason about it. Fail open, but say so:
            # this pass is a measured blind spot, not a clean bill of health.
            return GateDecision(True, "unparseable_command", "fail-open: could not parse")
        if "rm" in tokens and any(
            (token and not token.strip("/")) or token.rstrip("/") in {"~", "$HOME", "${HOME}", ".."}
            for token in tokens[tokens.index("rm") + 1 :]
            if not token.startswith("-")
        ):
            return GateDecision(False, "rm_root", "destructive command matched rm_root")
        if "git" in tokens:
            git_args = tokens[tokens.index("git") + 1 :]
            if "reset" in git_args and "--hard" in git_args[git_args.index("reset") + 1 :]:
                return GateDecision(
                    False, "git_reset_hard", "destructive command matched git_reset_hard"
                )
        return ALLOW

    def check_paths(self, paths: Iterable[str]) -> GateDecision:
        uncertain: GateDecision | None = None
        for raw in paths:
            path = posixpath.normpath(raw.replace("\\", "/"))
            if posixpath.isabs(path):
                root = posixpath.normpath(self.root.replace("\\", "/")) if self.root else None
                if root is None:
                    # Can't judge an absolute path without knowing the workspace.
                    # Blocking here would be FP-prone; pass, annotated.
                    uncertain = GateDecision(
                        True, "unknown_workspace", f"fail-open: cannot judge absolute path: {raw}"
                    )
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
        return uncertain or ALLOW
