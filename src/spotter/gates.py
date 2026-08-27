"""Deterministic pre-action gates (plan P3).

Rules only — an LLM verdict must never produce a BLOCK. False positives here
are the most expensive failure, so every pattern errs toward allowing:
ambiguous input passes, but passes are annotated so blindness is measurable.

Destructive-command judgment runs on the *parsed token stream*, not the raw
string: quoted text collapses into single tokens, so a command that merely
mentions ``git reset --hard`` (a PR review body, test code in a heredoc, a
patch to this very file) no longer trips the gate. First shadow-mode field
data showed 6/6 false positives from exactly that raw-string matching.
"""

import os
import posixpath
import re
import shlex
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatch

from spotter.trace import TraceEvent

GATE_RULE_VERSION = 1


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    rule: str | None = None
    reason: str | None = None


ALLOW = GateDecision(allowed=True)

# Fallback ONLY for commands shlex cannot parse (unbalanced quotes): a raw
# regex is better than nothing there, and quoted-text FPs cannot occur in a
# string whose quoting is broken anyway.
_DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
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


def _scratch_roots() -> frozenset[str]:
    """Ephemeral directories an agent legitimately writes outside the workspace.

    Shadow-mode field data over 213 sessions: ``workspace_escape`` produced 218
    of 230 gate flags, and 131 of those 218 were scratch writes such as
    ``/tmp/pr-body.md``. Confining an agent to its workspace was never meant to
    forbid a temp file, and enforcing that rule as written would block ordinary
    work. The other 87 were writes into sibling repository checkouts, which this
    allowance deliberately still blocks.

    Both spellings are kept because macOS resolves ``/tmp`` and ``$TMPDIR``
    through ``/private`` and agents produce either form.
    """
    declared = {"/tmp", "/var/tmp", tempfile.gettempdir()}
    resolved = {os.path.realpath(root) for root in declared}
    return frozenset(posixpath.normpath(root.replace("\\", "/")) for root in declared | resolved)


_SCRATCH_ROOTS = _scratch_roots()

_SHELLS = frozenset({"sh", "bash", "zsh", "dash"})
_CHAIN_SEPARATORS = frozenset({";", "&", "&&", "||", "(", ")"})
_RM_TARGETS = frozenset({"~", "$HOME", "${HOME}", ".."})
# git global options that consume the following token
_GIT_VALUE_OPTS = frozenset({"-c", "-C", "--exec-path", "--git-dir", "--work-tree", "--namespace"})


def _tokenize(command: str) -> list[str]:
    """Tokenize with quote awareness; operators split even without spaces."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _split(tokens: list[str], separators: frozenset[str]) -> list[list[str]]:
    groups: list[list[str]] = [[]]
    for token in tokens:
        if token in separators:
            groups.append([])
        else:
            groups[-1].append(token)
    return [group for group in groups if group]


def _base(token: str) -> str:
    return posixpath.basename(token)


@dataclass(frozen=True)
class Gate:
    forbidden_paths: tuple[str, ...] = ()
    block_dependency_changes: bool = False
    root: str | None = None  # absolute workspace root; enables absolute-path judgment
    # Directories whose immediate children are each a workspace. Field data:
    # every remaining `workspace_escape` on a real machine was one repository in
    # a multi-repo checkout editing another, under prompts that asked for exactly
    # that — an infra-wide OOM investigation touching twelve service repos, a
    # cross-region deploy, cross-service feature work. The job was the checkout,
    # not the directory the shell happened to start in.
    workspace_roots: tuple[str, ...] = ()

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
        try:
            tokens = _tokenize(command)
        except ValueError:
            for rule, pattern in _DESTRUCTIVE_PATTERNS:
                if pattern.search(command):
                    return GateDecision(False, rule, f"destructive command matched {rule}")
            # Unparseable and no raw-pattern hit — cannot reason about it.
            # Fail open, but say so: this is a measured blind spot.
            return GateDecision(True, "unparseable_command", "fail-open: could not parse")
        for chain in _split(tokens, _CHAIN_SEPARATORS):
            stages = _split(chain, frozenset({"|", "|&"}))
            decision = self._check_pipeline(stages)
            if not decision.allowed:
                return decision
        return ALLOW

    def _check_pipeline(self, stages: list[list[str]]) -> GateDecision:
        for index, stage in enumerate(stages):
            if _base(stage[0]) in {"curl", "wget"} and any(
                _base(later[0]) in _SHELLS for later in stages[index + 1 :]
            ):
                return GateDecision(False, "pipe_to_shell", "downloaded content piped into a shell")
            decision = self._check_stage(stage)
            if not decision.allowed:
                return decision
        return ALLOW

    def _check_stage(self, stage: list[str]) -> GateDecision:
        for index, token in enumerate(stage):
            base = _base(token)
            rest = stage[index + 1 :]
            if base == "rm" and _rm_hits_catastrophic_target(rest):
                return GateDecision(False, "rm_root", "rm targeting /, ~ or ..")
            if base == "git":
                verdict = _git_verdict(rest)
                if verdict is not None:
                    return verdict
            if base in _SHELLS:
                # bash -c '<string>' executes the string: judge it as a command.
                for position, arg in enumerate(rest):
                    if arg == "-c" and position + 1 < len(rest):
                        inner = self.check_command(rest[position + 1])
                        if not inner.allowed:
                            return inner
                        break
            if base == "mkfs" or base.startswith("mkfs."):
                return GateDecision(False, "mkfs", "filesystem creation")
            if token.startswith("of=/dev/"):
                return GateDecision(False, "dd_device", "writing to a raw device")
        return ALLOW

    def _sibling_workspace(self, path: str) -> str | None:
        """The declared workspace this absolute path belongs to, if any."""
        for raw_root in self.workspace_roots:
            parent = posixpath.normpath(
                posixpath.expanduser(raw_root.replace("\\", "/")) if raw_root else raw_root
            )
            if not parent or not posixpath.isabs(parent) or not path.startswith(parent + "/"):
                continue
            remainder = path[len(parent) + 1 :]
            project = remainder.split("/", 1)[0]
            if project and "/" in remainder:  # a file inside a project, not the project itself
                return posixpath.join(parent, project)
        return None

    def check_paths(self, paths: Iterable[str]) -> GateDecision:
        uncertain: GateDecision | None = None
        for raw in paths:
            path = posixpath.normpath(raw.replace("\\", "/"))
            root = posixpath.normpath(self.root.replace("\\", "/")) if self.root else None
            if path.startswith("..") and root is not None:
                # `../../../../tmp/pr-body.md` is the same scratch write as
                # `/tmp/pr-body.md`, and agents produce both. Judging the relative
                # spelling without resolving it first sent every one of them to
                # the escape branch: 30 of the flags left on this machine after
                # the absolute case was fixed were this, and nothing else.
                path = posixpath.normpath(posixpath.join(root, path))
            if posixpath.isabs(path):
                if root is None:
                    # Can't judge an absolute path without knowing the workspace.
                    # Blocking here would be FP-prone; pass, annotated.
                    uncertain = GateDecision(
                        True, "unknown_workspace", f"fail-open: cannot judge absolute path: {raw}"
                    )
                    continue
                sibling = self._sibling_workspace(path)
                if path == root or path.startswith(root + "/"):
                    path = posixpath.relpath(path, root)
                elif sibling is not None:
                    # Relative to that project, not to the shared parent, so a
                    # `secrets/*` pattern still means the same thing there.
                    path = posixpath.relpath(path, sibling)
                elif _is_scratch(path):
                    # Ephemeral scratch, not an escape. The path stays absolute so
                    # forbidden_paths still applies to it.
                    pass
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


def _is_scratch(path: str) -> bool:
    """True for a normalized absolute path inside an ephemeral scratch root."""
    return any(path == root or path.startswith(root + "/") for root in _SCRATCH_ROOTS)


def _rm_hits_catastrophic_target(rest: list[str]) -> bool:
    for token in rest:
        if token.startswith("-"):
            continue
        if (token and not token.strip("/")) or token.rstrip("/") in _RM_TARGETS:
            return True
    return False


def _git_verdict(args: list[str]) -> GateDecision | None:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in _GIT_VALUE_OPTS else 1
    if index >= len(args):
        return None
    subcommand, rest = args[index], args[index + 1 :]
    if subcommand == "reset" and "--hard" in rest:
        return GateDecision(False, "git_reset_hard", "destructive command matched git_reset_hard")
    if subcommand == "clean" and any(
        arg == "--force" or (arg.startswith("-") and not arg.startswith("--") and "f" in arg)
        for arg in rest
    ):
        return GateDecision(False, "git_clean_force", "destructive command matched git_clean_force")
    if subcommand == "push" and any(arg in {"--force", "-f"} for arg in rest):
        # --force-with-lease is deliberately allowed: it is the safe force.
        return GateDecision(False, "git_push_force", "destructive command matched git_push_force")
    return None
