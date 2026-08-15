from pathlib import Path

import pytest

from spotter.cli import main
from spotter.feedback import FeedbackError, add_feedback, load_feedback
from spotter.hook import journal_path
from spotter.identity import (
    AttachmentId,
    IdentityProvenance,
    RuntimeIdentity,
    ThreadId,
    TurnId,
)
from spotter.provenance import summarize_interventions
from spotter.snapshot import StepJournal
from spotter.trace import TraceEvent


@pytest.fixture
def intervention_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StepJournal:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    sessions = journal_path({"session_id": "probe"}).parent
    journal = StepJournal(sessions / "app-server-test.jsonl")
    identity = RuntimeIdentity(
        ThreadId("thread-1"),
        TurnId("turn-1"),
        AttachmentId("attachment-1"),
        IdentityProvenance("codex", "external-thread", "external-turn"),
    )
    common = {"review_job_id": "job-1"}
    journal.record(
        TraceEvent(
            "review_job_queued",
            {
                **common,
                "signal_ids": ["signal-1"],
                "candidate_event_ids": ["evidence-1"],
            },
            identity=identity,
        )
    )
    journal.record(
        TraceEvent(
            "reviewer_decision",
            {
                **common,
                "decision": "verify",
                "failure_class": "possible_spec_drift",
                "reason": "The requested validation is missing",
                "hypothesis": "The current change may be unvalidated",
                "confidence": 0.8,
                "model": "reviewer-model",
            },
            identity=identity,
        )
    )
    control = {
        **common,
        "control_id": "spt-0123456789ab",
        "intervention_id": "spt-0123456789ab",
        "target_turn_id": "turn-1",
        "target_connection_epoch": 3,
    }
    journal.record(TraceEvent("control_dispatch_started", control, identity=identity))
    journal.record(TraceEvent("control_rpc_accepted", control, identity=identity))
    journal.record(
        TraceEvent(
            "control_terminal",
            {
                **control,
                "outcome": "rpc_accepted_only",
                "reason_code": "target_completed_without_observed_input",
            },
            identity=identity,
        )
    )
    return journal


def test_summary_joins_reviewer_evidence_and_delivery(
    intervention_journal: StepJournal,
) -> None:
    summary = summarize_interventions(StepJournal.load(intervention_journal.path))[0]

    assert summary.intervention_id == "spt-0123456789ab"
    assert summary.action == "VERIFY"
    assert summary.thread_id == "thread-1"
    assert summary.turn_id == "turn-1"
    assert summary.connection_epoch == 3
    assert summary.signal_ids == ("signal-1",)
    assert summary.evidence_event_ids == ("evidence-1",)
    assert summary.status == "RPC_ACCEPTED_ONLY"
    assert summary.status_reason == "target_completed_without_observed_input"


def test_cli_lists_and_explains_intervention(
    intervention_journal: StepJournal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["interventions"]) == 0
    listed = capsys.readouterr().out
    assert "spt-0123456789ab  VERIFY" in listed
    assert "RPC_ACCEPTED_ONLY" in listed

    assert main(["explain", "--intervention-id", "spt-0123456789ab"]) == 0
    explained = capsys.readouterr().out
    assert "model judgment, not ground truth" in explained
    assert "signals=signal-1" in explained
    assert "events=evidence-1" in explained
    assert "target_completed_without_observed_input" in explained
    assert "Human feedback (evaluation evidence, not ground truth)\n  none" in explained


def test_feedback_is_structured_redacted_and_append_only(
    intervention_journal: StepJournal,
) -> None:
    first = add_feedback(
        "spt-0123456789ab",
        "useful",
        note="confirmed; token=ghp_1234567890123456",
        rater="developer-1",
    )
    second = add_feedback(
        "spt-0123456789ab",
        "too_late",
        note="correct, but the turn had ended",
        rater="developer-1",
    )

    history = load_feedback("spt-0123456789ab")
    assert [item.feedback_id for item in history] == [first.feedback_id, second.feedback_id]
    assert [item.category for item in history] == ["USEFUL", "TOO_LATE"]
    assert history[0].note == "confirmed; token=[REDACTED]"

    with pytest.raises(FeedbackError, match="category must be one of"):
        add_feedback("spt-0123456789ab", "false_positive")


def test_cli_records_feedback_and_explain_keeps_it_separate_from_ground_truth(
    intervention_journal: StepJournal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "feedback",
                "--intervention-id",
                "spt-0123456789ab",
                "--category",
                "wrong",
                "--note",
                "The assumption was already verified",
                "--rater",
                "developer-1",
            ]
        )
        == 0
    )
    assert "recorded WRONG feedback" in capsys.readouterr().out

    assert main(["explain", "--intervention-id", "spt-0123456789ab"]) == 0
    explained = capsys.readouterr().out
    assert "Human feedback (evaluation evidence, not ground truth)" in explained
    assert "WRONG by developer-1" in explained
    assert "The assumption was already verified" in explained


def test_cli_refuses_feedback_for_unknown_intervention(
    intervention_journal: StepJournal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(
            [
                "feedback",
                "--intervention-id",
                "spt-unknown",
                "--category",
                "OTHER",
            ]
        )
        == 1
    )
    assert "was not found" in capsys.readouterr().err
