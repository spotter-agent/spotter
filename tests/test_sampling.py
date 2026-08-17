import json
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.hook import journal_path
from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.labels import LabelError, add_label, load_labels
from spotter.metrics import agreement_session, tally_signal_silence
from spotter.sampling import (
    SCHEMA_VERSION,
    SIGNAL_SAMPLING_SCHEMA,
    SIGNAL_SAMPLING_SCHEMA_VERSION,
    SignalSampleError,
    load_signal_sampling,
    sample_signal_silence,
    signal_samples_path,
)
from spotter.snapshot import StepJournal, StepRecord
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))
    return tmp_path


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        ThreadId("thread-1"),
        TurnId("turn-1"),
        None,
        IdentityProvenance("codex", "thread-1", "turn-1"),
    )


def _event(event_id: str, *, identity: bool = True) -> TraceEvent:
    return TraceEvent(
        "tool_proposal",
        {"tool_use_id": event_id},
        event_id=event_id,
        identity=_identity() if identity else None,
    )


def _journal(session: str, events: list[TraceEvent]) -> list[StepRecord]:
    journal = StepJournal(journal_path({"session_id": session}))
    for event in events:
        journal.record(event)
    return StepJournal.load(journal_path({"session_id": session}))


def test_sampling_persists_non_emitted_observable_stratum_idempotently() -> None:
    records = _journal(
        "s1",
        [
            _event("source-1"),
            _event("source-2"),
            _event("source-3", identity=False),
            _event("source-4"),
            TraceEvent(
                "signal_candidate",
                {
                    "signal_id": "signal-2",
                    "signal_type": "failure_streak",
                    "status": "active",
                    "source_event_id": "source-2",
                },
            ),
            TraceEvent(
                "signal_candidate_suppressed",
                {
                    "signal_id": "signal-4",
                    "signal_type": "failure_streak",
                    "status": "cooled_down",
                    "source_event_id": "source-4",
                },
            ),
        ],
    )

    first = sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    second = sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    batches, samples = load_signal_sampling("s1")

    assert first == second == batches[0]
    assert (first.eligible, first.selected) == (1, 1)
    assert (first.excluded_emitted, first.excluded_suppressed, first.excluded_unobservable) == (
        1,
        1,
        1,
    )
    assert [(sample.step, sample.event_id) for sample in samples] == [(0, "source-1")]
    assert len(signal_samples_path("s1").read_text().splitlines()) == 2
    persisted = [json.loads(line) for line in signal_samples_path("s1").read_text().splitlines()]
    assert all(
        record["schema"] == SIGNAL_SAMPLING_SCHEMA
        and record["schema_version"] == SIGNAL_SAMPLING_SCHEMA_VERSION
        and record["version"] == SIGNAL_SAMPLING_SCHEMA_VERSION
        for record in persisted
    )


def test_scoped_signal_labels_coexist_with_gate_negative_labels() -> None:
    records = _journal("s1", [_event("source-1")])
    sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)

    add_label("s1", 0, "tn", "gate correctly stayed silent", records, rater="alice")
    add_label(
        "s1",
        0,
        "miss",
        "failure signal should have fired",
        records,
        rater="alice",
        signal_type="failure_streak",
    )
    add_label(
        "s1",
        0,
        "miss",
        "independent judgment",
        records,
        rater="bob",
        signal_type="failure_streak",
    )

    assert load_labels("s1")[0].verdict == "tn"
    assert load_labels("s1", scope="signal:failure_streak")[0].verdict == "miss"
    tallies, _ = tally_signal_silence("s1", records)
    assert tallies["failure_streak/tool_proposal@p=1"].positive == 1
    agreement = agreement_session("s1", records)
    assert agreement.labeled_targets == 2
    assert agreement.double_labeled_targets == agreement.agreed_targets == 1


def test_signal_silence_tally_preserves_a_multi_kind_sampling_frame() -> None:
    records = _journal(
        "s1",
        [
            _event("tool-1"),
            TraceEvent("file_edit", {}, event_id="edit-1", identity=_identity()),
        ],
    )
    sample_signal_silence(
        "s1",
        records,
        "failure_streak",
        ("tool_proposal", "file_edit"),
        1,
    )
    add_label(
        "s1",
        0,
        "miss",
        "tool failure criterion met",
        records,
        signal_type="failure_streak",
    )
    add_label(
        "s1",
        1,
        "tn",
        "file edit criterion absent",
        records,
        signal_type="failure_streak",
    )

    tallies, batches = tally_signal_silence("s1", records)

    assert len(batches) == 1
    assert batches[0].eligible == batches[0].selected == 2
    assert set(tallies) == {"failure_streak/file_edit,tool_proposal@p=1"}
    tally = tallies["failure_streak/file_edit,tool_proposal@p=1"]
    assert tally.total == tally.labeled == 2
    assert tally.positive == tally.negative == 1


def test_sampling_appends_only_the_new_journal_suffix() -> None:
    records = _journal("s1", [_event("source-1")])
    first = sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    records = _journal("s1", [_event("source-2")])
    second = sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    batches, samples = load_signal_sampling("s1")

    assert (first.start_step, first.end_step) == (0, 0)
    assert (second.start_step, second.end_step) == (1, 1)
    assert batches == (first, second)
    assert [sample.event_id for sample in samples] == ["source-1", "source-2"]


def test_late_signal_emission_makes_a_silence_sample_stale() -> None:
    records = _journal("s1", [_event("source-1")])
    sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    add_label(
        "s1",
        0,
        "miss",
        "initially silent",
        records,
        signal_type="failure_streak",
    )
    records = _journal(
        "s1",
        [
            TraceEvent(
                "signal_candidate",
                {
                    "signal_id": "signal-1",
                    "signal_type": "failure_streak",
                    "status": "active",
                    "source_event_id": "source-1",
                },
            )
        ],
    )

    with pytest.raises(LabelError, match="sample is stale"):
        add_label("s1", 0, "miss", "", records, signal_type="failure_streak")
    tallies, _ = tally_signal_silence("s1", records)
    tally = tallies["failure_streak/tool_proposal@p=1"]
    assert tally.total == tally.stale == 1 and tally.labeled == 0
    agreement = agreement_session("s1", records)
    assert agreement.labeled_targets == 0 and agreement.stale_labels == 1


def test_signal_label_requires_a_current_persisted_sample() -> None:
    records = _journal("s1", [_event("source-1")])
    with pytest.raises(LabelError, match="not in a persisted"):
        add_label("s1", 0, "miss", "", records, signal_type="failure_streak")

    sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    with pytest.raises(LabelError, match="verdict must be"):
        add_label("s1", 0, "tp", "", records, signal_type="failure_streak")
    with pytest.raises(LabelError, match="written criteria"):
        add_label("s1", 0, "miss", "", records, signal_type="failure_streak")


def test_sampling_rejects_unknown_types_and_invalid_rates() -> None:
    records = _journal("s1", [_event("source-1")])
    with pytest.raises(SignalSampleError, match="unknown signal type"):
        sample_signal_silence("s1", records, "imaginary", ("tool_proposal",), 1)
    with pytest.raises(SignalSampleError, match="sample rate"):
        sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 0)


def test_sampling_rejects_overlapping_frames_with_incompatible_rates() -> None:
    records = _journal("s1", [_event("source-1")])
    sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 0.5)

    with pytest.raises(SignalSampleError, match="overlapping signal strata"):
        sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)


def test_legacy_sampling_history_is_read_before_current_records_are_appended() -> None:
    records = _journal("s1", [_event("source-1")])
    sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    path = signal_samples_path("s1")
    legacy = []
    for line in path.read_text().splitlines():
        raw = json.loads(line)
        raw.pop("schema")
        raw.pop("schema_version")
        legacy.append(json.dumps(raw))
    path.write_text("\n".join(legacy) + "\n")

    records = _journal("s1", [_event("source-2")])
    sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    batches, samples = load_signal_sampling("s1")
    persisted = [json.loads(line) for line in path.read_text().splitlines()]

    assert len(batches) == len(samples) == 2
    assert "schema" not in persisted[0] and "schema" not in persisted[1]
    assert all(record["schema"] == SIGNAL_SAMPLING_SCHEMA for record in persisted[2:])


def test_newer_sampling_schema_is_refused_without_appending() -> None:
    records = _journal("s1", [_event("source-1")])
    sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    path = signal_samples_path("s1")
    lines = path.read_text().splitlines()
    raw = json.loads(lines[0])
    raw["version"] = SCHEMA_VERSION + 1
    raw["schema_version"] = SCHEMA_VERSION + 1
    lines[0] = json.dumps(raw)
    path.write_text("\n".join(lines) + "\n")
    before = path.read_bytes()
    records = _journal("s1", [_event("source-2")])

    with pytest.raises(SignalSampleError, match="understands up to"):
        sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    assert path.read_bytes() == before


def test_foreign_sampling_schema_is_refused_without_appending() -> None:
    records = _journal("s1", [_event("source-1")])
    sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    path = signal_samples_path("s1")
    lines = path.read_text().splitlines()
    raw = json.loads(lines[0])
    raw["schema"] = "someone.else"
    lines[0] = json.dumps(raw)
    path.write_text("\n".join(lines) + "\n")
    before = path.read_bytes()
    records = _journal("s1", [_event("source-2")])

    with pytest.raises(SignalSampleError, match="unsupported schema"):
        sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    assert path.read_bytes() == before


def test_orphaned_sample_is_refused() -> None:
    records = _journal("s1", [_event("source-1")])
    sample_signal_silence("s1", records, "failure_streak", ("tool_proposal",), 1)
    lines = signal_samples_path("s1").read_text().splitlines()
    raw = json.loads(lines[1])
    raw["batch_id"] = "missing"
    lines[1] = json.dumps(raw)
    signal_samples_path("s1").write_text("\n".join(lines) + "\n")
    with pytest.raises(SignalSampleError, match="unknown batch"):
        load_signal_sampling("s1")


def test_cli_samples_labels_and_reports_the_declared_bias(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _journal("s1", [_event("source-1")])

    assert (
        main(
            [
                "sample-signals",
                "--session",
                "s1",
                "--signal-type",
                "failure_streak",
                "--event-kind",
                "tool_proposal",
                "--sample-rate",
                "1",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "label",
                "--session",
                "s1",
                "--step",
                "0",
                "--signal-type",
                "failure_streak",
                "--verdict",
                "miss",
                "--note",
                "failure criterion met",
            ]
        )
        == 0
    )
    assert main(["metrics", "--session", "s1"]) == 0

    output = capsys.readouterr().out
    assert "sampled 1/1 eligible failure_streak sources" in output
    assert "step 0: tool_proposal source-1" in output
    assert "Signal candidate misses" in output
    assert "failure_streak/tool_proposal@p=1" in output
    assert "unobservable=0/1 (0%)" in output
    assert "rates represent only the declared event-kind strata" in output
