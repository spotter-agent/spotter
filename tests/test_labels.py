from pathlib import Path

import pytest

from spotter.cli import main
from spotter.hook import journal_path
from spotter.labels import LabelError, add_label, labels_path, load_labels, matches
from spotter.metrics import MIN_SAMPLES, Tally, tally_session
from spotter.snapshot import StepJournal, StepRecord
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))
    return tmp_path


def _journal(session: str, events: list[TraceEvent]) -> list[StepRecord]:
    journal = StepJournal(journal_path({"session_id": session}))
    for event in events:
        journal.record(event)
    return StepJournal.load(journal_path({"session_id": session}))


def _flagged(n: int) -> list[TraceEvent]:
    events: list[TraceEvent] = []
    for i in range(n):
        events.append(TraceEvent("tool_proposal", {"command": f"cmd{i}"}))
        events.append(TraceEvent("gate_shadow_block", {"rule": "git_reset_hard"}))
    return events


def test_labels_never_enter_the_journal() -> None:
    """The reviewer reads the journal; labels there would feed it its own
    report card, and would shift step numbers forks point at."""
    records = _journal("s1", [TraceEvent("tool_proposal", {"command": "x"})])
    add_label("s1", 0, "fp", "quoted text", records)
    after = StepJournal.load(journal_path({"session_id": "s1"}))
    assert [r.event.kind for r in after] == ["tool_proposal"]
    assert labels_path("s1").exists()


def test_latest_label_wins() -> None:
    records = _journal("s1", [TraceEvent("tool_proposal", {"command": "x"})])
    add_label("s1", 0, "tp", "first call", records)
    add_label("s1", 0, "fp", "on reflection", records)
    assert load_labels("s1")[0].verdict == "fp"


def test_verdict_vocabulary_is_enforced_per_target() -> None:
    records = _journal("s1", [TraceEvent("tool_proposal", {})])
    with pytest.raises(LabelError, match="verdict must be one of"):
        add_label("s1", 0, "visible", "", records)  # session verdict on a step
    with pytest.raises(LabelError, match="verdict must be one of"):
        add_label("s1", None, "tp", "", records)  # step verdict on a session
    with pytest.raises(LabelError, match="out of range"):
        add_label("s1", 99, "tp", "", records)


def test_label_goes_stale_when_its_target_changes() -> None:
    records = _journal("s1", [TraceEvent("tool_proposal", {"command": "original"})])
    add_label("s1", 0, "fp", "", records)
    label = load_labels("s1")[0]
    assert matches(label, records)
    drifted = _journal("s2", [TraceEvent("tool_proposal", {"command": "different"})])
    assert not matches(label, drifted)


def test_stale_labels_are_not_counted_as_evidence() -> None:
    records = _journal("s1", _flagged(1))
    add_label("s1", 1, "fp", "", records)
    drifted = _journal("s2", [TraceEvent("tool_proposal", {}), TraceEvent("gate_shadow_block", {})])
    gates, _, _ = tally_session("s1", drifted)
    tally = gates["unknown"]
    assert tally.stale == 1 and tally.labeled == 0


def test_continue_verdicts_are_not_scored() -> None:
    """Scoring silence as correct would inflate reviewer precision for free."""
    records = _journal(
        "s1",
        [
            TraceEvent("reviewer_decision", {"decision": "continue"}),
            TraceEvent("reviewer_decision", {"decision": "nudge"}),
        ],
    )
    _, reviewer, _ = tally_session("s1", records)
    assert reviewer.total == 1


def test_rate_is_withheld_below_minimum_samples() -> None:
    records = _journal("s1", _flagged(2))
    for step in (1, 3):
        add_label("s1", step, "fp", "", records)
    gates, _, _ = tally_session("s1", records)
    line = gates["git_reset_hard"].rate_line("git_reset_hard", "true-positive")
    assert "too few decided labels" in line
    assert "%" not in line  # no headline number from two samples


def test_rate_is_marked_provisional_until_coverage_is_complete() -> None:
    records = _journal("s1", _flagged(MIN_SAMPLES + 2))
    flag_steps = [r.step for r in records if r.event.kind == "gate_shadow_block"]
    for step in flag_steps[:MIN_SAMPLES]:
        add_label("s1", step, "fp", "", records)
    gates, _, _ = tally_session("s1", records)
    line = gates["git_reset_hard"].rate_line("git_reset_hard", "true-positive")
    assert "provisional" in line and f"{MIN_SAMPLES}/{MIN_SAMPLES + 2} labeled" in line

    for step in flag_steps[MIN_SAMPLES:]:
        add_label("s1", step, "fp", "", records)
    gates, _, _ = tally_session("s1", records)
    complete = gates["git_reset_hard"].rate_line("git_reset_hard", "true-positive")
    assert "provisional" not in complete and "true-positive 0%" in complete


def test_unclear_labels_count_as_coverage_but_not_as_a_verdict() -> None:
    tally = Tally().plus("unclear").plus("tp").plus(None)
    assert tally.total == 3 and tally.labeled == 2 and tally.unclear == 1
    assert tally.positive + tally.negative == 1


def test_cli_label_and_metrics_roundtrip(capsys: pytest.CaptureFixture[str]) -> None:
    _journal("s1", _flagged(1))
    assert main(["label", "--session", "s1", "--step", "1", "--verdict", "fp"]) == 0
    assert main(["label", "--session", "s1", "--verdict", "invisible"]) == 0
    assert main(["metrics", "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert "P3 gate false positives" in out
    assert "P4 reviewer precision" in out
    assert "P1 observability ceiling" in out
    assert "too few decided labels" in out  # honest about n=1


def test_cli_label_rejects_bad_verdict(capsys: pytest.CaptureFixture[str]) -> None:
    _journal("s1", [TraceEvent("tool_proposal", {})])
    assert main(["label", "--session", "s1", "--step", "0", "--verdict", "nope"]) == 1
    assert "verdict must be one of" in capsys.readouterr().err
