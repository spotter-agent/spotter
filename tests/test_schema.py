"""Journal and label records carry time and a schema version (#55, #47)."""

import json
import time
from pathlib import Path

import pytest

from spotter.hook import journal_path
from spotter.labels import LabelError, add_label, labels_path, load_labels
from spotter.snapshot import LEGACY_VERSION, SCHEMA_VERSION, SnapshotError, StepJournal
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "spotter"))
    return tmp_path / "spotter"


def _journal(session: str = "s") -> Path:
    return journal_path({"session_id": session})


# --- #55: records carry time -------------------------------------------------


def test_new_records_are_timestamped() -> None:
    before = time.time()
    record = StepJournal(_journal()).record(TraceEvent("tool_proposal", {"command": "ls"}))
    after = time.time()
    assert record.at is not None and before <= record.at <= after
    assert StepJournal.load(_journal())[0].at == record.at


def test_time_survives_a_round_trip_through_disk() -> None:
    journal = StepJournal(_journal())
    first = journal.record(TraceEvent("a"))
    time.sleep(0.01)
    second = journal.record(TraceEvent("b"))
    loaded = StepJournal.load(_journal())
    assert loaded[0].at == first.at and loaded[1].at == second.at
    assert loaded[0].at is not None and loaded[1].at is not None
    assert loaded[1].at > loaded[0].at  # ordering is now measurable, not assumed


def test_untimed_records_report_unknown_rather_than_zero() -> None:
    """A default of 0 is indistinguishable from an instantaneous session."""
    legacy = {"step": 0, "kind": "tool_proposal", "payload": {}, "snapshot": None}
    _journal().write_text(json.dumps(legacy) + "\n")
    record = StepJournal.load(_journal())[0]
    assert record.at is None
    assert record.version == LEGACY_VERSION


def test_analyze_reports_span_and_admits_when_it_cannot() -> None:
    from spotter.cli import _span_of

    journal = StepJournal(_journal())
    journal.record(TraceEvent("a"))
    journal.record(TraceEvent("b"))
    assert "span=" in _span_of(StepJournal.load(_journal()))

    legacy = {"step": 0, "kind": "a", "payload": {}, "snapshot": None}
    other = journal_path({"session_id": "legacy"})
    other.write_text(json.dumps(legacy) + "\n")
    assert _span_of(StepJournal.load(other)) == " span=unknown"


def test_a_partially_timed_journal_says_how_many_are_missing() -> None:
    from spotter.cli import _span_of

    legacy = {"step": 0, "kind": "a", "payload": {}, "snapshot": None}
    _journal().write_text(json.dumps(legacy) + "\n")
    StepJournal(_journal()).record(TraceEvent("b"))
    assert "+1 untimed" in _span_of(StepJournal.load(_journal()))


# --- #47: readers refuse what they cannot interpret --------------------------


def test_records_carry_the_current_schema_version() -> None:
    StepJournal(_journal()).record(TraceEvent("a"))
    raw = json.loads(_journal().read_text().splitlines()[0])
    assert raw["v"] == SCHEMA_VERSION
    assert StepJournal.load(_journal())[0].version == SCHEMA_VERSION


def test_a_newer_schema_is_refused_not_guessed() -> None:
    """A newer writer may have changed what a field means; reading it anyway
    is how old evidence gets silently misinterpreted."""
    future = {
        "v": SCHEMA_VERSION + 1,
        "step": 0,
        "kind": "tool_proposal",
        "payload": {},
        "snapshot": None,
    }
    _journal().write_text(json.dumps(future) + "\n")
    with pytest.raises(SnapshotError, match="understands up to"):
        StepJournal.load(_journal())


def test_a_mixed_version_journal_loads_every_record() -> None:
    """One journal spans an upgrade, which is why the version is per record
    rather than per file."""
    legacy = {"step": 0, "kind": "a", "payload": {}, "snapshot": None}
    _journal().write_text(json.dumps(legacy) + "\n")
    StepJournal(_journal()).record(TraceEvent("b"))
    records = StepJournal.load(_journal())
    assert [r.version for r in records] == [LEGACY_VERSION, SCHEMA_VERSION]
    assert [r.step for r in records] == [0, 1]


def test_a_non_integer_version_is_refused() -> None:
    bad = {"v": "one", "step": 0, "kind": "a", "payload": {}, "snapshot": None}
    _journal().write_text(json.dumps(bad) + "\n")
    with pytest.raises(SnapshotError, match="non-integer version"):
        StepJournal.load(_journal())


def test_labels_are_versioned_too() -> None:
    records = [
        StepJournal(_journal()).record(TraceEvent("tool_proposal", {"command": "x"})),
        StepJournal(_journal()).record(TraceEvent("gate_shadow_block", {"rule": "r"})),
    ]
    add_label("s", 1, "fp", "", StepJournal.load(_journal()))
    assert load_labels("s")[1].version == SCHEMA_VERSION
    assert records  # the journal really was the labelled thing


def test_a_newer_label_schema_is_refused() -> None:
    StepJournal(_journal()).record(TraceEvent("gate_shadow_block", {"rule": "r"}))
    add_label("s", 0, "fp", "", StepJournal.load(_journal()))
    raw = json.loads(labels_path("s").read_text().splitlines()[0])
    raw["version"] = SCHEMA_VERSION + 5
    labels_path("s").write_text(json.dumps(raw) + "\n")
    with pytest.raises(LabelError, match="understands up to"):
        load_labels("s")
