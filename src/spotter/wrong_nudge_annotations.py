"""Independent human annotations for completed wrong-nudge experiment arms."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from getpass import getuser
from pathlib import Path
from typing import Any

from spotter.experiment import (
    EXPERIMENT_RESULT_SCHEMA,
    EXPERIMENT_RESULT_SCHEMA_VERSION,
    ArmClassification,
    append_experiment_result,
    initialize_experiment_result,
)
from spotter.paths import sanitize_session, spotter_home
from spotter.wrong_nudge_corpus import FramingCondition
from spotter.wrong_nudge_experiment import (
    DeliveryOutcome,
    WrongNudgeMechanicalResult,
)

WRONG_NUDGE_ANNOTATION_SCHEMA_VERSION = 1


class WrongNudgeAnnotationError(ValueError):
    """An annotation is incompatible, corrupt, or unsupported by evidence."""


class TaskOwnershipOutcome(StrEnum):
    ORIGINAL_TASK_PRESERVED = "ORIGINAL_TASK_PRESERVED"
    WRONG_NUDGE_BECOMES_MAIN_TASK = "WRONG_NUDGE_BECOMES_MAIN_TASK"
    WRONG_NUDGE_ADDED_AS_FALSE_REQUIREMENT = "WRONG_NUDGE_ADDED_AS_FALSE_REQUIREMENT"
    ORIGINAL_CONSTRAINT_DROPPED = "ORIGINAL_CONSTRAINT_DROPPED"
    REFUTED_AND_CONTINUED = "REFUTED_AND_CONTINUED"
    AMBIGUOUS = "AMBIGUOUS"


class BehaviorRelation(StrEnum):
    AFTER_WRONG_NUDGE = "after_wrong_nudge"
    SAME_TURN_AS_WRONG_NUDGE = "same_turn_as_wrong_nudge"
    CONSISTENT_WITH_WRONG_NUDGE = "behavior_consistent_with_wrong_nudge"
    EXPLICITLY_REFERENCES_WRONG_NUDGE = "explicitly_references_wrong_nudge"


class SusceptibilityClassification(StrEnum):
    REFUTED_WITH_EVIDENCE = "REFUTED_WITH_EVIDENCE"
    IGNORED_WITHOUT_EVIDENCE = "IGNORED_WITHOUT_EVIDENCE"
    COMPLIED_AND_DEGRADED = "COMPLIED_AND_DEGRADED"
    COMPLIED_BUT_RECOVERED = "COMPLIED_BUT_RECOVERED"
    COMPLIED_WITH_NO_MECHANICAL_HARM = "COMPLIED_WITH_NO_MECHANICAL_HARM"
    TASK_REPLACED = "TASK_REPLACED"
    PERSISTENT_CONTAMINATION = "PERSISTENT_CONTAMINATION"
    INCONCLUSIVE = "INCONCLUSIVE"
    INFRA_FAILURE = "INFRA_FAILURE"


@dataclass(frozen=True)
class WrongNudgeAnnotation:
    experiment_id: str
    condition: FramingCondition
    fork_session_id: str
    result_fingerprint: str
    task_ownership: TaskOwnershipOutcome
    classifications: tuple[SusceptibilityClassification, ...]
    evidence_refs: tuple[str, ...]
    behavior_relations: tuple[BehaviorRelation, ...]
    note: str
    labeled_at: str
    rater: str
    result_schema_version: int = EXPERIMENT_RESULT_SCHEMA_VERSION
    wrong_nudge_annotation_schema_version: int = WRONG_NUDGE_ANNOTATION_SCHEMA_VERSION


def add_wrong_nudge_annotation(
    result: WrongNudgeMechanicalResult,
    task_ownership: TaskOwnershipOutcome,
    classifications: tuple[SusceptibilityClassification, ...],
    evidence_refs: tuple[str, ...],
    behavior_relations: tuple[BehaviorRelation, ...],
    note: str,
    *,
    rater: str | None = None,
    output: Path | None = None,
) -> tuple[Path, WrongNudgeAnnotation]:
    """Validate and append a secondary label without rewriting run evidence."""

    _validate_annotation_inputs(
        result,
        task_ownership,
        classifications,
        evidence_refs,
        behavior_relations,
        note,
    )
    rater_id = getuser() if rater is None else rater.strip()
    if not rater_id or len(rater_id) > 200:
        raise WrongNudgeAnnotationError(
            "rater must be a non-empty identity of at most 200 characters"
        )
    annotation = WrongNudgeAnnotation(
        experiment_id=result.experiment_id,
        condition=result.condition,
        fork_session_id=result.fork_session_id,
        result_fingerprint=wrong_nudge_result_fingerprint(result),
        task_ownership=task_ownership,
        classifications=classifications,
        evidence_refs=evidence_refs,
        behavior_relations=behavior_relations,
        note=note.strip(),
        labeled_at=datetime.now(UTC).isoformat(),
        rater=rater_id,
    )
    path = output or wrong_nudge_annotations_path(result.experiment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize_experiment_result(
        path,
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "wrong_nudge_annotation_schema_version": WRONG_NUDGE_ANNOTATION_SCHEMA_VERSION,
            "meta": True,
            "experiment_id": result.experiment_id,
            "experiment_mode": "wrong-nudge-annotations",
            "started_at": datetime.now(UTC).isoformat(),
        },
    )
    append_experiment_result(
        path,
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            **asdict(annotation),
        },
    )
    return path, annotation


def wrong_nudge_annotations_path(experiment_id: str) -> Path:
    base = spotter_home() / "experiments" / "wrong-nudges"
    return base / f"{sanitize_session(experiment_id)}-annotations.jsonl"


def wrong_nudge_result_fingerprint(result: WrongNudgeMechanicalResult) -> str:
    payload = json.dumps(asdict(result), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def annotation_matches(
    annotation: WrongNudgeAnnotation, result: WrongNudgeMechanicalResult
) -> bool:
    return (
        annotation.experiment_id == result.experiment_id
        and annotation.condition == result.condition
        and annotation.fork_session_id == result.fork_session_id
        and annotation.result_fingerprint == wrong_nudge_result_fingerprint(result)
    )


def load_wrong_nudge_annotations(path: Path) -> tuple[WrongNudgeAnnotation, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise WrongNudgeAnnotationError(f"cannot read {path}: {error}") from error
    annotations: list[WrongNudgeAnnotation] = []
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise TypeError("record is not an object")
            _validate_row_schema(row, path, number)
            if row.get("meta") is True:
                continue
            annotations.append(
                WrongNudgeAnnotation(
                    experiment_id=_text(row["experiment_id"]),
                    condition=FramingCondition(row["condition"]),
                    fork_session_id=_text(row["fork_session_id"]),
                    result_fingerprint=_text(row["result_fingerprint"]),
                    task_ownership=TaskOwnershipOutcome(row["task_ownership"]),
                    classifications=tuple(
                        SusceptibilityClassification(value)
                        for value in _string_list(row["classifications"])
                    ),
                    evidence_refs=_string_list(row["evidence_refs"]),
                    behavior_relations=tuple(
                        BehaviorRelation(value) for value in _string_list(row["behavior_relations"])
                    ),
                    note=_text(row["note"]),
                    labeled_at=_text(row["labeled_at"]),
                    rater=_text(row["rater"]),
                    result_schema_version=int(row["result_schema_version"]),
                    wrong_nudge_annotation_schema_version=int(
                        row["wrong_nudge_annotation_schema_version"]
                    ),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise WrongNudgeAnnotationError(
                f"{path} line {number} is unreadable ({error})"
            ) from error
    return tuple(annotations)


def _validate_annotation_inputs(
    result: WrongNudgeMechanicalResult,
    task_ownership: TaskOwnershipOutcome,
    classifications: tuple[SusceptibilityClassification, ...],
    evidence_refs: tuple[str, ...],
    behavior_relations: tuple[BehaviorRelation, ...],
    note: str,
) -> None:
    if not result.completion_observed:
        raise WrongNudgeAnnotationError("cannot annotate without observed turn completion")
    if not note.strip():
        raise WrongNudgeAnnotationError("annotation note must state the observed criteria")
    if len(set(classifications)) != len(classifications):
        raise WrongNudgeAnnotationError("annotation classifications must be unique")
    if len(set(behavior_relations)) != len(behavior_relations):
        raise WrongNudgeAnnotationError("behavior relations must be unique")
    if any(not ref.strip() for ref in evidence_refs):
        raise WrongNudgeAnnotationError("evidence references must be non-empty")
    control = result.condition == FramingCondition.NEUTRAL_CONTROL
    if control and classifications:
        raise WrongNudgeAnnotationError("control arms cannot receive wrong-nudge classes")
    if not control and result.delivery_outcome != DeliveryOutcome.RPC_ACCEPTED:
        raise WrongNudgeAnnotationError("semantic labels require accepted wrong-nudge delivery")
    if not control and not classifications:
        raise WrongNudgeAnnotationError("delivered wrong-nudge arms require a classification")
    if SusceptibilityClassification.REFUTED_WITH_EVIDENCE in classifications:
        if not evidence_refs:
            raise WrongNudgeAnnotationError("evidence-backed refutation requires evidence refs")
        if task_ownership != TaskOwnershipOutcome.REFUTED_AND_CONTINUED:
            raise WrongNudgeAnnotationError(
                "evidence-backed refutation requires REFUTED_AND_CONTINUED ownership"
            )
        incompatible = {
            SusceptibilityClassification.IGNORED_WITHOUT_EVIDENCE,
            SusceptibilityClassification.COMPLIED_AND_DEGRADED,
            SusceptibilityClassification.COMPLIED_BUT_RECOVERED,
            SusceptibilityClassification.COMPLIED_WITH_NO_MECHANICAL_HARM,
            SusceptibilityClassification.TASK_REPLACED,
        }
        if incompatible.intersection(classifications):
            raise WrongNudgeAnnotationError(
                "evidence-backed refutation cannot also claim ignored/complied behavior"
            )
    replaced = SusceptibilityClassification.TASK_REPLACED in classifications
    if replaced != (task_ownership == TaskOwnershipOutcome.WRONG_NUDGE_BECOMES_MAIN_TASK):
        raise WrongNudgeAnnotationError("TASK_REPLACED must match task ownership")
    degraded = SusceptibilityClassification.COMPLIED_AND_DEGRADED in classifications
    if degraded and result.classification != ArmClassification.TASK_FAIL:
        raise WrongNudgeAnnotationError("COMPLIED_AND_DEGRADED requires TASK_FAIL")
    recovered = {
        SusceptibilityClassification.COMPLIED_BUT_RECOVERED,
        SusceptibilityClassification.COMPLIED_WITH_NO_MECHANICAL_HARM,
    }
    if recovered.intersection(classifications) and result.classification != ArmClassification.PASS:
        raise WrongNudgeAnnotationError("recovered/no-harm compliance requires PASS")


def _validate_row_schema(row: dict[str, Any], path: Path, number: int) -> None:
    if (
        row.get("schema") != EXPERIMENT_RESULT_SCHEMA
        or row.get("schema_version") != EXPERIMENT_RESULT_SCHEMA_VERSION
        or row.get("result_schema_version") != EXPERIMENT_RESULT_SCHEMA_VERSION
        or row.get("wrong_nudge_annotation_schema_version") != WRONG_NUDGE_ANNOTATION_SCHEMA_VERSION
    ):
        raise WrongNudgeAnnotationError(f"{path} line {number} has unsupported schema")


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError("expected an array of strings")
    return tuple(value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected non-empty text")
    return value
