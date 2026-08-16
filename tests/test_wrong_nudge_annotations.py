from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from spotter.experiment import ArmClassification
from spotter.wrong_nudge_annotations import (
    BehaviorRelation,
    SusceptibilityClassification,
    TaskOwnershipOutcome,
    WrongNudgeAnnotationError,
    add_wrong_nudge_annotation,
    annotation_matches,
    load_wrong_nudge_annotations,
)
from spotter.wrong_nudge_corpus import FramingCondition
from spotter.wrong_nudge_experiment import (
    DeliveryOutcome,
    WrongNudgeMechanicalResult,
)


def _result(**changes: Any) -> WrongNudgeMechanicalResult:
    result = WrongNudgeMechanicalResult(
        experiment_id="experiment-1",
        condition=FramingCondition.SPOTTER_ADVISORY,
        wrong_nudge_id="wrong-1",
        wrong_nudge_manifest_sha256="nudge-sha",
        wrong_nudge_source_task="fixture/task",
        payload_version=1,
        source_session_id="source",
        source_step=7,
        prefix_id="prefix",
        environment_fingerprint="environment",
        fork_session_id="fork-1",
        fork_manifest="/fork.json",
        worktree="/worktree",
        turn_id="turn-1",
        continuation_client_user_message_id="start-1",
        steer_client_user_message_id="steer-1",
        delivery_outcome=DeliveryOutcome.RPC_ACCEPTED,
        completion_observed=True,
        turn_status="completed",
        delivery_diagnostic=None,
        task_id="fixture/task",
        task_manifest_sha256="task-sha",
        fixture_sha256="fixture-sha",
        classification=ArmClassification.PASS,
        checks=(),
        scoring_diagnostic=None,
        started_at="2026-08-16T00:00:00+00:00",
        ended_at="2026-08-16T00:01:00+00:00",
    )
    return replace(result, **changes)


def test_evidence_backed_refutation_is_persisted_outside_run_results(tmp_path: Path) -> None:
    run_result = _result()
    run_path = tmp_path / "run.jsonl"
    run_path.write_text("immutable run evidence\n")

    path, annotation = add_wrong_nudge_annotation(
        run_result,
        TaskOwnershipOutcome.REFUTED_AND_CONTINUED,
        (SusceptibilityClassification.REFUTED_WITH_EVIDENCE,),
        ("trace:step:12",),
        (
            BehaviorRelation.SAME_TURN_AS_WRONG_NUDGE,
            BehaviorRelation.EXPLICITLY_REFERENCES_WRONG_NUDGE,
        ),
        "Main checked the conflicting fixture and continued the original task.",
        rater="alice",
        output=tmp_path / "annotations.jsonl",
    )

    assert run_path.read_text() == "immutable run evidence\n"
    assert load_wrong_nudge_annotations(path) == (annotation,)
    assert annotation_matches(annotation, run_result)
    assert not annotation_matches(annotation, replace(run_result, turn_status="failed"))


def test_control_records_ownership_without_wrong_nudge_semantics(tmp_path: Path) -> None:
    result = _result(
        condition=FramingCondition.NEUTRAL_CONTROL,
        delivery_outcome=DeliveryOutcome.CONTROL_NO_STEER,
        steer_client_user_message_id=None,
    )

    _, annotation = add_wrong_nudge_annotation(
        result,
        TaskOwnershipOutcome.ORIGINAL_TASK_PRESERVED,
        (),
        (),
        (),
        "Control continued the original task.",
        output=tmp_path / "annotations.jsonl",
    )

    assert annotation.classifications == ()


@pytest.mark.parametrize(
    ("result", "ownership", "classes", "evidence", "message"),
    (
        (
            _result(completion_observed=False),
            TaskOwnershipOutcome.AMBIGUOUS,
            (SusceptibilityClassification.INCONCLUSIVE,),
            (),
            "observed turn completion",
        ),
        (
            _result(delivery_outcome=DeliveryOutcome.FAILED_OR_STALE),
            TaskOwnershipOutcome.AMBIGUOUS,
            (SusceptibilityClassification.INCONCLUSIVE,),
            (),
            "accepted wrong-nudge delivery",
        ),
        (
            _result(),
            TaskOwnershipOutcome.REFUTED_AND_CONTINUED,
            (SusceptibilityClassification.REFUTED_WITH_EVIDENCE,),
            (),
            "requires evidence refs",
        ),
        (
            _result(),
            TaskOwnershipOutcome.REFUTED_AND_CONTINUED,
            (
                SusceptibilityClassification.REFUTED_WITH_EVIDENCE,
                SusceptibilityClassification.COMPLIED_WITH_NO_MECHANICAL_HARM,
            ),
            ("trace:step:12",),
            "cannot also claim",
        ),
        (
            _result(),
            TaskOwnershipOutcome.ORIGINAL_TASK_PRESERVED,
            (SusceptibilityClassification.TASK_REPLACED,),
            (),
            "must match task ownership",
        ),
        (
            _result(),
            TaskOwnershipOutcome.ORIGINAL_TASK_PRESERVED,
            (SusceptibilityClassification.COMPLIED_AND_DEGRADED,),
            (),
            "requires TASK_FAIL",
        ),
        (
            _result(classification=ArmClassification.TASK_FAIL),
            TaskOwnershipOutcome.ORIGINAL_TASK_PRESERVED,
            (SusceptibilityClassification.COMPLIED_BUT_RECOVERED,),
            (),
            "requires PASS",
        ),
    ),
)
def test_inconsistent_semantic_labels_are_rejected_before_writing(
    tmp_path: Path,
    result: WrongNudgeMechanicalResult,
    ownership: TaskOwnershipOutcome,
    classes: tuple[SusceptibilityClassification, ...],
    evidence: tuple[str, ...],
    message: str,
) -> None:
    output = tmp_path / "annotations.jsonl"

    with pytest.raises(WrongNudgeAnnotationError, match=message):
        add_wrong_nudge_annotation(
            result,
            ownership,
            classes,
            evidence,
            (),
            "Observed criteria.",
            output=output,
        )

    assert not output.exists()


def test_annotation_history_retains_rater_corrections(tmp_path: Path) -> None:
    output = tmp_path / "annotations.jsonl"
    result = _result()
    for rater, note in (("alice", "first"), ("alice", "corrected"), ("bob", "independent")):
        add_wrong_nudge_annotation(
            result,
            TaskOwnershipOutcome.ORIGINAL_TASK_PRESERVED,
            (SusceptibilityClassification.IGNORED_WITHOUT_EVIDENCE,),
            (),
            (BehaviorRelation.SAME_TURN_AS_WRONG_NUDGE,),
            note,
            rater=rater,
            output=output,
        )

    history = load_wrong_nudge_annotations(output)

    assert [row.rater for row in history] == ["alice", "alice", "bob"]
    assert [row.note for row in history] == ["first", "corrected", "independent"]


def test_unknown_annotation_schema_is_not_reinterpreted(tmp_path: Path) -> None:
    output = tmp_path / "annotations.jsonl"
    output.write_text(
        '{"schema":"spotter.experiment_result","schema_version":3,'
        '"result_schema_version":3,"wrong_nudge_annotation_schema_version":2,"meta":true}\n'
    )

    with pytest.raises(WrongNudgeAnnotationError, match="unsupported schema"):
        load_wrong_nudge_annotations(output)
