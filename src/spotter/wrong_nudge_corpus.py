"""Immutable, versioned manifests for wrong-nudge experiments."""

import hashlib
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

WRONG_NUDGE_SCHEMA = "spotter.wrong_nudge"
WRONG_NUDGE_SCHEMA_VERSION = 1
WRONG_NUDGE_SET_SCHEMA = "spotter.wrong_nudge_set"
WRONG_NUDGE_SET_SCHEMA_VERSION = 1
EnumT = TypeVar("EnumT", bound=StrEnum)


class WrongNudgeCorpusError(ValueError):
    """A wrong-nudge corpus cannot be reproduced from its declared inputs."""


class NudgeKind(StrEnum):
    FALSE_CAUSAL_PREMISE = "false_causal_premise"
    INCORRECT_TARGET = "incorrect_target"
    SCOPE_EXPANSION = "scope_expansion"
    PREMATURE_STOP = "premature_stop"
    CONSTRAINT_CONTRADICTION = "constraint_contradiction"
    FALSIFIED_EVIDENCE_REQUEST = "falsified_evidence_request"
    ACCEPTANCE_CRITERION_CONFLICT = "acceptance_criterion_conflict"


class NudgeScope(StrEnum):
    CURRENT_DECISION = "CURRENT_DECISION"
    CURRENT_TURN = "CURRENT_TURN"


class HealthyResponse(StrEnum):
    REFUTE_WITH_AVAILABLE_EVIDENCE = "REFUTE_WITH_AVAILABLE_EVIDENCE"
    VERIFY_THEN_REFUTE = "VERIFY_THEN_REFUTE"
    PRESERVE_USER_CONSTRAINT = "PRESERVE_USER_CONSTRAINT"


@dataclass(frozen=True)
class WrongNudge:
    wrong_nudge_id: str
    path: Path
    source_task: str
    prefix_family: str
    kind: NudgeKind
    false_premise: str
    contradicting_evidence: str
    intended_scope: NudgeScope
    payload_template: str
    payload_version: int
    expected_healthy_response: HealthyResponse


@dataclass(frozen=True)
class WrongNudgeSet:
    wrong_nudge_set_id: str
    version: int
    nudges: tuple[WrongNudge, ...]


def validate_wrong_nudge_set(path: Path) -> WrongNudgeSet:
    """Validate identities and hashes without executing corpus content."""

    set_path = path.resolve()
    data = _load_toml(set_path)
    _schema_identity(
        data,
        "wrong_nudge_set_schema",
        WRONG_NUDGE_SET_SCHEMA,
        "wrong_nudge_set_schema_version",
        WRONG_NUDGE_SET_SCHEMA_VERSION,
        set_path,
    )
    set_id = _text(data, "wrong_nudge_set_id", set_path)
    version = _positive_int(data, "version", set_path)
    refs = data.get("nudges")
    if not isinstance(refs, list) or not refs:
        raise WrongNudgeCorpusError(f"{set_path}: nudges must be a non-empty array of tables")

    nudges: list[WrongNudge] = []
    seen: set[str] = set()
    for index, raw in enumerate(refs):
        where = f"{set_path}: nudges[{index}]"
        if not isinstance(raw, dict):
            raise WrongNudgeCorpusError(f"{where} must be a table")
        nudge_id = _text(raw, "wrong_nudge_id", where)
        if nudge_id in seen:
            raise WrongNudgeCorpusError(f"{set_path}: duplicate wrong_nudge_id {nudge_id!r}")
        seen.add(nudge_id)
        manifest = _contained_file(set_path.parent, _text(raw, "manifest", where), where)
        if _file_digest(manifest) != _text(raw, "sha256", where):
            raise WrongNudgeCorpusError(f"{where}: manifest sha256 mismatch")
        nudge = _validate_wrong_nudge(manifest)
        if nudge.wrong_nudge_id != nudge_id:
            raise WrongNudgeCorpusError(f"{where}: wrong_nudge_id does not match {manifest}")
        nudges.append(nudge)
    return WrongNudgeSet(set_id, version, tuple(nudges))


def _validate_wrong_nudge(path: Path) -> WrongNudge:
    data = _load_toml(path)
    _schema_identity(
        data,
        "wrong_nudge_schema",
        WRONG_NUDGE_SCHEMA,
        "wrong_nudge_schema_version",
        WRONG_NUDGE_SCHEMA_VERSION,
        path,
    )
    return WrongNudge(
        wrong_nudge_id=_text(data, "wrong_nudge_id", path),
        path=path,
        source_task=_text(data, "source_task", path),
        prefix_family=_text(data, "prefix_family", path),
        kind=_enum(data, "nudge_kind", NudgeKind, path),
        false_premise=_text(data, "false_premise", path),
        contradicting_evidence=_text(data, "contradicting_evidence", path),
        intended_scope=_enum(data, "intended_scope", NudgeScope, path),
        payload_template=_text(data, "payload_template", path),
        payload_version=_positive_int(data, "payload_version", path),
        expected_healthy_response=_enum(data, "expected_healthy_response", HealthyResponse, path),
    )


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise WrongNudgeCorpusError(f"cannot read {path}: {error}") from error


def _schema_identity(
    data: dict[str, Any],
    schema_key: str,
    expected_schema: str,
    version_key: str,
    expected_version: int,
    where: object,
) -> None:
    if data.get(schema_key) != expected_schema or data.get(version_key) != expected_version:
        raise WrongNudgeCorpusError(
            f"{where}: expected {schema_key}={expected_schema!r} and "
            f"{version_key}={expected_version}"
        )


def _enum(data: dict[str, Any], key: str, enum_type: type[EnumT], where: object) -> EnumT:
    value = _text(data, key, where)
    try:
        return enum_type(value)
    except ValueError as error:
        allowed = ", ".join(item.value for item in enum_type)
        raise WrongNudgeCorpusError(f"{where}: {key} must be one of {allowed}") from error


def _text(data: dict[str, Any], key: str, where: object) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WrongNudgeCorpusError(f"{where}: {key} must be non-empty text")
    return value.strip()


def _positive_int(data: dict[str, Any], key: str, where: object) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WrongNudgeCorpusError(f"{where}: {key} must be a positive integer")
    return value


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contained_file(root: Path, value: str, where: object) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise WrongNudgeCorpusError(f"{where}: path escapes the corpus: {value}")
    if not path.is_file():
        raise WrongNudgeCorpusError(f"{where}: manifest does not exist: {path}")
    return path
