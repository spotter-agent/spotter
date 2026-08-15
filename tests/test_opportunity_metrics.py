from pathlib import Path

import pytest

from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.opportunities import add_opportunity
from spotter.opportunity_metrics import (
    measure_opportunity_timing,
    merge_opportunity_timing,
    render_opportunity_timing,
)
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))


def _record(
    step: int,
    kind: str,
    payload: dict[str, object],
    *,
    event_id: str,
    operation_id: str | None = None,
    occurred_at: float | None = None,
    epoch: int | None = 1,
    identity: RuntimeIdentity | None = None,
) -> StepRecord:
    return StepRecord(
        step,
        TraceEvent(
            kind,
            payload,
            event_id=event_id,
            operation_id=operation_id,
            occurred_at=occurred_at,
            identity=identity,
            connection_epoch=epoch,
        ),
        None,
        occurred_at,
    )


def _records() -> list[StepRecord]:
    return [
        _record(0, "tool_result", {}, event_id="semantic", occurred_at=1.0),
        _record(1, "tool_result", {}, event_id="observable", occurred_at=2.0),
        _record(
            2,
            "signal_candidate",
            {"status": "active", "evidence_event_ids": ["unrelated"]},
            event_id="unrelated-signal",
            occurred_at=3.0,
        ),
        _record(
            3,
            "tool_proposal",
            {"files": ["src/a.py"]},
            event_id="action-1",
            operation_id="op-1",
            occurred_at=4.0,
        ),
        _record(
            4,
            "tool_result",
            {"status": "failed"},
            event_id="outcome-1",
            operation_id="op-1",
            occurred_at=5.0,
        ),
        _record(
            5,
            "file_change_started",
            {"files": ["src/b.py"]},
            event_id="action-2",
            operation_id="op-2",
            occurred_at=6.0,
        ),
        _record(
            6,
            "signal_candidate",
            {
                "status": "active",
                "evidence_event_ids": ["semantic", "observable", "extra"],
            },
            event_id="linked-signal",
            occurred_at=7.0,
        ),
        _record(7, "tool_result", {}, event_id="never-evidence", occurred_at=8.0),
        _record(8, "turn_completed", {}, event_id="terminal", occurred_at=9.0),
    ]


def _identity(turn: str = "turn-1") -> RuntimeIdentity:
    return RuntimeIdentity(
        ThreadId("thread-1"),
        TurnId(turn),
        None,
        IdentityProvenance("codex"),
    )


def test_signal_delay_requires_all_annotated_evidence_and_counts_post_window_work() -> None:
    records = _records()
    add_opportunity(
        "s1",
        "late-loop",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=4,
        required_evidence=(0, 1),
        note="both observations are required",
        rater="alice",
    )
    add_opportunity(
        "s1",
        "never-fired",
        records,
        semantic_earliest=7,
        semantic_latest=7,
        observable_earliest=7,
        observable_latest=7,
        required_evidence=(7,),
        note="no candidate cites this evidence",
        rater="alice",
    )

    report = measure_opportunity_timing("s1", records)

    assert report.annotations == report.current_annotations == 2
    assert report.unique_opportunities == 2
    assert report.late == 1 and report.never == 1
    assert report.early == report.within_window == 0
    assert report.step_from_earliest == (5,)
    assert report.step_from_latest == (2,)
    assert report.source_wall_ms == (5000.0,)
    assert report.post_window_actions == (2, 0)
    assert report.post_window_unattributed_actions == (0, 0)
    assert report.post_window_failed_outcomes == (1, 0)
    assert report.post_window_unattributed_failed_outcomes == (0, 0)
    assert report.post_window_files == (2, 0)
    assert report.linked_signal_annotations == 1
    assert report.review_terminal_without_decision == 1
    rendered = render_opportunity_timing(report)
    assert "LATE=1 NEVER=1 UNJUDGEABLE=0" in rendered
    assert "unrelated candidates do not stop the clock" in rendered


def test_stale_opportunity_is_unjudgeable_instead_of_counted_as_never() -> None:
    records = _records()
    add_opportunity(
        "s1",
        "stale",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=4,
        required_evidence=(0, 1),
        note="will drift",
    )
    records[1] = _record(
        1,
        "tool_result",
        {"changed": True},
        event_id="observable",
        occurred_at=2.0,
    )

    report = measure_opportunity_timing("s1", records)

    assert report.current_annotations == 0
    assert report.stale_annotations == 1
    assert report.never == 0
    assert report.unjudgeable == 0


def test_open_window_without_a_signal_is_not_yet_a_never() -> None:
    records = _records()[:8]
    records.append(
        _record(
            8,
            "tool_proposal",
            {},
            event_id="later-live-action",
            operation_id="op-later",
            occurred_at=9.0,
        )
    )
    add_opportunity(
        "s1",
        "still-open",
        records,
        semantic_earliest=7,
        semantic_latest=7,
        observable_earliest=7,
        observable_latest=7,
        required_evidence=(7,),
        note="later activity does not prove the turn is terminal",
    )

    report = measure_opportunity_timing("s1", records)

    assert report.unjudgeable == 1
    assert report.never == 0


def test_observation_gap_keeps_delay_and_post_window_work_unjudgeable() -> None:
    records = _records()
    records[5] = _record(
        5,
        "observation_gap",
        {},
        event_id="gap",
        occurred_at=6.0,
    )
    add_opportunity(
        "s1",
        "gap-crossed",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=4,
        required_evidence=(0, 1),
        note="the observation surface was interrupted",
    )

    report = measure_opportunity_timing("s1", records)

    assert report.unjudgeable == 1
    assert report.late == report.never == 0
    assert report.post_window_actions == ()


def test_reviewer_delay_follows_the_evidence_linked_signal_job() -> None:
    records = [
        _record(0, "tool_result", {}, event_id="evidence", occurred_at=1.0),
        _record(
            1,
            "signal_candidate",
            {"status": "active", "evidence_event_ids": ["evidence"]},
            event_id="signal",
            occurred_at=2.0,
        ),
        _record(
            2,
            "review_job_queued",
            {
                "review_job_id": "job-1",
                "candidate_event_id": "signal",
                "candidate_event_ids": ["signal"],
            },
            event_id="queued",
            occurred_at=3.0,
        ),
        _record(
            3,
            "review_inference_started",
            {"review_job_id": "job-1"},
            event_id="started",
            occurred_at=4.0,
        ),
        _record(
            4,
            "reviewer_decision",
            {"review_job_id": "job-1", "decision": "verify", "stale": False},
            event_id="decision",
            occurred_at=5.0,
        ),
        _record(5, "turn_completed", {}, event_id="terminal", occurred_at=6.0),
    ]
    add_opportunity(
        "s1",
        "review-delay",
        records,
        semantic_earliest=0,
        semantic_latest=0,
        observable_earliest=0,
        observable_latest=2,
        required_evidence=(0,),
        note="measure the linked reviewer decision",
    )

    report = measure_opportunity_timing("s1", records)

    assert report.linked_signal_annotations == 1
    assert report.review_jobs_queued == report.review_inferences_started == 1
    assert report.review_decisions == 1
    assert report.review_late == 1
    assert report.review_early == report.review_within_window == 0
    assert report.review_terminal_without_decision == report.review_unjudgeable == 0
    assert report.review_step_from_earliest == (4,)
    assert report.review_step_from_latest == (2,)
    assert report.signal_to_queue_steps == (1,)
    assert report.queue_to_inference_steps == (1,)
    assert report.inference_to_decision_steps == (1,)
    assert report.queue_to_decision_steps == (2,)
    assert report.control_eligible_decisions == 1
    assert report.control_terminal_without_dispatch == 1
    assert "signals=1, queued=1, started=1, decided=1" in render_opportunity_timing(report)


def test_control_delay_follows_an_intervention_decision_to_rpc_acceptance() -> None:
    records = [
        _record(0, "tool_result", {}, event_id="evidence"),
        _record(
            1,
            "signal_candidate",
            {"status": "active", "evidence_event_ids": ["evidence"]},
            event_id="signal",
        ),
        _record(
            2,
            "review_job_queued",
            {
                "review_job_id": "job-1",
                "candidate_event_ids": ["signal"],
            },
            event_id="queued",
        ),
        _record(
            3,
            "review_inference_started",
            {"review_job_id": "job-1"},
            event_id="started",
        ),
        _record(
            4,
            "reviewer_decision",
            {"review_job_id": "job-1", "decision": "nudge", "stale": False},
            event_id="decision",
        ),
        _record(
            5,
            "control_dispatch_started",
            {"review_job_id": "job-1", "control_id": "control-1"},
            event_id="dispatch",
        ),
        _record(
            6,
            "control_rpc_accepted",
            {"review_job_id": "job-1", "control_id": "control-1"},
            event_id="accepted",
        ),
        _record(7, "turn_completed", {}, event_id="terminal"),
    ]
    add_opportunity(
        "s1",
        "control-delay",
        records,
        semantic_earliest=0,
        semantic_latest=0,
        observable_earliest=0,
        observable_latest=2,
        required_evidence=(0,),
        note="measure control dispatch and RPC acceptance",
    )

    report = measure_opportunity_timing("s1", records)

    assert report.control_eligible_decisions == report.control_dispatches == 1
    assert report.control_rpc_accepted == 1
    assert report.control_dispatch_late == 1
    assert report.control_dispatch_early == report.control_dispatch_within_window == 0
    assert report.control_step_from_earliest == (5,)
    assert report.control_step_from_latest == (3,)
    assert report.decision_to_dispatch_steps == (1,)
    assert report.dispatch_to_resolution_steps == (1,)
    rendered = render_opportunity_timing(report)
    assert "eligible=1, dispatched=1" in rendered
    assert "Control resolution: ACCEPTED=1" in rendered


def test_stale_control_before_dispatch_is_not_counted_as_missing() -> None:
    records = [
        _record(0, "tool_result", {}, event_id="evidence"),
        _record(
            1,
            "signal_candidate",
            {"status": "active", "evidence_event_ids": ["evidence"]},
            event_id="signal",
        ),
        _record(
            2,
            "review_job_queued",
            {"review_job_id": "job-1", "candidate_event_ids": ["signal"]},
            event_id="queued",
        ),
        _record(
            3,
            "reviewer_decision",
            {"review_job_id": "job-1", "decision": "verify", "stale": False},
            event_id="decision",
        ),
        _record(
            4,
            "control_terminal",
            {"review_job_id": "job-1", "control_id": "control-1", "outcome": "stale"},
            event_id="stale-control",
        ),
        _record(5, "turn_completed", {}, event_id="terminal"),
    ]
    add_opportunity(
        "s1",
        "stale-control",
        records,
        semantic_earliest=0,
        semantic_latest=0,
        observable_earliest=0,
        observable_latest=1,
        required_evidence=(0,),
        note="control target was stale before dispatch",
    )

    report = measure_opportunity_timing("s1", records)

    assert report.control_eligible_decisions == 1
    assert report.control_dispatches == 0
    assert report.control_stale_before_dispatch == 1
    assert report.control_terminal_without_dispatch == 0


def test_adoption_delay_requires_the_accepted_steer_identity() -> None:
    identity = _identity()
    target = {"target_turn_id": "turn-1", "target_connection_epoch": 1}
    records = [
        _record(0, "tool_result", {}, event_id="evidence", identity=identity),
        _record(
            1,
            "signal_candidate",
            {"status": "active", "evidence_event_ids": ["evidence"]},
            event_id="signal",
            identity=identity,
        ),
        _record(
            2,
            "review_job_queued",
            {"review_job_id": "job-1", "candidate_event_ids": ["signal"]},
            event_id="queued",
            identity=identity,
        ),
        _record(
            3,
            "reviewer_decision",
            {"review_job_id": "job-1", "decision": "nudge", "stale": False},
            event_id="decision",
            identity=identity,
        ),
        _record(
            4,
            "control_dispatch_started",
            {
                "review_job_id": "job-1",
                "control_id": "control-1",
                "control_kind": "steer",
                "client_user_message_id": "message-1",
                **target,
            },
            event_id="dispatch",
            identity=identity,
        ),
        _record(
            5,
            "control_rpc_accepted",
            {
                "review_job_id": "job-1",
                "control_id": "control-1",
                "control_kind": "steer",
                "client_user_message_id": "message-1",
                **target,
            },
            event_id="accepted",
            identity=identity,
        ),
        _record(
            6,
            "user_prompt",
            {"client_user_message_id": "message-1"},
            event_id="adopted",
            identity=identity,
        ),
        _record(7, "turn_completed", {}, event_id="terminal", identity=identity),
    ]
    add_opportunity(
        "s1",
        "adoption-delay",
        records,
        semantic_earliest=0,
        semantic_latest=0,
        observable_earliest=0,
        observable_latest=2,
        required_evidence=(0,),
        note="measure observed steer adoption",
    )

    report = measure_opportunity_timing("s1", records)

    assert report.control_adoption_eligible == report.control_adoptions == 1
    assert report.control_adoption_late == 1
    assert report.control_adoption_early == report.control_adoption_within_window == 0
    assert report.control_rpc_accepted_only == report.control_adoption_unknown == 0
    assert report.adoption_step_from_earliest == (6,)
    assert report.adoption_step_from_latest == (4,)
    assert report.acceptance_to_adoption_steps == (1,)
    assert report.decision_to_adoption_steps == (3,)
    assert "eligible=1, observed=1" in render_opportunity_timing(report)


def test_same_message_id_on_another_turn_is_not_adoption() -> None:
    identity = _identity()
    other_turn = _identity("turn-2")
    target = {"target_turn_id": "turn-1", "target_connection_epoch": 1}
    records = [
        _record(0, "tool_result", {}, event_id="evidence", identity=identity),
        _record(
            1,
            "signal_candidate",
            {"status": "active", "evidence_event_ids": ["evidence"]},
            event_id="signal",
            identity=identity,
        ),
        _record(
            2,
            "review_job_queued",
            {"review_job_id": "job-1", "candidate_event_ids": ["signal"]},
            event_id="queued",
            identity=identity,
        ),
        _record(
            3,
            "reviewer_decision",
            {"review_job_id": "job-1", "decision": "verify", "stale": False},
            event_id="decision",
            identity=identity,
        ),
        _record(
            4,
            "control_dispatch_started",
            {
                "review_job_id": "job-1",
                "control_id": "control-1",
                "control_kind": "steer",
                "client_user_message_id": "message-1",
                **target,
            },
            event_id="dispatch",
            identity=identity,
        ),
        _record(
            5,
            "control_rpc_accepted",
            {
                "review_job_id": "job-1",
                "control_id": "control-1",
                "control_kind": "steer",
                "client_user_message_id": "message-1",
                **target,
            },
            event_id="accepted",
            identity=identity,
        ),
        _record(
            6,
            "user_prompt",
            {"client_user_message_id": "message-1"},
            event_id="wrong-turn-prompt",
            identity=other_turn,
        ),
        _record(7, "turn_completed", {}, event_id="terminal", identity=identity),
    ]
    add_opportunity(
        "s1",
        "accepted-only",
        records,
        semantic_earliest=0,
        semantic_latest=0,
        observable_earliest=0,
        observable_latest=2,
        required_evidence=(0,),
        note="matching message id on another turn is not adoption",
    )

    report = measure_opportunity_timing("s1", records)

    assert report.control_adoption_eligible == 1
    assert report.control_adoptions == 0
    assert report.control_rpc_accepted_only == 1
    assert report.control_adoption_unknown == 0


def test_opportunity_reports_merge_without_inventing_coverage() -> None:
    empty = measure_opportunity_timing("empty", _records())
    merged = merge_opportunity_timing([empty, empty])

    assert merged.annotations == 0
    assert "no opportunity annotations" in render_opportunity_timing(merged)
