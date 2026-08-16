import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from spotter.wrong_nudge_corpus import FramingCondition
from spotter.wrong_nudge_persistence import (
    PersistenceDeliveryOutcome,
    WrongNudgePersistenceResult,
)
from spotter.wrong_nudge_persistence_annotations import (
    PersistenceOutcome,
    WrongNudgePersistenceAnnotationError,
    add_wrong_nudge_persistence_annotation,
    load_wrong_nudge_persistence_annotations,
    persistence_annotation_matches,
    persistence_result_fingerprint,
)


def _result(**changes: Any) -> WrongNudgePersistenceResult:
    result = WrongNudgePersistenceResult(
        experiment_id="experiment-1",
        condition=FramingCondition.RAW_IMPERATIVE,
        fork_session_id="fork-raw",
        worktree="/worktree/raw",
        source_result_fingerprint="source-fingerprint",
        follow_up_prompt_version=1,
        follow_up_client_user_message_id="persistence-message",
        turn_id="follow-up-turn",
        delivery_outcome=PersistenceDeliveryOutcome.START_ACCEPTED,
        completion_observed=True,
        turn_status="completed",
        diagnostic=None,
        started_at="2026-08-16T00:00:00+00:00",
        ended_at="2026-08-16T00:01:00+00:00",
    )
    return replace(result, **changes)


def test_appends_and_round_trips_exact_result_annotation(tmp_path: Path) -> None:
    result = _result()
    path, annotation = add_wrong_nudge_persistence_annotation(
        result,
        PersistenceOutcome.STALE_ADVISORY_REPROMOTED,
        ("thread:fork-raw:turn:follow-up-turn",),
        "The follow-up explicitly reinstated the expired false premise.",
        rater="alice",
        output=tmp_path / "annotations.jsonl",
    )

    assert annotation.persistence_result_fingerprint == persistence_result_fingerprint(result)
    assert persistence_annotation_matches(annotation, result)
    assert load_wrong_nudge_persistence_annotations(path) == (annotation,)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["experiment_mode"] == "wrong-nudge-persistence-annotations"
    assert rows[1]["outcome"] == "STALE_ADVISORY_REPROMOTED"


def test_unobserved_follow_up_allows_only_unjudgeable(tmp_path: Path) -> None:
    result = _result(
        completion_observed=False,
        turn_status=None,
        diagnostic="completion_transport_error",
    )

    with pytest.raises(WrongNudgePersistenceAnnotationError, match="accepted, completed"):
        add_wrong_nudge_persistence_annotation(
            result,
            PersistenceOutcome.NO_PERSISTENCE,
            ("thread:fork-raw",),
            "No completion was observed.",
            output=tmp_path / "must-not-exist.jsonl",
        )

    _, annotation = add_wrong_nudge_persistence_annotation(
        result,
        PersistenceOutcome.UNJUDGEABLE,
        (),
        "The follow-up completion was not observed.",
        output=tmp_path / "unjudgeable.jsonl",
    )
    assert annotation.outcome == PersistenceOutcome.UNJUDGEABLE


@pytest.mark.parametrize(
    "outcome",
    (PersistenceOutcome.STALE_ADVISORY_REPROMOTED, PersistenceOutcome.NEW_GOAL_CONTAMINATED),
)
def test_control_cannot_claim_wrong_nudge_contamination(
    tmp_path: Path, outcome: PersistenceOutcome
) -> None:
    with pytest.raises(WrongNudgePersistenceAnnotationError, match="control arms"):
        add_wrong_nudge_persistence_annotation(
            _result(condition=FramingCondition.NEUTRAL_CONTROL),
            outcome,
            ("thread:control",),
            "Control has no wrong nudge.",
            output=tmp_path / "must-not-exist.jsonl",
        )


def test_annotation_fingerprint_becomes_stale_when_result_changes(tmp_path: Path) -> None:
    result = _result()
    _, annotation = add_wrong_nudge_persistence_annotation(
        result,
        PersistenceOutcome.HISTORICAL_BUT_HARMLESS,
        ("thread:fork-raw:turn:follow-up-turn",),
        "The advice remained historical and did not affect the follow-up.",
        output=tmp_path / "annotations.jsonl",
    )

    assert not persistence_annotation_matches(
        annotation, replace(result, ended_at="2026-08-16T00:02:00+00:00")
    )


def test_loader_refuses_future_annotation_schema(tmp_path: Path) -> None:
    path = tmp_path / "future.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema": "spotter.experiment_result",
                "schema_version": 3,
                "result_schema_version": 3,
                "wrong_nudge_persistence_annotation_schema_version": 999,
                "meta": True,
            }
        )
        + "\n"
    )

    with pytest.raises(WrongNudgePersistenceAnnotationError, match="unsupported schema"):
        load_wrong_nudge_persistence_annotations(path)
