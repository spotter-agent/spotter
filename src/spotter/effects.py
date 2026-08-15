"""Bounded reversibility classification and external-effect reconstruction.

Classification runs in the synchronous hook.  It is deliberately deterministic,
bounded, and conservative: recognized semantics may prove an action safer, while
unknown command shapes still map to Class C without being mislabeled as known writes.
"""

import shlex
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4, uuid5

from spotter.config import McpToolSemantics
from spotter.outcomes import outcome_failure
from spotter.trace import TraceEvent

ReversibilityClass = Literal["A", "B", "C"]
ParseConfidence = Literal["exact", "bounded", "unknown"]
EffectResolution = Literal[
    "reversed", "compensated", "reconciled_present", "reconciled_absent", "still_unresolved"
]

_MAX_TOKENS = 64
_MAX_COMMAND_CHARS = 4096
_MAX_WRAPPER_DEPTH = 2
_COMPOSITION = frozenset({"&&", "||", ";", "|", "&"})
_OUTPUT_REDIRECTION = frozenset({">", ">>"})
_EFFECT_NAMESPACE = UUID("264972c0-2308-4ea9-b6b4-08a0310d2a78")


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


class EffectResolutionError(ValueError):
    """An explicit effect-resolution record is malformed or ambiguous."""


@dataclass(frozen=True)
class EffectCoverageReport:
    operations_total: int
    classified_exact: int
    classified_bounded: int
    unknown_family: int
    unknown_shell_shape: int
    unknown_mcp_tool: int
    unknown_adapter_operation: int
    conservative_c_from_unknown: int
    missing_provenance: int
    unknown_reasons: Mapping[str, int]


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
_HTTP_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_HTTP_WRITE_METHODS = frozenset({"DELETE", "PATCH", "POST", "PUT"})
_CURL_BOOLEAN_SHORT_OPTIONS = frozenset("#0123456789:BaGfghiIJjklLMnNOqRSsvVZ")
_CURL_VALUE_SHORT_OPTIONS = frozenset(
    {
        "A",
        "b",
        "C",
        "c",
        "D",
        "d",
        "E",
        "e",
        "F",
        "H",
        "K",
        "m",
        "o",
        "P",
        "Q",
        "r",
        "T",
        "t",
        "U",
        "u",
        "w",
        "X",
        "x",
        "Y",
        "y",
        "z",
    }
)
_SQL_READ_VERBS = frozenset({"DESC", "DESCRIBE", "SHOW"})
_SQL_WRITE_VERBS = frozenset(
    {
        "ALTER",
        "CALL",
        "COMMENT",
        "CREATE",
        "DELETE",
        "DO",
        "DROP",
        "GRANT",
        "INSERT",
        "MERGE",
        "REINDEX",
        "REVOKE",
        "TRUNCATE",
        "UPDATE",
        "VACUUM",
    }
)


def classify(
    tool: object,
    tool_input: object,
    mcp_semantics: Sequence[McpToolSemantics] = (),
) -> Classification:
    """Classify an action without I/O, model calls, or unbounded inspection."""

    name = str(tool or "")
    values = tool_input if isinstance(tool_input, dict) else {}
    if name == "apply_patch":
        return _known("B", "workspace_write", _first_path(values), True, "native", "apply_patch")

    lowered = name.lower()
    if lowered.startswith("mcp__") or lowered.startswith("mcp_"):
        identity = _mcp_identity(lowered)
        configured = _configured_mcp(identity, values, mcp_semantics)
        if configured is not None:
            return configured
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


def _mcp_identity(name: str) -> tuple[str, str] | None:
    parts = name.split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp" or not parts[1] or not parts[2]:
        return None
    return parts[1].casefold(), parts[2].casefold()


def _configured_mcp(
    identity: tuple[str, str] | None,
    values: dict[str, Any],
    semantics: Sequence[McpToolSemantics],
) -> Classification | None:
    if identity is None:
        return None
    for rule in semantics[:256]:
        if (rule.server, rule.tool) != identity:
            continue
        resource = _configured_mcp_resource(identity, values, rule.resource_fields)
        cls = cast(ReversibilityClass, rule.reversibility)
        kind = (
            "external_read"
            if cls == "A"
            else ("configured_tool_write" if cls == "B" else "external_tool_write")
        )
        return Classification(
            cls,
            kind,
            resource,
            cls != "C",
            "mcp_config",
            "configured_semantics",
            "exact",
            f"mcp.{identity[0]}.{identity[1]}.{rule.operation}",
        )
    return None


def _configured_mcp_resource(
    identity: tuple[str, str], values: dict[str, Any], fields: Sequence[str]
) -> str:
    parts: list[str] = []
    for field in fields:
        value = values.get(field)
        if not isinstance(value, (str, int, bool)):
            continue
        rendered = str(value)
        if isinstance(value, str) and "://" in value:
            rendered = _sanitize_remote_resource(value)
        parts.append(f"{field}={rendered}")
    if parts:
        return "|".join(parts)[:300]
    return f"mcp:{identity[0]}/{identity[1]}"


def effect_event(result: TraceEvent) -> TraceEvent | None:
    """Turn an accepted or completed Class C call into a compact ledger observation."""

    started = result.kind in {"tool_started", "command_started", "file_change_started"}
    completed = result.kind in {"tool_result", "command_result", "file_edit"}
    if (not started and not completed) or result.payload.get("reversibility_class") != "C":
        return None
    outcome, evidence = (
        ("unknown", "operation_started") if started else _effect_outcome(result.payload)
    )
    effect_id, correlation_quality = _effect_identity(result)
    return TraceEvent(
        "external_effect",
        {
            "effect_id": effect_id,
            "correlation_quality": correlation_quality,
            "source_event_ids": _source_event_ids(result),
            "kind": result.payload.get("effect_kind"),
            "resource": result.payload.get("resource"),
            "result": outcome,
            "outcome": outcome,
            "outcome_evidence": evidence,
            "lifecycle": "attempted" if started else "completed",
            "reversible": False,
            "checkpoint": result.payload.get("checkpoint"),
            "turn_id": result.payload.get("turn_id"),
            "tool_use_id": result.payload.get("tool_use_id"),
            "classifier": result.payload.get("effect_classifier"),
            "reason": result.payload.get("effect_reason"),
            "confidence": result.payload.get("effect_confidence"),
            "semantic_operation": result.payload.get("semantic_operation"),
        },
        identity=result.identity,
        operation_id=result.operation_id,
        item_id=result.item_id,
        provenance=result.provenance,
        connection_epoch=result.connection_epoch,
    )


def _effect_identity(event: TraceEvent) -> tuple[str, str]:
    operation_id = event.operation_id or _optional_identifier(event.payload.get("tool_use_id"))
    if operation_id is not None:
        return f"effect-{uuid5(_EFFECT_NAMESPACE, f'operation:{operation_id}').hex}", "exact"

    components = [
        _optional_identifier(event.event_id),
        _optional_identifier(event.payload.get("turn_id")),
        _optional_identifier(event.payload.get("semantic_operation")),
        _optional_identifier(event.payload.get("resource")),
    ]
    fingerprint = "\x1f".join(value for value in components if value is not None)
    return f"effect-{uuid5(_EFFECT_NAMESPACE, f'fingerprint:{fingerprint}').hex}", "inferred"


def _source_event_ids(event: TraceEvent) -> list[str]:
    candidates = (
        ("event", event.event_id),
        ("operation", event.operation_id),
        ("item", event.item_id),
        ("tool", event.payload.get("tool_use_id")),
    )
    return [
        f"{kind}:{value}"[:300]
        for kind, raw in candidates
        if (value := _optional_identifier(raw)) is not None
    ][:8]


def _optional_identifier(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _effect_outcome(payload: Mapping[str, object]) -> tuple[str, str]:
    response = payload.get("tool_response")
    statuses = [payload.get("status")]
    if isinstance(response, Mapping):
        statuses.append(response.get("status"))
        if response.get("partial") is True:
            return "partial", "partial_result"
    if any(
        isinstance(status, str) and status.casefold() in {"partial", "partially_succeeded"}
        for status in statuses
    ):
        return "partial", "partial_result"

    failed = outcome_failure(payload)
    if failed is True:
        return "failed", "explicit_failure"
    if _has_explicit_success(payload, response):
        return "succeeded", "explicit_success"
    if _has_zero_exit_code(payload, response):
        return "unknown", "exit_zero_only"
    return "unknown", "no_conclusive_result"


def _has_explicit_success(payload: Mapping[str, object], response: object) -> bool:
    candidates: list[object] = [payload.get("success"), payload.get("status")]
    if isinstance(response, Mapping):
        candidates.extend((response.get("ok"), response.get("success"), response.get("status")))
    has_exit_code = _has_exit_code(payload, response)
    return any(
        value is True
        or (
            isinstance(value, str)
            and value.casefold() in {"completed", "passed", "succeeded", "success"}
            and not (has_exit_code and value.casefold() == "completed")
        )
        for value in candidates
    )


def _has_exit_code(payload: Mapping[str, object], response: object) -> bool:
    values = [payload.get("exitCode"), payload.get("exit_code")]
    if isinstance(response, Mapping):
        values.append(response.get("exit_code"))
    return any(isinstance(value, int) and not isinstance(value, bool) for value in values)


def _has_zero_exit_code(payload: Mapping[str, object], response: object) -> bool:
    values = [payload.get("exitCode"), payload.get("exit_code")]
    if isinstance(response, Mapping):
        values.append(response.get("exit_code"))
    return any(value == 0 and not isinstance(value, bool) for value in values)


def external_effects(
    records: Iterable[Any], through_step: int | None = None
) -> list[dict[str, Any]]:
    """Enumerate effects a local restore cannot claim to have undone."""

    effects: list[dict[str, Any]] = []
    exact: dict[str, int] = {}
    resolutions: list[dict[str, Any]] = []
    for record in records:
        if through_step is not None and record.step > through_step:
            continue
        if record.event.kind == "effect_resolution":
            resolutions.append(dict(record.event.payload))
            continue
        if record.event.kind != "external_effect":
            continue
        payload = dict(record.event.payload)
        effect_id = _optional_identifier(payload.get("effect_id"))
        if payload.get("correlation_quality") != "exact" or effect_id is None:
            effects.append(payload)
            continue
        previous = exact.get(effect_id)
        if previous is None:
            exact[effect_id] = len(effects)
            effects.append(payload)
        else:
            effects[previous] = _merge_effect_observations(effects[previous], payload)
    for resolution in resolutions:
        target = _optional_identifier(resolution.get("effect_id"))
        if target is not None and target in exact:
            effects[exact[target]] = _apply_effect_resolution(
                effects[exact[target]], resolution, set(exact)
            )
    return effects


def measure_effect_coverage(records: Iterable[Any]) -> EffectCoverageReport:
    """Measure classifier support from proposal records without double-counting results."""

    total = exact = bounded = conservative = missing = 0
    buckets: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for record in records:
        event = getattr(record, "event", None)
        if not isinstance(event, TraceEvent) or event.kind != "tool_proposal":
            continue
        total += 1
        confidence = event.payload.get("effect_confidence")
        if confidence == "exact":
            exact += 1
            continue
        if confidence == "bounded":
            bounded += 1
            continue
        if confidence != "unknown":
            missing += 1
            continue
        reason = event.payload.get("effect_reason")
        classifier = event.payload.get("effect_classifier")
        reason_name = reason if isinstance(reason, str) and reason else "unknown_reason"
        reasons[reason_name] += 1
        if event.payload.get("reversibility_class") == "C":
            conservative += 1
        if reason_name == "unknown_mcp_tool":
            buckets["mcp"] += 1
        elif classifier == "shell_structure":
            buckets["shell"] += 1
        elif reason_name == "unknown_tool_effect":
            buckets["adapter"] += 1
        else:
            buckets["family"] += 1
    return EffectCoverageReport(
        operations_total=total,
        classified_exact=exact,
        classified_bounded=bounded,
        unknown_family=buckets["family"],
        unknown_shell_shape=buckets["shell"],
        unknown_mcp_tool=buckets["mcp"],
        unknown_adapter_operation=buckets["adapter"],
        conservative_c_from_unknown=conservative,
        missing_provenance=missing,
        unknown_reasons=dict(sorted(reasons.items())),
    )


def render_effect_coverage(report: EffectCoverageReport) -> str:
    reasons = ", ".join(f"{key}={value}" for key, value in report.unknown_reasons.items()) or "none"
    return (
        "Effect classification coverage:\n"
        f"  operations={report.operations_total}, exact={report.classified_exact}, "
        f"bounded={report.classified_bounded}, missing_provenance={report.missing_provenance}\n"
        f"  unknown family={report.unknown_family}, shell_shape={report.unknown_shell_shape}, "
        f"mcp_tool={report.unknown_mcp_tool}, "
        f"adapter_operation={report.unknown_adapter_operation}, "
        f"conservative_C={report.conservative_c_from_unknown}\n"
        f"  unknown reasons: {reasons}"
    )


def effect_resolution_event(
    effect_id: str,
    resolution: EffectResolution,
    evidence: str,
    *,
    related_effect_id: str | None = None,
) -> TraceEvent:
    """Create an append-only explicit reversal, compensation, or reconciliation record."""

    if not effect_id.startswith("effect-") or len(effect_id) > 80:
        raise EffectResolutionError("effect_id must be a bounded stable effect id")
    if resolution not in {
        "reversed",
        "compensated",
        "reconciled_present",
        "reconciled_absent",
        "still_unresolved",
    }:
        raise EffectResolutionError("unsupported effect resolution")
    evidence = evidence.strip()
    if not evidence or len(evidence) > 500:
        raise EffectResolutionError("resolution evidence must contain 1-500 characters")
    if resolution == "compensated" and related_effect_id is None:
        raise EffectResolutionError("compensated effects require related_effect_id")
    if related_effect_id is not None:
        if not related_effect_id.startswith("effect-") or len(related_effect_id) > 80:
            raise EffectResolutionError("related_effect_id must be a bounded stable effect id")
        if related_effect_id == effect_id:
            raise EffectResolutionError("an effect cannot resolve itself")
    resolution_event_id = f"effect-resolution-{uuid4().hex}"
    return TraceEvent(
        "effect_resolution",
        {
            "resolution_event_id": resolution_event_id,
            "effect_id": effect_id,
            "resolution": resolution,
            "evidence": evidence,
            "related_effect_id": related_effect_id,
        },
        event_id=resolution_event_id,
    )


def _apply_effect_resolution(
    effect: dict[str, Any], resolution: dict[str, Any], known_effect_ids: set[str]
) -> dict[str, Any]:
    value = resolution.get("resolution")
    if value not in {
        "reversed",
        "compensated",
        "reconciled_present",
        "reconciled_absent",
        "still_unresolved",
    }:
        return effect
    related = _optional_identifier(resolution.get("related_effect_id"))
    relation_verified = value != "compensated" or related in known_effect_ids
    entry = {
        key: resolution.get(key)
        for key in ("resolution_event_id", "resolution", "evidence", "related_effect_id")
    }
    entry["relation_verified"] = relation_verified
    history = effect.get("resolution_history", [])
    if not isinstance(history, list):
        history = []
    merged = {
        **effect,
        "resolution": value,
        "resolved": relation_verified and value in {"reversed", "compensated", "reconciled_absent"},
        "resolution_relation_verified": relation_verified,
        "resolution_history": [*history, entry][-16:],
    }
    return merged


def _merge_effect_observations(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    previous_terminal = previous.get("outcome_evidence") != "operation_started"
    current_terminal = current.get("outcome_evidence") != "operation_started"
    merged = (
        {**current, **previous}
        if previous_terminal and not current_terminal
        else {**previous, **current}
    )
    merged["source_event_ids"] = list(
        dict.fromkeys(
            (
                *_identifier_list(previous.get("source_event_ids")),
                *_identifier_list(current.get("source_event_ids")),
            )
        )
    )[:16]
    merged["observation_count"] = _observation_count(previous) + _observation_count(current)
    observed = _identifier_list(previous.get("observed_outcomes"))
    outcomes = {
        value
        for value in (
            *(observed or (previous.get("outcome"),)),
            current.get("outcome"),
        )
        if isinstance(value, str)
    }
    if not previous_terminal:
        outcomes.discard(previous.get("outcome"))
    if not current_terminal:
        outcomes.discard(current.get("outcome"))
    merged["lifecycle"] = "completed" if previous_terminal or current_terminal else "attempted"
    if len(outcomes) > 1:
        merged["result"] = "unknown"
        merged["outcome"] = "unknown"
        merged["outcome_evidence"] = "conflicting_observations"
        merged["observed_outcomes"] = sorted(outcomes)
    return merged


def _identifier_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _observation_count(payload: Mapping[str, object]) -> int:
    value = payload.get("observation_count", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


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
    elif executable == "curl":
        result = _classify_curl(rest, values)
    elif executable in {"mysql", "psql"}:
        result = _classify_database(executable, rest, values)
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


def _classify_curl(words: Sequence[str], values: dict[str, Any]) -> Classification:
    normalized = _normalize_curl_args(words)
    if normalized is None:
        return _unknown("unsupported_curl_cluster", "http", "curl")
    words = normalized
    if _has_option_prefix(words, "-K", "--config") or _has_option(words, "-:", "--next"):
        return _unknown("unsupported_curl_shape", "http", "curl")
    if _has_option(words, "-Q", "--quote"):
        return _unknown("unsupported_curl_remote_command", "http", "curl")
    url = _curl_url(words)
    if url is None:
        return _unknown("missing_http_resource", "http", "curl")

    explicit_method = _option_value(words, "-X", "--request")
    has_data = _has_option_prefix(
        words,
        "-d",
        "-F",
        "--data",
        "--data-ascii",
        "--data-binary",
        "--data-raw",
        "--data-urlencode",
        "--form",
        "--form-string",
        "--json",
    )
    upload = _option_value(words, "-T", "--upload-file")
    if explicit_method is not None:
        method = explicit_method.upper()
    elif _has_option(words, "-G", "--get"):
        method = "GET"
    elif _has_option(words, "-I", "--head"):
        method = "HEAD"
    elif upload is not None:
        method = "PUT"
    elif has_data:
        method = "POST"
    else:
        method = "GET"

    resource = _sanitize_remote_resource(url)
    semantic = f"http.{method.lower()}"
    if method in _HTTP_WRITE_METHODS:
        return _known("C", "external_command_write", resource, False, "curl", semantic)
    if method not in _HTTP_READ_METHODS:
        return _unknown("unsupported_http_method", resource, "curl", semantic)
    if upload is not None or (has_data and not _has_option(words, "-G", "--get")):
        return _unknown("http_read_method_with_request_body", resource, "curl", semantic)

    output = _curl_local_output(words)
    if output is not None:
        return _known(
            "B",
            "local_write",
            f"{output} <- {resource}",
            True,
            "curl",
            f"{semantic}.download",
        )
    return _known("A", "external_read", resource, True, "curl", semantic)


def _classify_database(
    executable: str, words: Sequence[str], values: dict[str, Any]
) -> Classification:
    classifier = "postgres" if executable == "psql" else "mysql"
    resource = _database_resource(executable, words, values)
    if _has_option(words, "--help", "--version"):
        return _known("A", "observation", executable, True, classifier, f"{classifier}.help")
    if _has_option_prefix(words, "-f", "--file") or _has_option(words, "--init-command"):
        return _unknown("uninspected_database_script", resource, classifier)

    queries = _option_values(
        words,
        *(("-c", "--command") if executable == "psql" else ("-e", "--execute")),
    )
    if len(queries) > 1:
        return _unknown("multiple_database_commands", resource, classifier)
    if not queries:
        if executable == "psql" and _has_option(words, "-l", "--list"):
            return _known("A", "external_read", resource, True, classifier, "postgres.list")
        return _unknown("interactive_database_command", resource, classifier)
    verb = _sql_verb(queries[0])
    if verb is None:
        return _unknown("unsupported_sql_shape", resource, classifier)
    semantic = f"{classifier}.{verb.lower()}"
    if verb in _SQL_READ_VERBS:
        return _known("A", "external_read", resource, True, classifier, semantic)
    if verb in _SQL_WRITE_VERBS:
        return _known("C", "external_command_write", resource, False, classifier, semantic)
    return _unknown("unsupported_sql_semantics", resource, classifier, semantic)


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
    values = _option_values(words, *names)
    return values[-1] if values else None


def _option_values(words: Sequence[str], *names: str) -> list[str]:
    values: list[str] = []
    for index, token in enumerate(words):
        if token in names and index + 1 < len(words):
            values.append(words[index + 1])
            continue
        for name in names:
            if token.startswith(name + "="):
                values.append(token.partition("=")[2])
                break
            if len(name) == 2 and token.startswith(name) and len(token) > len(name):
                values.append(token[len(name) :])
                break
    return values


def _has_option(words: Sequence[str], *names: str) -> bool:
    return any(
        token in names or any(token.startswith(name + "=") for name in names) for token in words
    )


def _has_option_prefix(words: Sequence[str], *names: str) -> bool:
    for token in words:
        for name in names:
            if token == name or token.startswith(name + "="):
                return True
            if len(name) == 2 and token.startswith(name) and len(token) > len(name):
                return True
    return False


def _has_short_flag(words: Sequence[str], flag: str) -> bool:
    return any(token == f"-{flag}" for token in words)


def _normalize_curl_args(words: Sequence[str]) -> list[str] | None:
    normalized: list[str] = []
    index = 0
    while index < len(words):
        token = words[index]
        if not token.startswith("-") or token.startswith("--") or token == "-":
            normalized.append(token)
            index += 1
            continue
        cluster = token[1:]
        offset = 0
        while offset < len(cluster):
            option = cluster[offset]
            if option in _CURL_BOOLEAN_SHORT_OPTIONS:
                normalized.append(f"-{option}")
                offset += 1
                continue
            if option not in _CURL_VALUE_SHORT_OPTIONS:
                return None
            normalized.append(f"-{option}")
            attached = cluster[offset + 1 :]
            if attached:
                normalized.append(attached)
            elif index + 1 < len(words):
                index += 1
                normalized.append(words[index])
            else:
                return None
            break
        index += 1
    return normalized


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


def _curl_url(words: Sequence[str]) -> str | None:
    explicit = _option_value(words, "--url")
    if explicit is not None:
        return explicit
    return next(
        (token for token in words if token.startswith(("http://", "https://"))),
        None,
    )


def _curl_local_output(words: Sequence[str]) -> str | None:
    output = _option_value(words, "-o", "--output")
    if output is not None:
        return output
    file_options = (
        ("-D", "--dump-header"),
        ("-c", "--cookie-jar"),
        ("--alt-svc",),
        ("--etag-save",),
        ("--hsts",),
        ("--stderr",),
        ("--trace",),
        ("--trace-ascii",),
    )
    for names in file_options:
        if value := _option_value(words, *names):
            return value
    write_out = _option_value(words, "-w", "--write-out")
    if write_out is not None and "%output{" in write_out:
        return "curl write-out file"
    if _has_short_flag(words, "O") or _has_option(
        words, "--remote-name", "--remote-header-name", "--remote-name-all"
    ):
        return "remote-named download"
    return None


def _sanitize_remote_resource(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        without_query = value.partition("?")[0].partition("#")[0]
        scheme, separator, remainder = without_query.partition("://")
        if separator:
            return f"{scheme}{separator}{remainder.rsplit('@', 1)[-1]}"[:300]
        return without_query[:300]
    if not parsed.scheme or not parsed.hostname:
        return value.partition("?")[0].partition("#")[0][:300]
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))[:300]


def _database_resource(executable: str, words: Sequence[str], values: dict[str, Any]) -> str:
    explicit = values.get("resource")
    if isinstance(explicit, str):
        return _sanitize_remote_resource(explicit) if "://" in explicit else explicit[:300]
    if executable == "psql":
        database = _option_value(words, "-d", "--dbname")
        host = _option_value(words, "-h", "--host")
        family = "postgres"
    else:
        database = _option_value(words, "-D", "--database")
        host = _option_value(words, "-h", "--host")
        family = "mysql"
    if database and "://" in database:
        return _sanitize_remote_resource(database)
    parts = [family]
    if host:
        parts.append(f"host/{host}")
    if database:
        parts.append(f"database/{database}")
    return ":".join(parts)[:300]


def _sql_verb(query: str) -> str | None:
    stripped = query.strip()
    if not stripped or stripped.startswith(("--", "/*", "\\")):
        return None
    statements = [part.strip() for part in stripped.split(";") if part.strip()]
    if len(statements) != 1:
        return None
    first = statements[0].split(maxsplit=1)[0]
    return first.upper() if first.isalpha() else None


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
