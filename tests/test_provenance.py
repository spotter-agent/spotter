import json
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.feedback import (
    FEEDBACK_SCHEMA,
    FEEDBACK_SCHEMA_VERSION,
    FeedbackError,
    add_feedback,
    feedback_path,
    load_feedback,
)
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
    stored = [json.loads(line) for line in feedback_path().read_text().splitlines()]
    assert {(row["schema"], row["schema_version"], row["version"]) for row in stored} == {
        (FEEDBACK_SCHEMA, FEEDBACK_SCHEMA_VERSION, FEEDBACK_SCHEMA_VERSION)
    }

    with pytest.raises(FeedbackError, match="category must be one of"):
        add_feedback("spt-0123456789ab", "false_positive")


def test_feedback_reads_legacy_records_and_writes_current_schema(
    intervention_journal: StepJournal,
) -> None:
    path = feedback_path(create=True)
    path.write_text(
        json.dumps(
            {
                "feedback_id": "feedback-legacy",
                "supervision_event_id": "spt-0123456789ab",
                "category": "USEFUL",
                "created_at": "2026-08-15T00:00:00+00:00",
                "note": "legacy",
                "rater": "developer-1",
                "version": 1,
            }
        )
        + "\n"
    )

    assert load_feedback()[0].feedback_id == "feedback-legacy"
    add_feedback("spt-0123456789ab", "too_late", rater="developer-1")

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert "schema" not in rows[0]
    assert rows[1]["schema"] == FEEDBACK_SCHEMA
    assert rows[1]["schema_version"] == FEEDBACK_SCHEMA_VERSION


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (
            {
                "schema": FEEDBACK_SCHEMA,
                "schema_version": FEEDBACK_SCHEMA_VERSION + 1,
                "version": FEEDBACK_SCHEMA_VERSION + 1,
            },
            "uses schema v2",
        ),
        (
            {
                "schema": "future.intervention_feedback",
                "schema_version": FEEDBACK_SCHEMA_VERSION,
                "version": FEEDBACK_SCHEMA_VERSION,
            },
            "unsupported schema",
        ),
    ],
)
def test_feedback_refuses_unknown_history_before_append(
    intervention_journal: StepJournal, record: dict[str, object], message: str
) -> None:
    path = feedback_path(create=True)
    record.update(
        {
            "feedback_id": "feedback-future",
            "supervision_event_id": "spt-0123456789ab",
            "category": "USEFUL",
            "created_at": "2026-08-15T00:00:00+00:00",
        }
    )
    path.write_text(json.dumps(record) + "\n")
    before = path.read_bytes()

    with pytest.raises(FeedbackError, match=message):
        add_feedback("spt-0123456789ab", "useful", rater="developer-1")

    assert path.read_bytes() == before


def test_feedback_refuses_corrupt_history_before_append(
    intervention_journal: StepJournal,
) -> None:
    path = feedback_path(create=True)
    path.write_text("not-json\n")
    before = path.read_bytes()

    with pytest.raises(FeedbackError, match="line 1 is unreadable"):
        add_feedback("spt-0123456789ab", "useful", rater="developer-1")

    assert path.read_bytes() == before


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


def test_cli_explains_block_policy_resource_and_safe_remedy(
    intervention_journal: StepJournal, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = StepJournal(intervention_journal.path.parent / "hook-session.jsonl")
    journal.record(
        TraceEvent(
            "gate_block",
            {
                "supervision_event_id": "spt-block-0123456789ab",
                "rule": "dependency_change",
                "rule_version": 1,
                "reason": "manifest edit: pyproject.toml",
                "tool": "apply_patch",
                "resource": "pyproject.toml",
                "reversibility_class": "B",
                "effect_kind": "workspace_write",
            },
            identity=RuntimeIdentity.legacy_hook("codex", "session-1"),
        )
    )

    assert main(["interventions"]) == 0
    listed = capsys.readouterr().out
    assert "spt-block-0123456789ab  BLOCK" in listed
    assert "ENFORCED" in listed

    assert main(["explain", "--supervision-id", "spt-block-0123456789ab"]) == 0
    explained = capsys.readouterr().out
    assert "Action\n  BLOCK (ENFORCED)" in explained
    assert "rule=dependency_change version=1" in explained
    assert "resource=pyproject.toml" in explained
    assert "block_dependency_changes configuration" in explained

    assert (
        main(
            [
                "feedback",
                "--supervision-id",
                "spt-block-0123456789ab",
                "--category",
                "block_correct",
                "--rater",
                "developer-1",
            ]
        )
        == 0
    )


@pytest.fixture
def interrupt_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StepJournal:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    sessions = journal_path({"session_id": "probe"}).parent
    journal = StepJournal(sessions / "app-server-interrupt.jsonl")
    identity = RuntimeIdentity(
        ThreadId("thread-9"),
        TurnId("turn-9"),
        AttachmentId("attachment-9"),
        IdentityProvenance("codex", "external-thread", "external-turn"),
    )
    # No intervention_id: interrupts inject no advisory input to correlate.
    control = {
        "control_id": "spt-int-0123456789ab",
        "control_kind": "interrupt",
        "target_turn_id": "turn-9",
        "target_connection_epoch": 4,
    }
    journal.record(TraceEvent("control_dispatch_started", control, identity=identity))
    journal.record(TraceEvent("control_rpc_accepted", control, identity=identity))
    journal.record(
        TraceEvent(
            "control_terminal",
            {
                **control,
                "outcome": "turn_aborted",
                "reason_code": "observed_interrupted_status",
            },
            identity=identity,
        )
    )
    return journal


def test_summary_surfaces_the_interrupt_lifecycle(interrupt_journal: StepJournal) -> None:
    summary = summarize_interventions(StepJournal.load(interrupt_journal.path))[0]

    assert summary.intervention_id == "spt-int-0123456789ab"
    assert summary.action == "INTERRUPT"
    assert summary.status == "TURN_ABORTED"
    assert summary.status_reason == "observed_interrupted_status"
    assert summary.thread_id == "thread-9"
    assert summary.turn_id == "turn-9"
    assert summary.connection_epoch == 4


def test_cli_lists_and_explains_an_interrupt(
    interrupt_journal: StepJournal, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["interventions"]) == 0
    listed = capsys.readouterr().out
    assert "spt-int-0123456789ab  INTERRUPT" in listed
    assert "TURN_ABORTED" in listed

    assert main(["explain", "--intervention-id", "spt-int-0123456789ab"]) == 0
    assert "observed_interrupted_status" in capsys.readouterr().out
