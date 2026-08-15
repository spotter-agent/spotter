"""Bounded reversibility classification and external-effect reconstruction.

Classification runs in the synchronous hook.  It is deliberately deterministic,
bounded, and conservative: recognized semantics may prove an action safer, while
unknown command shapes still map to Class C without being mislabeled as known writes.
"""

import shlex
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from spotter.trace import TraceEvent

ReversibilityClass = Literal["A", "B", "C"]
ParseConfidence = Literal["exact", "bounded", "unknown"]

_MAX_TOKENS = 64
_MAX_COMMAND_CHARS = 4096
_MAX_WRAPPER_DEPTH = 2
_COMPOSITION = frozenset({"&&", "||", ";", "|", "&"})
_OUTPUT_REDIRECTION = frozenset({">", ">>"})


@dataclass(frozen=True)
class Classification:
    reversibility_class: ReversibilityClass
    kind: str
    resource: str
    reversible: bool
    classifier_id: str = "fallback"
    reason_code: str = "unclassified"
    parse_confidence: ParseConfidence = "unknown"
    semantic_operation: str | None = None


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
_MCP_READ_VERBS = frozenset(
    {"describe", "fetch", "find", "get", "list", "open", "read", "search", "view"}
)
_MCP_WRITE_VERBS = frozenset(
    {
        "create",
        "update",
        "edit",
        "delete",
        "remove",
        "close",
        "merge",
        "post",
        "send",
    }
)
_GH_READ_VERBS = frozenset({"list", "status", "view"})
_GH_WRITE_VERBS = frozenset(
    {"close", "comment", "create", "delete", "edit", "merge", "reopen", "submit"}
)
_KUBECTL_READ_VERBS = frozenset(
    {
        "api-resources",
        "api-versions",
        "cluster-info",
        "describe",
        "diff",
        "explain",
        "get",
        "logs",
        "version",
    }
)
_KUBECTL_WRITE_VERBS = frozenset(
    {
        "annotate",
        "apply",
        "create",
        "delete",
        "label",
        "patch",
        "replace",
        "rollout",
        "scale",
        "set",
    }
)
_TERRAFORM_READ_VERBS = frozenset({"plan", "show", "validate", "version"})
_TERRAFORM_WRITE_VERBS = frozenset(
    {"apply", "destroy", "force-unlock", "import", "taint", "untaint"}
)
_SCRIPT_RUNNERS = frozenset(
    {"deno", "make", "node", "npm", "npx", "perl", "php", "python", "python3", "ruby"}
)


def classify(tool: object, tool_input: object) -> Classification:
    """Classify an action without I/O, model calls, or unbounded inspection."""

    name = str(tool or "")
    values = tool_input if isinstance(tool_input, dict) else {}
    if name == "apply_patch":
        return _known("B", "workspace_write", _first_path(values), True, "native", "apply_patch")

    lowered = name.lower()
    if lowered.startswith("mcp__") or lowered.startswith("mcp_"):
        operation = lowered.rsplit("__", 1)[-1]
        verb = operation.partition("_")[0]
        resource = _external_resource(values, name)
        if verb in _MCP_READ_VERBS:
            return _known("A", "external_read", resource, True, "mcp_name", operation)
        if verb in _MCP_WRITE_VERBS:
            return _known("C", "external_tool_write", resource, False, "mcp_name", operation)
        return _unknown("unknown_mcp_tool", resource, "mcp_name", operation)

    command = values.get("command")
    if not isinstance(command, str):
        if lowered in {"read", "glob", "grep", "websearch", "webfetch"}:
            return _known("A", "observation", _first_path(values), True, "native", lowered)
        return _unknown("unknown_tool_effect", _external_resource(values, name), "fallback")
    return _classify_command(command, values, depth=0, wrapped=False)


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
            "classifier": result.payload.get("effect_classifier"),
            "reason": result.payload.get("effect_reason"),
            "confidence": result.payload.get("effect_confidence"),
            "semantic_operation": result.payload.get("semantic_operation"),
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


def _classify_command(
    command: str, values: dict[str, Any], *, depth: int, wrapped: bool
) -> Classification:
    if len(command) > _MAX_COMMAND_CHARS:
        return _unknown("command_too_large", "shell", "shell_structure")
    tokens = _tokens(command)
    if tokens is None:
        return _unknown("malformed_shell", "shell", "shell_structure")
    if not tokens:
        return _unknown("empty_command", "shell", "shell_structure")
    if len(tokens) > _MAX_TOKENS:
        return _unknown("command_too_large", _executable(tokens[0]), "shell_structure")

    components, output = _components(tokens)
    if len(components) > 1 or output is not None:
        if any(not component for component in components):
            return _unknown("malformed_composition", "shell", "shell_structure")
        assessments = [
            _classify_tokens(component, values, depth=depth, wrapped=True)
            for component in components
            if component
        ]
        if not assessments:
            return _unknown("malformed_composition", "shell", "shell_structure")
        strongest = _strongest(assessments)
        if output is not None and strongest.reversibility_class == "A":
            strongest = _known(
                "B",
                "local_write",
                output,
                True,
                "shell_structure",
                "output_redirection",
                confidence="bounded",
            )
        return replace(strongest, parse_confidence=_combined_confidence(strongest))
    return _classify_tokens(tokens, values, depth=depth, wrapped=wrapped)


def _classify_tokens(
    tokens: Sequence[str], values: dict[str, Any], *, depth: int, wrapped: bool
) -> Classification:
    executable = _executable(tokens[0])
    rest = list(tokens[1:])
    if executable == "env":
        inner = _unwrap_env(rest)
        if inner is None:
            return _unknown("unsupported_env_wrapper", "env", "shell_structure")
        return _wrapped(inner, values, depth, "env")
    if executable == "sudo":
        inner = _unwrap_sudo(rest)
        if inner is None:
            return _unknown("unsupported_sudo_wrapper", "sudo", "shell_structure")
        return _wrapped(inner, values, depth, "sudo")
    if executable in {"bash", "fish", "sh", "zsh"}:
        nested = _shell_command(rest)
        if nested is None:
            return _unknown("unsupported_shell_script", executable, "shell_structure")
        if depth >= _MAX_WRAPPER_DEPTH:
            return _unknown("wrapper_depth_exceeded", executable, "shell_structure")
        result = _classify_command(nested, values, depth=depth + 1, wrapped=True)
        return replace(result, parse_confidence=_combined_confidence(result))

    if executable == "git":
        result = _classify_git(rest, values)
    elif executable == "gh":
        result = _classify_gh(rest, values)
    elif executable == "kubectl":
        result = _classify_kubectl(rest, values)
    elif executable == "terraform":
        result = _classify_terraform(rest, values)
    elif executable in _SCRIPT_RUNNERS:
        result = _unknown("uninspected_script", executable, "script_runner")
    else:
        head = " ".join((executable, rest[0])) if executable == "git" and rest else executable
        if executable == "sed" and any(
            token == "-i" or (token.startswith("-i") and len(token) > 2) for token in rest
        ):
            result = _known(
                "B", "local_write", _first_path(values), True, "shell_builtin", "sed.in_place"
            )
        elif head in _READ_COMMANDS:
            result = _known("A", "observation", _first_path(values), True, "shell_builtin", head)
        elif head in _LOCAL_WRITES:
            result = _known("B", "local_write", _first_path(values), True, "shell_builtin", head)
        else:
            result = _unknown("unclassified_command_effect", executable or "shell", "fallback")
    return replace(result, parse_confidence=_combined_confidence(result)) if wrapped else result


def _classify_git(words: Sequence[str], values: dict[str, Any]) -> Classification:
    if not words:
        return _unknown("missing_subcommand", "git", "git")
    verb = words[0]
    semantic = f"git.{verb}"
    if verb == "push":
        return _known(
            "C", "git_remote_write", _git_push_resource(list(words)), False, "git", semantic
        )
    head = f"git {verb}"
    if head in _READ_COMMANDS:
        return _known("A", "observation", _first_path(values), True, "git", semantic)
    if head in _LOCAL_WRITES:
        return _known("B", "local_write", _first_path(values), True, "git", semantic)
    return _unknown("unsupported_git_subcommand", "git", "git", semantic)


def _classify_gh(words: Sequence[str], values: dict[str, Any]) -> Classification:
    if not words:
        return _unknown("missing_subcommand", _external_resource(values, "github"), "gh")
    if words[0] == "api":
        write_fields = ("-f", "-F", "--field", "--raw-field", "--input")
        has_write_fields = any(
            token == option or token.startswith(option + "=")
            for token in words[1:]
            for option in write_fields
        )
        explicit_method = _option_value(words, "-X", "--method")
        method = (explicit_method or ("POST" if has_write_fields else "GET")).upper()
        if method == "GET":
            return _known(
                "A", "external_read", _gh_resource(words, values), True, "gh", "gh.api.get"
            )
        return _known(
            "C",
            "external_command_write",
            _gh_resource(words, values),
            False,
            "gh",
            f"gh.api.{method.lower()}",
        )
    entity = words[0]
    verb = words[1] if len(words) > 1 else ""
    semantic = f"gh.{entity}.{verb}" if verb else f"gh.{entity}"
    if verb in _GH_READ_VERBS or (entity == "auth" and verb == "status"):
        return _known("A", "external_read", _gh_resource(words, values), True, "gh", semantic)
    if verb in _GH_WRITE_VERBS or (entity == "pr" and verb == "review"):
        return _known(
            "C", "external_command_write", _gh_resource(words, values), False, "gh", semantic
        )
    return _unknown("unsupported_gh_subcommand", _gh_resource(words, values), "gh", semantic)


def _classify_kubectl(words: Sequence[str], values: dict[str, Any]) -> Classification:
    verb = _first_positional(
        words, options_with_values={"-n", "--namespace", "--context", "--kubeconfig"}
    )
    resource = _external_resource(values, _kubectl_resource(words, verb))
    if verb is None:
        return _unknown("missing_subcommand", resource, "kubectl")
    semantic = f"kubectl.{verb}"
    if verb in _KUBECTL_READ_VERBS:
        return _known("A", "external_read", resource, True, "kubectl", semantic)
    if verb in _KUBECTL_WRITE_VERBS:
        return _known("C", "external_command_write", resource, False, "kubectl", semantic)
    return _unknown("unsupported_kubectl_subcommand", resource, "kubectl", semantic)


def _classify_terraform(words: Sequence[str], values: dict[str, Any]) -> Classification:
    verb = _first_positional(words, options_with_values={"-chdir"})
    resource = _external_resource(values, "terraform workspace")
    if verb is None:
        return _unknown("missing_subcommand", resource, "terraform")
    semantic = f"terraform.{verb}"
    if verb == "fmt":
        if "-check" in words or "-check=true" in words:
            return _known("A", "observation", resource, True, "terraform", "terraform.fmt.check")
        return _known("B", "local_write", resource, True, "terraform", "terraform.fmt")
    if verb == "plan" and any(token == "-out" or token.startswith("-out=") for token in words):
        target = _option_value(words, "-out") or next(
            (token.partition("=")[2] for token in words if token.startswith("-out=")),
            "terraform plan artifact",
        )
        return _known("B", "local_write", target, True, "terraform", "terraform.plan.out")
    if verb in _TERRAFORM_READ_VERBS:
        return _known("A", "external_read", resource, True, "terraform", semantic)
    if verb in _TERRAFORM_WRITE_VERBS:
        return _known("C", "external_command_write", resource, False, "terraform", semantic)
    return _unknown("unsupported_terraform_subcommand", resource, "terraform", semantic)


def _tokens(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        return list(lexer)
    except ValueError:
        return None


def _components(tokens: Sequence[str]) -> tuple[list[list[str]], str | None]:
    components: list[list[str]] = [[]]
    output: str | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _COMPOSITION:
            components.append([])
        elif token in _OUTPUT_REDIRECTION:
            output = tokens[index + 1] if index + 1 < len(tokens) else "redirected output"
            index += 1
        elif token == "<":
            index += 1
        else:
            components[-1].append(token)
        index += 1
    return components, output


def _strongest(values: Sequence[Classification]) -> Classification:
    rank = {"A": 0, "B": 1, "C": 2}
    strongest = max(values, key=lambda item: rank[item.reversibility_class])
    unknown = next((item for item in values if item.parse_confidence == "unknown"), None)
    return unknown or strongest


def _wrapped(
    tokens: Sequence[str], values: dict[str, Any], depth: int, wrapper: str
) -> Classification:
    if depth >= _MAX_WRAPPER_DEPTH:
        return _unknown("wrapper_depth_exceeded", wrapper, "shell_structure")
    result = _classify_tokens(tokens, values, depth=depth + 1, wrapped=True)
    return replace(result, parse_confidence=_combined_confidence(result))


def _unwrap_env(words: Sequence[str]) -> list[str] | None:
    index = 0
    while index < len(words):
        token = words[index]
        if token == "--":
            return list(words[index + 1 :]) or None
        if token in {"-u", "--unset", "-C", "--chdir"}:
            index += 2
            continue
        if token.startswith("-") or ("=" in token and not token.startswith("=")):
            index += 1
            continue
        return list(words[index:])
    return None


def _unwrap_sudo(words: Sequence[str]) -> list[str] | None:
    options_with_values = {"-C", "-D", "-g", "-h", "-p", "-R", "-T", "-u"}
    index = 0
    while index < len(words):
        token = words[index]
        if token == "--":
            return list(words[index + 1 :]) or None
        if token in options_with_values:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return list(words[index:])
    return None


def _shell_command(words: Sequence[str]) -> str | None:
    for index, token in enumerate(words):
        if token in {"-c", "-lc", "--command"} and index + 1 < len(words):
            return words[index + 1]
    return None


def _first_positional(words: Sequence[str], *, options_with_values: set[str]) -> str | None:
    skip = False
    for token in words:
        if skip:
            skip = False
            continue
        option = token.partition("=")[0]
        if option in options_with_values and "=" not in token:
            skip = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _known(
    cls: ReversibilityClass,
    kind: str,
    resource: str,
    reversible: bool,
    classifier: str,
    semantic: str,
    *,
    confidence: ParseConfidence = "exact",
) -> Classification:
    return Classification(
        cls,
        kind,
        resource[:300],
        reversible,
        classifier,
        "recognized_semantics",
        confidence,
        semantic,
    )


def _unknown(
    reason: str, resource: str, classifier: str, semantic: str | None = None
) -> Classification:
    return Classification(
        "C",
        "unknown_command_effect",
        resource[:300],
        False,
        classifier,
        reason,
        "unknown",
        semantic,
    )


def _combined_confidence(value: Classification) -> ParseConfidence:
    return "unknown" if value.parse_confidence == "unknown" else "bounded"


def _option_value(words: Sequence[str], *names: str) -> str | None:
    for index, token in enumerate(words):
        if token in names and index + 1 < len(words):
            return words[index + 1]
        for name in names:
            if token.startswith(name + "="):
                return token.partition("=")[2]
            if len(name) == 2 and token.startswith(name) and len(token) > len(name):
                return token[len(name) :]
    return None


def _executable(value: str) -> str:
    return value.rsplit("/", 1)[-1].lower()


def _first_path(values: dict[str, Any]) -> str:
    for key in ("path", "file_path", "url", "uri"):
        if isinstance(values.get(key), str):
            return str(values[key])
    return "workspace"


def _external_resource(values: dict[str, Any], fallback: str) -> str:
    for key in ("resource", "url", "uri", "repository", "project", "name", "path", "cwd"):
        if isinstance(values.get(key), str):
            return str(values[key])[:300]
    return fallback[:300]


def _gh_resource(words: Sequence[str], values: dict[str, Any]) -> str:
    explicit = _option_value(words, "-R", "--repo")
    if explicit is not None:
        return explicit
    if words and words[0] == "api":
        skip = False
        for token in words[1:]:
            if skip:
                skip = False
                continue
            if token in {"-X", "--method", "-H", "--header"}:
                skip = True
                continue
            if not token.startswith("-"):
                return f"github:{token}"[:300]
    return _external_resource(values, "github")


def _kubectl_resource(words: Sequence[str], verb: str | None) -> str:
    namespace = _option_value(words, "-n", "--namespace")
    if verb in {"apply", "create", "delete", "patch", "replace"}:
        manifest = _option_value(words, "-f", "--filename")
        if manifest is not None:
            return f"kubernetes:manifest:{manifest}"[:300]
    if verb is not None:
        try:
            start = words.index(verb) + 1
        except ValueError:
            start = len(words)
        resource = _first_positional(
            words[start:], options_with_values={"-f", "--filename", "-n", "--namespace"}
        )
        if resource is not None:
            scope = f"namespace/{namespace}:" if namespace else ""
            return f"kubernetes:{scope}{resource}"[:300]
    return "kubernetes"


def _git_push_resource(words: list[str]) -> str:
    args = [word for word in words[1:] if not word.startswith("-")]
    return args[0] if args else "default git remote"
