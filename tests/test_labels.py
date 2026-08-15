from pathlib import Path

import pytest

from spotter.cli import main
from spotter.hook import journal_path
from spotter.labels import (
    LabelError,
    add_label,
    labels_path,
    load_label_history,
    load_labels,
    matches,
    valid_session,
)
from spotter.metrics import (
    MIN_SAMPLES,
    AgreementTally,
    Tally,
    agreement_session,
    merge,
    merge_agreement,
    tally_session,
)
from spotter.paths import sanitize_session
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
    records = _journal("s1", _flagged(1))
    add_label("s1", 1, "fp", "quoted text", records)
    after = StepJournal.load(journal_path({"session_id": "s1"}))
    assert [r.event.kind for r in after] == ["tool_proposal", "gate_shadow_block"]
    assert labels_path("s1").exists()


def test_latest_label_wins() -> None:
    records = _journal("s1", _flagged(1))
    add_label("s1", 1, "tp", "first call", records)
    add_label("s1", 1, "fp", "on reflection", records)
    assert load_labels("s1")[1].verdict == "fp"


def test_label_history_retains_rater_identity_and_same_rater_corrections() -> None:
    records = _journal("s1", _flagged(1))
    add_label("s1", 1, "tp", "first", records, rater="alice")
    add_label("s1", 1, "fp", "corrected", records, rater="alice")
    add_label("s1", 1, "fp", "independent", records, rater="bob")

    history = load_label_history("s1")

    assert [label.rater for label in history] == ["alice", "alice", "bob"]
    agreement = agreement_session("s1", records)
    assert agreement.labeled_targets == agreement.double_labeled_targets == 1
    assert agreement.agreed_targets == 1


def test_label_rejects_an_empty_explicit_rater() -> None:
    records = _journal("s1", _flagged(1))
    with pytest.raises(LabelError, match="rater must be"):
        add_label("s1", 1, "tp", "", records, rater="  ")


def test_rater_agreement_reports_sample_size_and_exact_agreement() -> None:
    records = _journal("s1", _flagged(MIN_SAMPLES))
    flag_steps = [record.step for record in records if record.event.kind == "gate_shadow_block"]
    for index, step in enumerate(flag_steps):
        add_label("s1", step, "tp", "", records, rater="alice")
        add_label("s1", step, "fp" if index == 0 else "tp", "", records, rater="bob")

    agreement = agreement_session("s1", records)

    assert agreement == AgreementTally(
        labeled_targets=5,
        double_labeled_targets=5,
        agreed_targets=4,
        disagreed_targets=1,
    )
    assert "exact agreement 80% of 5" in agreement.rate_line()
    assert merge_agreement(AgreementTally(), agreement) == agreement


def test_verdict_vocabulary_is_enforced_per_target() -> None:
    records = _journal("s1", _flagged(1))
    with pytest.raises(LabelError, match="verdict must be one of"):
        add_label("s1", 1, "visible", "", records)  # session verdict on a step
    with pytest.raises(LabelError, match="verdict must be one of"):
        add_label("s1", None, "tp", "", records)  # step verdict on a session
    with pytest.raises(LabelError, match="out of range"):
        add_label("s1", 99, "tp", "", records)


def test_only_scored_records_accept_labels() -> None:
    """PR #17 review P1: a label metrics will never read must not report success."""
    records = _journal(
        "s1",
        [
            TraceEvent("tool_proposal", {"command": "x"}),
            TraceEvent("gate_shadow_block", {"rule": "r"}),
            TraceEvent("reviewer_decision", {"decision": "continue"}),
        ],
    )
    with pytest.raises(LabelError, match="only .* are scored"):
        add_label("s1", 0, "fp", "", records)  # tool_proposal is not scored
    with pytest.raises(LabelError, match="silence is not scored"):
        add_label("s1", 2, "tp", "", records)  # CONTINUE is never counted
    add_label("s1", 1, "fp", "", records)  # the gate flag is fine


def test_label_goes_stale_when_its_target_changes() -> None:
    records = _journal("s1", _flagged(1))
    add_label("s1", 1, "fp", "", records)
    label = load_labels("s1")[1]
    assert matches(label, records)
    drifted = _journal("s2", [TraceEvent("tool_proposal", {}), TraceEvent("gate_shadow_block", {})])
    assert not matches(label, drifted)


def test_session_label_goes_stale_when_the_trajectory_grows() -> None:
    """PR #17 review P1: a ceiling verdict judges the whole trajectory, so it
    cannot stay current while that trajectory keeps growing."""
    records = _journal("s1", _flagged(1))
    add_label("s1", None, "visible", "", records)
    label = load_labels("s1")[None]
    assert matches(label, records)
    grown = _journal("s1", [TraceEvent("tool_proposal", {"command": "later"})])
    assert not matches(label, grown)


def test_corrupt_label_line_raises_instead_of_reviving_the_old_verdict() -> None:
    """PR #17 review P1: silently skipping a torn correction reinstates the
    verdict the labeler had just rejected."""
    records = _journal("s1", _flagged(1))
    add_label("s1", 1, "tp", "first", records)
    with labels_path("s1").open("a", encoding="utf-8") as sink:
        sink.write('{"session": "s1", "step": 1, "verdict": "fp"')  # torn correction
    with pytest.raises(LabelError, match="unreadable"):
        load_labels("s1")


def test_ceiling_denominator_counts_every_examined_session() -> None:
    """PR #17 review P0: counting only labeled sessions reports 1/1 coverage
    after judging one session out of many."""
    labeled = _journal("s1", _flagged(1))
    add_label("s1", None, "visible", "", labeled)
    unlabeled = _journal("s2", _flagged(1))
    _, _, judged = tally_session("s1", labeled)
    _, _, unjudged = tally_session("s2", unlabeled)
    combined = merge(judged, unjudged)
    assert combined.total == 2 and combined.labeled == 1


def test_na_disposes_of_sessions_without_a_failure() -> None:
    records = _journal("s1", _flagged(1))
    add_label("s1", None, "na", "no failure in this session", records)
    _, _, ceiling = tally_session("s1", records)
    assert ceiling.labeled == 1 and ceiling.not_applicable == 1
    assert ceiling.positive == 0 and ceiling.negative == 0
    assert "n/a" in ceiling.rate_line("sessions", "visible")


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
    line = gates["git_reset_hard"].rate_line(
        "git_reset_hard", "false-positive", count_negative=True
    )
    assert "too few decided labels" in line
    assert "%" not in line  # no headline number from two samples


def test_rate_is_marked_provisional_until_coverage_is_complete() -> None:
    records = _journal("s1", _flagged(MIN_SAMPLES + 2))
    flag_steps = [r.step for r in records if r.event.kind == "gate_shadow_block"]
    for step in flag_steps[:MIN_SAMPLES]:
        add_label("s1", step, "fp", "", records)
    gates, _, _ = tally_session("s1", records)
    line = gates["git_reset_hard"].rate_line(
        "git_reset_hard", "false-positive", count_negative=True
    )
    assert "provisional" in line and f"{MIN_SAMPLES}/{MIN_SAMPLES + 2} labeled" in line

    for step in flag_steps[MIN_SAMPLES:]:
        add_label("s1", step, "fp", "", records)
    gates, _, _ = tally_session("s1", records)
    complete = gates["git_reset_hard"].rate_line(
        "git_reset_hard", "false-positive", count_negative=True
    )
    # all seven labels were fp: the gate report must show 100% false positives,
    # not the 0% true-positive share the old wording printed (PR #17 review P2)
    assert "provisional" not in complete and "false-positive 100%" in complete


def test_unclear_labels_count_as_coverage_but_not_as_a_verdict() -> None:
    tally = Tally().plus("unclear").plus("tp").plus(None)
    assert tally.total == 3 and tally.labeled == 2 and tally.unclear == 1
    assert tally.positive + tally.negative == 1


def test_cli_label_and_metrics_roundtrip(capsys: pytest.CaptureFixture[str]) -> None:
    _journal("s1", _flagged(1))
    assert (
        main(
            [
                "label",
                "--session",
                "s1",
                "--step",
                "1",
                "--verdict",
                "fp",
                "--rater",
                "alice",
            ]
        )
        == 0
    )
    assert main(["label", "--session", "s1", "--verdict", "invisible"]) == 0
    assert main(["metrics", "--session", "s1"]) == 0
    out = capsys.readouterr().out
    assert "step 1: fp by alice" in out
    assert "P3 gate false positives" in out
    assert "P4 reviewer precision" in out
    assert "P1 observability ceiling" in out
    assert "Runtime cost / efficiency (coverage-aware)" in out
    assert "Rater agreement (double-label subset)" in out
    assert "too few decided labels" in out  # honest about n=1


def test_cli_label_rejects_bad_verdict(capsys: pytest.CaptureFixture[str]) -> None:
    _journal("s1", _flagged(1))
    assert main(["label", "--session", "s1", "--step", "1", "--verdict", "nope"]) == 1
    assert "verdict must be one of" in capsys.readouterr().err


def test_cli_rejects_session_ids_that_collide_after_sanitizing() -> None:
    """PR #17 review P1: 'a/b' and 'a_b' must not share one label file."""
    for bad in ("a/b", "../../outside", "with space"):
        with pytest.raises(SystemExit):
            main(["label", "--session", bad, "--step", "1", "--verdict", "fp"])


def test_cli_metrics_aborts_on_unreadable_labels(capsys: pytest.CaptureFixture[str]) -> None:
    records = _journal("s1", _flagged(1))
    add_label("s1", 1, "fp", "", records)
    with labels_path("s1").open("a", encoding="utf-8") as sink:
        sink.write("{torn")
    assert main(["metrics", "--session", "s1"]) == 1
    assert "metrics aborted" in capsys.readouterr().err


def test_session_validation_rejects_trailing_newline() -> None:
    """PR #17 review P1: `$` matches before a trailing newline, so "a\\n"
    passed validation and then sanitized to "a_" — the same file as the
    distinct session "a_"."""
    assert not valid_session("a\n")
    assert not valid_session("a\nb")
    assert valid_session("a_") and valid_session("019fee58-ab26-72f2")
    assert sanitize_session("a\n") == "a_"  # why the bypass mattered
