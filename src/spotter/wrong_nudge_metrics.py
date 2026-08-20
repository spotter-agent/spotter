"""Coverage-aware paired metrics for wrong-nudge experiment results."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spotter.experiment import (
    EXPERIMENT_RESULT_SCHEMA,
    EXPERIMENT_RESULT_SCHEMA_VERSION,
    ArmClassification,
)
from spotter.task_corpus import CommandResult
from spotter.wrong_nudge_annotations import (
    WRONG_NUDGE_ANNOTATION_SCHEMA_VERSION,
    SusceptibilityClassification,
    TaskOwnershipOutcome,
    WrongNudgeAnnotation,
    annotation_matches,
    wrong_nudge_result_fingerprint,
)
from spotter.wrong_nudge_corpus import FramingCondition
from spotter.wrong_nudge_experiment import (
    WRONG_NUDGE_RESULT_SCHEMA_VERSION,
    DeliveryOutcome,
    WrongNudgeMechanicalResult,
)
from spotter.wrong_nudge_persistence import (
    WRONG_NUDGE_PERSISTENCE_SCHEMA_VERSION,
    PersistenceDeliveryOutcome,
    WrongNudgePersistenceResult,
)
from spotter.wrong_nudge_persistence_annotations import (
    WRONG_NUDGE_PERSISTENCE_ANNOTATION_SCHEMA_VERSION,
    PersistenceOutcome,
    WrongNudgePersistenceAnnotation,
    persistence_annotation_matches,
)


class WrongNudgeMetricsError(ValueError):
    """Wrong-nudge results cannot be safely interpreted."""


@dataclass(frozen=True)
class WrongNudgeConditionReport:
    condition: FramingCondition
    attempts: int
    delivery_accepted: int
    completion_observed: int
    mechanically_judgeable_pairs: int
    control_pass_nudge_fail: int
    semantic_eligible: int
    semantically_labeled: int
    annotation_conflicts: int
    stale_annotations: int
    refuted_with_evidence: int
    complied: int
    recovered_after_compliance: int
    task_replaced: int
    original_constraint_dropped: int
    persistent_contamination: int


@dataclass(frozen=True)
class WrongNudgeReport:
    conditions: tuple[WrongNudgeConditionReport, ...]
    orphan_annotations: int
    persistence_conditions: tuple["WrongNudgePersistenceConditionReport", ...] = ()
    orphan_persistence_annotations: int = 0


@dataclass(frozen=True)
class WrongNudgePersistenceConditionReport:
    condition: FramingCondition
    attempts: int
    delivery_accepted: int
    completion_observed: int
    labeled: int
    annotation_conflicts: int
    stale_annotations: int
    no_persistence: int
    historical_but_harmless: int
    stale_advisory_repromoted: int
    new_goal_contaminated: int
    unjudgeable: int


def load_wrong_nudge_results(path: Path) -> tuple[WrongNudgeMechanicalResult, ...]:
    """Load partial or complete durable arm rows without inventing missing results."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise WrongNudgeMetricsError(f"cannot read {path}: {error}") from error
    results: list[WrongNudgeMechanicalResult] = []
    seen: set[tuple[str, FramingCondition]] = set()
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise TypeError("record is not an object")
            _validate_result_schema(row, path, number)
            if row.get("meta") is True or row.get("complete") is True:
                continue
            result = _result_from_row(row)
            key = (result.experiment_id, result.condition)
            if key in seen:
                raise WrongNudgeMetricsError(f"{path} contains duplicate result {key}")
            seen.add(key)
            results.append(result)
        except WrongNudgeMetricsError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise WrongNudgeMetricsError(f"{path} line {number} is unreadable ({error})") from error
    return tuple(results)


def measure_wrong_nudge_susceptibility(
    results: tuple[WrongNudgeMechanicalResult, ...],
    annotations: tuple[WrongNudgeAnnotation, ...] = (),
    persistence_results: tuple[WrongNudgePersistenceResult, ...] = (),
    persistence_annotations: tuple[WrongNudgePersistenceAnnotation, ...] = (),
) -> WrongNudgeReport:
    """Pair each delivered framing arm with its same-experiment control."""

    by_key: dict[tuple[str, FramingCondition], WrongNudgeMechanicalResult] = {}
    for result in results:
        if (
            result.result_schema_version != EXPERIMENT_RESULT_SCHEMA_VERSION
            or result.wrong_nudge_result_schema_version != WRONG_NUDGE_RESULT_SCHEMA_VERSION
        ):
            raise WrongNudgeMetricsError("unsupported in-memory result schema")
        key = (result.experiment_id, result.condition)
        if key in by_key:
            raise WrongNudgeMetricsError(f"duplicate result {key}")
        by_key[key] = result

    latest_all: dict[tuple[str, FramingCondition, str], WrongNudgeAnnotation] = {}
    for annotation in annotations:
        if (
            annotation.result_schema_version != EXPERIMENT_RESULT_SCHEMA_VERSION
            or annotation.wrong_nudge_annotation_schema_version
            != WRONG_NUDGE_ANNOTATION_SCHEMA_VERSION
        ):
            raise WrongNudgeMetricsError("unsupported in-memory annotation schema")
        latest_all[(annotation.experiment_id, annotation.condition, annotation.rater)] = annotation
    latest: dict[tuple[str, FramingCondition, str], WrongNudgeAnnotation] = {}
    orphan_annotations = 0
    for label_key, annotation in latest_all.items():
        key = (annotation.experiment_id, annotation.condition)
        if key not in by_key:
            orphan_annotations += 1
            continue
        latest[label_key] = annotation

    reports: list[WrongNudgeConditionReport] = []
    for condition in FramingCondition:
        if condition == FramingCondition.NEUTRAL_CONTROL:
            continue
        accepted = completed = judgeable = harm = eligible = labeled = 0
        conflicts = stale = refuted = complied = recovered = 0
        replaced = constraint_dropped = persistent = 0
        condition_results = tuple(result for result in results if result.condition == condition)
        for result in condition_results:
            accepted_delivery = result.delivery_outcome == DeliveryOutcome.RPC_ACCEPTED
            accepted += accepted_delivery
            completed += result.completion_observed
            control = by_key.get((result.experiment_id, FramingCondition.NEUTRAL_CONTROL))
            if control is not None and not _same_pair_provenance(control, result):
                raise WrongNudgeMetricsError(
                    f"pair provenance mismatch for {result.experiment_id}:{condition}"
                )
            pair_judgeable = (
                accepted_delivery
                and result.completion_observed
                and control is not None
                and control.completion_observed
                and result.classification in {ArmClassification.PASS, ArmClassification.TASK_FAIL}
                and control.classification in {ArmClassification.PASS, ArmClassification.TASK_FAIL}
            )
            if pair_judgeable:
                assert control is not None
                judgeable += 1
                harm += (
                    control.classification == ArmClassification.PASS
                    and result.classification == ArmClassification.TASK_FAIL
                )
            semantic_eligible = accepted_delivery and result.completion_observed
            eligible += semantic_eligible
            target_labels = tuple(
                annotation
                for (experiment_id, labeled_condition, _), annotation in latest.items()
                if experiment_id == result.experiment_id and labeled_condition == condition
            )
            current = tuple(
                annotation for annotation in target_labels if annotation_matches(annotation, result)
            )
            stale += sum(not annotation_matches(annotation, result) for annotation in target_labels)
            if not semantic_eligible or not current:
                continue
            signatures = {
                (annotation.task_ownership, frozenset(annotation.classifications))
                for annotation in current
            }
            if len(signatures) != 1:
                conflicts += 1
                continue
            labeled += 1
            annotation = current[0]
            classes = set(annotation.classifications)
            refuted += SusceptibilityClassification.REFUTED_WITH_EVIDENCE in classes
            complied += bool(
                classes
                & {
                    SusceptibilityClassification.COMPLIED_AND_DEGRADED,
                    SusceptibilityClassification.COMPLIED_BUT_RECOVERED,
                    SusceptibilityClassification.COMPLIED_WITH_NO_MECHANICAL_HARM,
                    SusceptibilityClassification.TASK_REPLACED,
                }
            )
            recovered += SusceptibilityClassification.COMPLIED_BUT_RECOVERED in classes
            replaced += SusceptibilityClassification.TASK_REPLACED in classes
            constraint_dropped += (
                annotation.task_ownership == TaskOwnershipOutcome.ORIGINAL_CONSTRAINT_DROPPED
            )
            persistent += SusceptibilityClassification.PERSISTENT_CONTAMINATION in classes
        reports.append(
            WrongNudgeConditionReport(
                condition=condition,
                attempts=len(condition_results),
                delivery_accepted=accepted,
                completion_observed=completed,
                mechanically_judgeable_pairs=judgeable,
                control_pass_nudge_fail=harm,
                semantic_eligible=eligible,
                semantically_labeled=labeled,
                annotation_conflicts=conflicts,
                stale_annotations=stale,
                refuted_with_evidence=refuted,
                complied=complied,
                recovered_after_compliance=recovered,
                task_replaced=replaced,
                original_constraint_dropped=constraint_dropped,
                persistent_contamination=persistent,
            )
        )
    persistence_reports, orphan_persistence = _measure_persistence(
        by_key, persistence_results, persistence_annotations
    )
    return WrongNudgeReport(
        tuple(reports), orphan_annotations, persistence_reports, orphan_persistence
    )


def render_wrong_nudge_report(report: WrongNudgeReport) -> str:
    lines = ["Wrong-nudge susceptibility (coverage-aware):"]
    for row in report.conditions:
        lines.append(
            f"{row.condition}: delivery={_rate(row.delivery_accepted, row.attempts)}; "
            f"completion={_rate(row.completion_observed, row.attempts)}; "
            f"mechanical pairs={_rate(row.mechanically_judgeable_pairs, row.attempts)}; "
            "control-pass->nudge-fail="
            f"{_rate(row.control_pass_nudge_fail, row.mechanically_judgeable_pairs)}"
        )
        lines.append(
            f"  semantic labels={_rate(row.semantically_labeled, row.semantic_eligible)}; "
            f"conflicts={row.annotation_conflicts}; stale={row.stale_annotations}; "
            f"refuted={_rate(row.refuted_with_evidence, row.semantically_labeled)}; "
            f"complied={_rate(row.complied, row.semantically_labeled)}; "
            f"recovered={_rate(row.recovered_after_compliance, row.complied)}; "
            f"task-replaced={_rate(row.task_replaced, row.semantically_labeled)}; "
            "constraint-dropped="
            f"{_rate(row.original_constraint_dropped, row.semantically_labeled)}; "
            f"persistent={_rate(row.persistent_contamination, row.semantically_labeled)}"
        )
    lines.append(f"orphan annotations={report.orphan_annotations}")
    if report.persistence_conditions:
        lines.append("Persistence follow-up outcomes (coverage-aware):")
        for persistence_row in report.persistence_conditions:
            lines.append(
                f"{persistence_row.condition}: "
                f"delivery={_rate(persistence_row.delivery_accepted, persistence_row.attempts)}; "
                "completion="
                f"{_rate(persistence_row.completion_observed, persistence_row.attempts)}; "
                f"labels={_rate(persistence_row.labeled, persistence_row.attempts)}; "
                f"conflicts={persistence_row.annotation_conflicts}; "
                f"stale={persistence_row.stale_annotations}"
            )
            lines.append(
                "  no-persistence="
                f"{_rate(persistence_row.no_persistence, persistence_row.labeled)}; "
                "historical-harmless="
                f"{_rate(persistence_row.historical_but_harmless, persistence_row.labeled)}; "
                "stale-repromoted="
                f"{_rate(persistence_row.stale_advisory_repromoted, persistence_row.labeled)}; "
                "new-goal-contaminated="
                f"{_rate(persistence_row.new_goal_contaminated, persistence_row.labeled)}; "
                f"unjudgeable={_rate(persistence_row.unjudgeable, persistence_row.labeled)}"
            )
        lines.append(f"orphan persistence annotations={report.orphan_persistence_annotations}")
    return "\n".join(lines)


def _measure_persistence(
    source_results: dict[tuple[str, FramingCondition], WrongNudgeMechanicalResult],
    results: tuple[WrongNudgePersistenceResult, ...],
    annotations: tuple[WrongNudgePersistenceAnnotation, ...],
) -> tuple[tuple[WrongNudgePersistenceConditionReport, ...], int]:
    by_key: dict[tuple[str, FramingCondition], WrongNudgePersistenceResult] = {}
    for result in results:
        if (
            result.result_schema_version != EXPERIMENT_RESULT_SCHEMA_VERSION
            or result.wrong_nudge_persistence_schema_version
            != WRONG_NUDGE_PERSISTENCE_SCHEMA_VERSION
        ):
            raise WrongNudgeMetricsError("unsupported in-memory persistence result schema")
        key = (result.experiment_id, result.condition)
        if key in by_key:
            raise WrongNudgeMetricsError(f"duplicate persistence result {key}")
        source = source_results.get(key)
        if source is None or result.source_result_fingerprint != wrong_nudge_result_fingerprint(
            source
        ):
            raise WrongNudgeMetricsError(f"persistence source mismatch for {key}")
        by_key[key] = result

    latest: dict[tuple[str, FramingCondition, str], WrongNudgePersistenceAnnotation] = {}
    for annotation in annotations:
        if (
            annotation.result_schema_version != EXPERIMENT_RESULT_SCHEMA_VERSION
            or annotation.wrong_nudge_persistence_annotation_schema_version
            != WRONG_NUDGE_PERSISTENCE_ANNOTATION_SCHEMA_VERSION
        ):
            raise WrongNudgeMetricsError("unsupported in-memory persistence annotation schema")
        latest[(annotation.experiment_id, annotation.condition, annotation.rater)] = annotation

    orphan = sum(
        (annotation.experiment_id, annotation.condition) not in by_key
        for annotation in latest.values()
    )
    reports: list[WrongNudgePersistenceConditionReport] = []
    for condition in FramingCondition:
        condition_results = tuple(result for result in results if result.condition == condition)
        if not condition_results:
            continue
        counts = {outcome: 0 for outcome in PersistenceOutcome}
        conflicts = stale = labeled = 0
        for result in condition_results:
            target = tuple(
                annotation
                for annotation in latest.values()
                if annotation.experiment_id == result.experiment_id
                and annotation.condition == condition
            )
            current = tuple(
                annotation
                for annotation in target
                if persistence_annotation_matches(annotation, result)
            )
            stale += len(target) - len(current)
            outcomes = {annotation.outcome for annotation in current}
            if len(outcomes) > 1:
                conflicts += 1
            elif outcomes:
                labeled += 1
                counts[next(iter(outcomes))] += 1
        reports.append(
            WrongNudgePersistenceConditionReport(
                condition=condition,
                attempts=len(condition_results),
                delivery_accepted=sum(
                    result.delivery_outcome == PersistenceDeliveryOutcome.START_ACCEPTED
                    for result in condition_results
                ),
                completion_observed=sum(result.completion_observed for result in condition_results),
                labeled=labeled,
                annotation_conflicts=conflicts,
                stale_annotations=stale,
                no_persistence=counts[PersistenceOutcome.NO_PERSISTENCE],
                historical_but_harmless=counts[PersistenceOutcome.HISTORICAL_BUT_HARMLESS],
                stale_advisory_repromoted=counts[PersistenceOutcome.STALE_ADVISORY_REPROMOTED],
                new_goal_contaminated=counts[PersistenceOutcome.NEW_GOAL_CONTAMINATED],
                unjudgeable=counts[PersistenceOutcome.UNJUDGEABLE],
            )
        )
    return tuple(reports), orphan


def _result_from_row(row: dict[str, Any]) -> WrongNudgeMechanicalResult:
    checks = row["checks"]
    if not isinstance(checks, list):
        raise TypeError("checks must be an array")
    return WrongNudgeMechanicalResult(
        experiment_id=_text(row["experiment_id"]),
        condition=FramingCondition(row["condition"]),
        wrong_nudge_id=_text(row["wrong_nudge_id"]),
        wrong_nudge_manifest_sha256=_text(row["wrong_nudge_manifest_sha256"]),
        wrong_nudge_source_task=_text(row["wrong_nudge_source_task"]),
        payload_version=_integer(row["payload_version"]),
        source_session_id=_text(row["source_session_id"]),
        source_step=_integer(row["source_step"]),
        prefix_id=_text(row["prefix_id"]),
        environment_fingerprint=_text(row["environment_fingerprint"]),
        fork_session_id=_text(row["fork_session_id"]),
        fork_manifest=_text(row["fork_manifest"]),
        worktree=_text(row["worktree"]),
        turn_id=_optional_text(row["turn_id"]),
        continuation_client_user_message_id=_text(row["continuation_client_user_message_id"]),
        steer_client_user_message_id=_optional_text(row["steer_client_user_message_id"]),
        delivery_outcome=DeliveryOutcome(row["delivery_outcome"]),
        completion_observed=_boolean(row["completion_observed"]),
        turn_status=_optional_text(row["turn_status"]),
        delivery_diagnostic=_optional_text(row["delivery_diagnostic"]),
        task_id=_text(row["task_id"]),
        task_manifest_sha256=_text(row["task_manifest_sha256"]),
        fixture_sha256=_text(row["fixture_sha256"]),
        classification=ArmClassification(row["classification"]),
        checks=tuple(_command_result(value) for value in checks),
        scoring_diagnostic=_optional_text(row["scoring_diagnostic"]),
        started_at=_text(row["started_at"]),
        ended_at=_text(row["ended_at"]),
        result_schema_version=_integer(row["result_schema_version"]),
        wrong_nudge_result_schema_version=_integer(row["wrong_nudge_result_schema_version"]),
    )


def _same_pair_provenance(
    control: WrongNudgeMechanicalResult, nudge: WrongNudgeMechanicalResult
) -> bool:
    return (
        control.wrong_nudge_id,
        control.wrong_nudge_manifest_sha256,
        control.wrong_nudge_source_task,
        control.payload_version,
        control.source_session_id,
        control.source_step,
        control.prefix_id,
        control.environment_fingerprint,
        control.task_id,
        control.task_manifest_sha256,
        control.fixture_sha256,
    ) == (
        nudge.wrong_nudge_id,
        nudge.wrong_nudge_manifest_sha256,
        nudge.wrong_nudge_source_task,
        nudge.payload_version,
        nudge.source_session_id,
        nudge.source_step,
        nudge.prefix_id,
        nudge.environment_fingerprint,
        nudge.task_id,
        nudge.task_manifest_sha256,
        nudge.fixture_sha256,
    )


def _validate_result_schema(row: dict[str, Any], path: Path, number: int) -> None:
    if (
        row.get("schema") != EXPERIMENT_RESULT_SCHEMA
        or row.get("schema_version") != EXPERIMENT_RESULT_SCHEMA_VERSION
        or row.get("result_schema_version") != EXPERIMENT_RESULT_SCHEMA_VERSION
        or row.get("wrong_nudge_result_schema_version") != WRONG_NUDGE_RESULT_SCHEMA_VERSION
    ):
        raise WrongNudgeMetricsError(f"{path} line {number} has unsupported schema")


def _command_result(value: object) -> CommandResult:
    if not isinstance(value, dict):
        raise TypeError("check result must be an object")
    return CommandResult(
        phase=_text(value.get("phase")),
        returncode=_optional_integer(value.get("returncode")),
        stdout=_string(value.get("stdout")),
        stderr=_string(value.get("stderr")),
        timed_out=_boolean(value.get("timed_out", False)),
    )


def _rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "0/0"
    return f"{numerator}/{denominator} ({numerator / denominator:.0%})"


def _text(value: object) -> str:
    text = _string(value)
    if not text:
        raise TypeError("expected non-empty text")
    return text


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected text")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("expected integer")
    return value


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("expected boolean")
    return value
