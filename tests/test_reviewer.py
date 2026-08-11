import json
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.digest import build
from spotter.hook import journal_path
from spotter.reviewer import ReviewerDecision, parse_decision, review
from spotter.snapshot import StepJournal, StepRecord
from spotter.trace import TraceEvent


def _records() -> list[StepRecord]:
    return [
        StepRecord(0, TraceEvent("user_prompt", {"prompt": "fix the login timeout"}), None),
        StepRecord(1, TraceEvent("tool_proposal", {"tool": "Bash", "command": "pytest -x"}), None),
        StepRecord(2, TraceEvent("tool_result", {"tool_response": {"exit_code": 1}}), None),
        StepRecord(3, TraceEvent("gate_shadow_block", {"rule": "git_reset_hard"}), None),
        StepRecord(
            4,
            TraceEvent(
                "reasoning_summary", {"text": "I am sure redis is the cause"}
            ),  # must NOT reach the digest
            None,
        ),
    ]


def test_digest_is_observation_only() -> None:
    digest = build(_records()).body
    assert "GOAL (step 0): fix the login timeout" in digest
    assert "pytest -x" in digest and "exit=1" in digest and "git_reset_hard" in digest
    assert "redis" not in digest  # Main's own claims are not observations (plan Q2)


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"decision": "halt", "failure_class": "none", "reason": "x", "confidence": 0.9}',
        '{"decision": "nudge", "failure_class": "none", "reason": "x", "confidence": 7}',
        '{"decision": "nudge"}',
    ],
)
def test_garbage_reviewer_output_becomes_continue(raw: str) -> None:
    decision = parse_decision(raw)
    assert decision.decision == "continue"
    assert decision.confidence == 0.0


def test_valid_output_parses() -> None:
    raw = json.dumps(
        {
            "decision": "verify",
            "failure_class": "tool_failure_loop",
            "reason": "same failing pytest retried",
            "confidence": 0.8,
        }
    )
    assert parse_decision(raw) == ReviewerDecision(
        "verify", "tool_failure_loop", "same failing pytest retried", 0.8
    )


def test_review_uses_injected_runner() -> None:
    seen: dict[str, str] = {}

    def fake_runner(model: str, prompt: str) -> str:
        seen["model"], seen["prompt"] = model, prompt
        return (
            '{"decision": "continue", "failure_class": "none", "reason": "ok", "confidence": 0.6}'
        )

    decision, _ = review(_records(), "test-model", runner=fake_runner)
    assert decision.decision == "continue"
    assert seen["model"] == "test-model"
    assert "pytest -x" in seen["prompt"]


def test_review_cli_journals_shadow_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))
    journal = StepJournal(journal_path({"session_id": "rv1"}))
    journal.record(TraceEvent("tool_proposal", {"tool": "Bash", "command": "pytest"}))

    monkeypatch.setattr(
        "spotter.cli.review",
        lambda records, model, window, constraints: (
            ReviewerDecision("nudge", "exploration_loop", "loop", 0.9),
            build(records),
        ),
    )
    assert main(["review", "--session", "rv1"]) == 0

    records = StepJournal.load(journal_path({"session_id": "rv1"}))
    verdict = records[-1]
    assert verdict.event.kind == "reviewer_decision"
    assert verdict.event.payload["shadow"] is True
    assert verdict.event.payload["decision"] == "nudge"
    assert verdict.event.payload["reviewed_upto"] == 0


def test_inflight_lock_skips_duplicate_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PR #15 review P1: a slow review must not stack paid duplicates."""
    from fcntl import LOCK_EX, flock

    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))
    journal = StepJournal(journal_path({"session_id": "busy"}))
    journal.record(TraceEvent("tool_proposal", {"command": "pytest"}))

    called: list[bool] = []
    monkeypatch.setattr("spotter.cli.review", lambda *a, **k: called.append(True))

    lock_file = journal_path({"session_id": "busy"}).with_suffix(".review.lock")
    with lock_file.open("w") as held:
        flock(held, LOCK_EX)  # simulate an in-flight review
        assert main(["review", "--session", "busy"]) == 0
    assert called == []  # no duplicate model call
    assert "already in flight" in capsys.readouterr().err
