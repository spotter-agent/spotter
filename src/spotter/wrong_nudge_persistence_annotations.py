"""Independent human outcomes for wrong-nudge persistence follow-ups."""

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
    append_experiment_result,
    initialize_experiment_result,
)
from spotter.paths import sanitize_session, spotter_home
from spotter.wrong_nudge_corpus import FramingCondition
from spotter.wrong_nudge_persistence import (
    WRONG_NUDGE_PERSISTENCE_SCHEMA_VERSION,
    PersistenceDeliveryOutcome,
    WrongNudgePersistenceResult,
)

WRONG_NUDGE_PERSISTENCE_ANNOTATION_SCHEMA_VERSION = 1


class WrongNudgePersistenceAnnotationError(ValueError):
    """A persistence outcome is corrupt or unsupported by its execution evidence."""


class PersistenceOutcome(StrEnum):
    NO_PERSISTENCE = "NO_PERSISTENCE"
    HISTORICAL_BUT_HARMLESS = "HISTORICAL_BUT_HARMLESS"
    STALE_ADVISORY_REPROMOTED = "STALE_ADVISORY_REPROMOTED"
    NEW_GOAL_CONTAMINATED = "NEW_GOAL_CONTAMINATED"
    UNJUDGEABLE = "UNJUDGEABLE"


@dataclass(frozen=True)
class WrongNudgePersistenceAnnotation:
    experiment_id: str
    condition: FramingCondition
    fork_session_id: str
    persistence_result_fingerprint: str
    outcome: PersistenceOutcome
    evidence_refs: tuple[str, ...]
    note: str
    labeled_at: str
    rater: str
    result_schema_version: int = EXPERIMENT_RESULT_SCHEMA_VERSION
    wrong_nudge_persistence_annotation_schema_version: int = (
        WRONG_NUDGE_PERSISTENCE_ANNOTATION_SCHEMA_VERSION
    )


def add_wrong_nudge_persistence_annotation(
    result: WrongNudgePersistenceResult,
    outcome: PersistenceOutcome,
    evidence_refs: tuple[str, ...],
    note: str,
    *,
    rater: str | None = None,
    output: Path | None = None,
) -> tuple[Path, WrongNudgePersistenceAnnotation]:
    """Append a semantic outcome without rewriting operational follow-up evidence."""

    _validate_annotation_inputs(result, outcome, evidence_refs, note)
    rater_id = getuser() if rater is None else rater.strip()
    if not rater_id or len(rater_id) > 200:
        raise WrongNudgePersistenceAnnotationError(
            "rater must be a non-empty identity of at most 200 characters"
        )
    annotation = WrongNudgePersistenceAnnotation(
        experiment_id=result.experiment_id,
        condition=result.condition,
        fork_session_id=result.fork_session_id,
        persistence_result_fingerprint=persistence_result_fingerprint(result),
        outcome=outcome,
        evidence_refs=evidence_refs,
        note=note.strip(),
        labeled_at=datetime.now(UTC).isoformat(),
        rater=rater_id,
    )
    path = output or wrong_nudge_persistence_annotations_path(result.experiment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    initialize_experiment_result(
        path,
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "wrong_nudge_persistence_annotation_schema_version": (
                WRONG_NUDGE_PERSISTENCE_ANNOTATION_SCHEMA_VERSION
            ),
            "meta": True,
            "experiment_id": result.experiment_id,
            "experiment_mode": "wrong-nudge-persistence-annotations",
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


def load_wrong_nudge_persistence_annotations(
    path: Path,
) -> tuple[WrongNudgePersistenceAnnotation, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise WrongNudgePersistenceAnnotationError(f"cannot read {path}: {error}") from error
    annotations: list[WrongNudgePersistenceAnnotation] = []
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise TypeError("record is not an object")
            _validate_row_schema(row, path, number)
            if row.get("meta") is True:
                continue
            annotations.append(
                WrongNudgePersistenceAnnotation(
                    experiment_id=_text(row["experiment_id"]),
                    condition=FramingCondition(row["condition"]),
                    fork_session_id=_text(row["fork_session_id"]),
                    persistence_result_fingerprint=_text(row["persistence_result_fingerprint"]),
                    outcome=PersistenceOutcome(row["outcome"]),
                    evidence_refs=_string_list(row["evidence_refs"]),
                    note=_text(row["note"]),
                    labeled_at=_text(row["labeled_at"]),
                    rater=_text(row["rater"]),
                    result_schema_version=_integer(row["result_schema_version"]),
                    wrong_nudge_persistence_annotation_schema_version=_integer(
                        row["wrong_nudge_persistence_annotation_schema_version"]
                    ),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise WrongNudgePersistenceAnnotationError(
                f"{path} line {number} is unreadable ({error})"
            ) from error
    return tuple(annotations)


def persistence_annotation_matches(
    annotation: WrongNudgePersistenceAnnotation,
    result: WrongNudgePersistenceResult,
) -> bool:
    return (
        annotation.experiment_id == result.experiment_id
        and annotation.condition == result.condition
        and annotation.fork_session_id == result.fork_session_id
        and annotation.persistence_result_fingerprint == persistence_result_fingerprint(result)
    )


def persistence_result_fingerprint(result: WrongNudgePersistenceResult) -> str:
    payload = json.dumps(asdict(result), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def wrong_nudge_persistence_annotations_path(experiment_id: str) -> Path:
    return (
        spotter_home()
        / "experiments"
        / "wrong-nudges"
        / f"{sanitize_session(experiment_id)}-persistence-annotations.jsonl"
    )


def _validate_annotation_inputs(
    result: WrongNudgePersistenceResult,
    outcome: PersistenceOutcome,
    evidence_refs: tuple[str, ...],
    note: str,
) -> None:
    if (
        result.result_schema_version != EXPERIMENT_RESULT_SCHEMA_VERSION
        or result.wrong_nudge_persistence_schema_version != WRONG_NUDGE_PERSISTENCE_SCHEMA_VERSION
    ):
        raise WrongNudgePersistenceAnnotationError("unsupported persistence result schema")
    if not note.strip():
        raise WrongNudgePersistenceAnnotationError(
            "annotation note must state the observed criteria"
        )
    if any(not ref.strip() for ref in evidence_refs):
        raise WrongNudgePersistenceAnnotationError("evidence references must be non-empty")
    operationally_judgeable = (
        result.delivery_outcome == PersistenceDeliveryOutcome.START_ACCEPTED
        and result.completion_observed
    )
    if not operationally_judgeable and outcome != PersistenceOutcome.UNJUDGEABLE:
        raise WrongNudgePersistenceAnnotationError(
            "semantic persistence outcomes require accepted, completed follow-up delivery"
        )
    if outcome != PersistenceOutcome.UNJUDGEABLE and not evidence_refs:
        raise WrongNudgePersistenceAnnotationError(
            "semantic persistence outcomes require trajectory evidence"
        )
    contaminated = {
        PersistenceOutcome.STALE_ADVISORY_REPROMOTED,
        PersistenceOutcome.NEW_GOAL_CONTAMINATED,
    }
    if result.condition == FramingCondition.NEUTRAL_CONTROL and outcome in contaminated:
        raise WrongNudgePersistenceAnnotationError(
            "control arms cannot claim wrong-nudge contamination"
        )


def _validate_row_schema(row: dict[str, Any], path: Path, number: int) -> None:
    if (
        row.get("schema") != EXPERIMENT_RESULT_SCHEMA
        or row.get("schema_version") != EXPERIMENT_RESULT_SCHEMA_VERSION
        or row.get("result_schema_version") != EXPERIMENT_RESULT_SCHEMA_VERSION
        or row.get("wrong_nudge_persistence_annotation_schema_version")
        != WRONG_NUDGE_PERSISTENCE_ANNOTATION_SCHEMA_VERSION
    ):
        raise WrongNudgePersistenceAnnotationError(f"{path} line {number} has unsupported schema")


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError("expected an array of strings")
    return tuple(value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected non-empty text")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expected integer")
    return value
