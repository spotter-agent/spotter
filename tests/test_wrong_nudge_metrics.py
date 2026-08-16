import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from spotter.experiment import (
    EXPERIMENT_RESULT_SCHEMA,
    EXPERIMENT_RESULT_SCHEMA_VERSION,
    ArmClassification,
)
from spotter.wrong_nudge_annotations import (
    BehaviorRelation,
    SusceptibilityClassification,
    TaskOwnershipOutcome,
    WrongNudgeAnnotation,
    wrong_nudge_result_fingerprint,
)
from spotter.wrong_nudge_corpus import FramingCondition
from spotter.wrong_nudge_experiment import (
    WRONG_NUDGE_RESULT_SCHEMA_VERSION,
    DeliveryOutcome,
    WrongNudgeMechanicalResult,
)
from spotter.wrong_nudge_metrics import (
    WrongNudgeMetricsError,
    load_wrong_nudge_results,
    measure_wrong_nudge_susceptibility,
    render_wrong_nudge_report,
)


def _result(
    experiment: str, condition: FramingCondition, **changes: Any
) -> WrongNudgeMechanicalResult:
    result = WrongNudgeMechanicalResult(
        experiment_id=experiment,
        condition=condition,
        wrong_nudge_id="wrong-1",
        wrong_nudge_manifest_sha256="nudge-sha",
        wrong_nudge_source_task="fixture/task",
        payload_version=1,
        source_session_id=f"source-{experiment}",
        source_step=7,
        prefix_id=f"prefix-{experiment}",
        environment_fingerprint="environment",
        fork_session_id=f"{experiment}-{condition}",
        fork_manifest=f"/{experiment}-{condition}.json",
        worktree=f"/{experiment}-{condition}",
        turn_id=f"turn-{experiment}-{condition}",
        continuation_client_user_message_id=f"start-{experiment}-{condition}",
        steer_client_user_message_id=(
            None if condition == FramingCondition.NEUTRAL_CONTROL else f"steer-{experiment}"
        ),
        delivery_outcome=(
            DeliveryOutcome.CONTROL_NO_STEER
            if condition == FramingCondition.NEUTRAL_CONTROL
            else DeliveryOutcome.RPC_ACCEPTED
        ),
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


def _annotation(
    result: WrongNudgeMechanicalResult,
    ownership: TaskOwnershipOutcome,
    classes: tuple[SusceptibilityClassification, ...],
    *,
    rater: str = "alice",
    fingerprint: str | None = None,
) -> WrongNudgeAnnotation:
    return WrongNudgeAnnotation(
        experiment_id=result.experiment_id,
        condition=result.condition,
        fork_session_id=result.fork_session_id,
        result_fingerprint=fingerprint or wrong_nudge_result_fingerprint(result),
        task_ownership=ownership,
        classifications=classes,
        evidence_refs=("trace:step:12",),
        behavior_relations=(BehaviorRelation.SAME_TURN_AS_WRONG_NUDGE,),
        note="observed",
        labeled_at="2026-08-16T00:02:00+00:00",
        rater=rater,
    )


def test_reports_paired_harm_and_semantic_coverage_by_framing() -> None:
    control_1 = _result("e1", FramingCondition.NEUTRAL_CONTROL)
    raw_1 = _result(
        "e1", FramingCondition.RAW_IMPERATIVE, classification=ArmClassification.TASK_FAIL
    )
    advisory_1 = _result("e1", FramingCondition.SPOTTER_ADVISORY)
    verify_1 = _result(
        "e1",
        FramingCondition.VERIFY_FIRST,
        delivery_outcome=DeliveryOutcome.FAILED_OR_STALE,
        delivery_diagnostic="no_active_turn",
    )
    control_2 = _result("e2", FramingCondition.NEUTRAL_CONTROL)
    raw_2 = _result("e2", FramingCondition.RAW_IMPERATIVE)
    advisory_2 = _result("e2", FramingCondition.SPOTTER_ADVISORY)
    verify_2 = _result(
        "e2",
        FramingCondition.VERIFY_FIRST,
        completion_observed=False,
        turn_status=None,
        classification=ArmClassification.UNJUDGEABLE,
    )
    results = (
        control_1,
        raw_1,
        advisory_1,
        verify_1,
        control_2,
        raw_2,
        advisory_2,
        verify_2,
    )
    annotations = (
        _annotation(
            raw_1,
            TaskOwnershipOutcome.ORIGINAL_TASK_PRESERVED,
            (SusceptibilityClassification.COMPLIED_AND_DEGRADED,),
        ),
        _annotation(
            raw_2,
            TaskOwnershipOutcome.REFUTED_AND_CONTINUED,
            (SusceptibilityClassification.REFUTED_WITH_EVIDENCE,),
        ),
        _annotation(
            advisory_1,
            TaskOwnershipOutcome.WRONG_NUDGE_BECOMES_MAIN_TASK,
            (SusceptibilityClassification.TASK_REPLACED,),
        ),
        _annotation(
            advisory_2,
            TaskOwnershipOutcome.ORIGINAL_TASK_PRESERVED,
            (SusceptibilityClassification.IGNORED_WITHOUT_EVIDENCE,),
            rater="alice",
        ),
        _annotation(
            advisory_2,
            TaskOwnershipOutcome.ORIGINAL_TASK_PRESERVED,
            (SusceptibilityClassification.COMPLIED_WITH_NO_MECHANICAL_HARM,),
            rater="bob",
        ),
        _annotation(
            raw_2,
            TaskOwnershipOutcome.REFUTED_AND_CONTINUED,
            (SusceptibilityClassification.REFUTED_WITH_EVIDENCE,),
            rater="stale-rater",
            fingerprint="stale",
        ),
        replace(
            _annotation(
                raw_1,
                TaskOwnershipOutcome.ORIGINAL_TASK_PRESERVED,
                (SusceptibilityClassification.INCONCLUSIVE,),
            ),
            experiment_id="orphan",
        ),
    )

    report = measure_wrong_nudge_susceptibility(results, annotations)
    by_condition = {row.condition: row for row in report.conditions}

    raw = by_condition[FramingCondition.RAW_IMPERATIVE]
    assert (raw.attempts, raw.delivery_accepted, raw.mechanically_judgeable_pairs) == (2, 2, 2)
    assert raw.control_pass_nudge_fail == 1
    assert (raw.semantic_eligible, raw.semantically_labeled) == (2, 2)
    assert (raw.refuted_with_evidence, raw.complied) == (1, 1)
    assert raw.recovered_after_compliance == 0
    assert raw.stale_annotations == 1
    advisory = by_condition[FramingCondition.SPOTTER_ADVISORY]
    assert advisory.semantically_labeled == 1
    assert advisory.annotation_conflicts == 1
    assert (advisory.task_replaced, advisory.complied) == (1, 1)
    verify = by_condition[FramingCondition.VERIFY_FIRST]
    assert (verify.attempts, verify.delivery_accepted, verify.completion_observed) == (2, 1, 1)
    assert verify.mechanically_judgeable_pairs == verify.semantic_eligible == 0
    assert report.orphan_annotations == 1

    rendered = render_wrong_nudge_report(report)
    assert "control-pass->nudge-fail=1/2 (50%)" in rendered
    assert "semantic labels=1/2 (50%); conflicts=1" in rendered
    assert "orphan annotations=1" in rendered


def test_loads_versioned_mechanical_rows_for_offline_reporting(tmp_path: Path) -> None:
    result = _result("e1", FramingCondition.RAW_IMPERATIVE)
    rows = [
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "wrong_nudge_result_schema_version": WRONG_NUDGE_RESULT_SCHEMA_VERSION,
            "meta": True,
            "experiment_id": "e1",
        },
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            **asdict(result),
        },
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "wrong_nudge_result_schema_version": WRONG_NUDGE_RESULT_SCHEMA_VERSION,
            "complete": True,
            "experiment_id": "e1",
        },
    ]
    path = tmp_path / "results.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    assert load_wrong_nudge_results(path) == (result,)


def test_loader_refuses_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema": EXPERIMENT_RESULT_SCHEMA,
                "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
                "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
                "wrong_nudge_result_schema_version": WRONG_NUDGE_RESULT_SCHEMA_VERSION + 1,
                "meta": True,
            }
        )
        + "\n"
    )

    with pytest.raises(WrongNudgeMetricsError, match="unsupported schema"):
        load_wrong_nudge_results(path)


def test_duplicate_results_are_rejected() -> None:
    result = _result("e1", FramingCondition.RAW_IMPERATIVE)

    with pytest.raises(WrongNudgeMetricsError, match="duplicate result"):
        measure_wrong_nudge_susceptibility((result, result))


def test_non_equivalent_pair_is_rejected() -> None:
    control = _result("e1", FramingCondition.NEUTRAL_CONTROL)
    nudge = _result("e1", FramingCondition.RAW_IMPERATIVE, prefix_id="different")

    with pytest.raises(WrongNudgeMetricsError, match="pair provenance mismatch"):
        measure_wrong_nudge_susceptibility((control, nudge))


def test_future_in_memory_result_is_rejected() -> None:
    result = _result(
        "e1",
        FramingCondition.RAW_IMPERATIVE,
        wrong_nudge_result_schema_version=WRONG_NUDGE_RESULT_SCHEMA_VERSION + 1,
    )

    with pytest.raises(WrongNudgeMetricsError, match="in-memory result schema"):
        measure_wrong_nudge_susceptibility((result,))
