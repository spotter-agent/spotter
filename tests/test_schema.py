"""Journal and label records carry time and a schema version (#55, #47)."""

import json
import time
from pathlib import Path

import pytest

from spotter.hook import journal_path
from spotter.labels import (
    LABEL_SCHEMA,
    LABEL_SCHEMA_VERSION,
    LabelError,
    add_label,
    labels_path,
    load_labels,
)
from spotter.snapshot import (
    JOURNAL_SCHEMA,
    JOURNAL_SCHEMA_VERSION,
    JOURNAL_STATE_SCHEMA,
    JOURNAL_STATE_SCHEMA_VERSION,
    LEGACY_VERSION,
    SCHEMA_VERSION,
    SnapshotError,
    StepJournal,
)
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


def test_analyze_joins_coverage_aware_costs_with_review_results(
    capsys: pytest.CaptureFixture[str],
    home: Path,
) -> None:
    from spotter.cli import _analyze_main

    journal = StepJournal(_journal())
    journal.record(TraceEvent("token_usage", {"total": {"totalTokens": 12}}))
    journal.record(
        TraceEvent(
            "reviewer_decision",
            {
                "decision": "warn",
                "failure_class": "loop",
                "confidence": 0.8,
                "reason": "repeated action",
                "spend": {"session_tokens": 3},
            },
        )
    )
    experiment_dir = home / "experiments"
    experiment_dir.mkdir(parents=True)
    (experiment_dir / "task-results.jsonl").write_text(
        json.dumps(
            {
                "result_schema_version": 1,
                "run_id": "run-1",
                "experiment_pair_id": "run-1:task-1",
                "arm": "guidance",
                "classification": "PASS",
                "replay_source_session_id": "s",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert _analyze_main("s") == 0

    output = capsys.readouterr().out
    assert "reviews=1" in output
    assert "main_tokens=12 (1/1 sessions)" in output
    assert "semantic reviewer_calls=1 reviewer_tokens=3" in output
    assert "deterministic gate_calls=0" in output
    assert "control accepted=0 adoption=0/0" in output
    assert "Objective outcomes with durable provenance to session s" in output
    assert "arms: pass=1" in output
    assert "reviewer         warn/loop" in output


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
    assert (raw["schema"], raw["schema_version"]) == (
        JOURNAL_SCHEMA,
        JOURNAL_SCHEMA_VERSION,
    )
    assert StepJournal.load(_journal())[0].version == SCHEMA_VERSION


def test_a_newer_schema_is_refused_not_guessed() -> None:
    """A newer writer may have changed what a field means; reading it anyway
    is how old evidence gets silently misinterpreted."""
    future = {
        "schema": JOURNAL_SCHEMA,
        "schema_version": SCHEMA_VERSION + 1,
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


def test_a_non_object_journal_record_is_corrupt() -> None:
    _journal().write_text("[]\n")
    with pytest.raises(SnapshotError, match="invalid journal record"):
        StepJournal.load(_journal())


def test_future_journal_cannot_hide_behind_a_matching_state_cache() -> None:
    journal = StepJournal(_journal())
    journal.record(TraceEvent("a"))
    raw = json.loads(_journal().read_text())
    raw["v"] = JOURNAL_SCHEMA_VERSION + 1
    raw["schema_version"] = JOURNAL_SCHEMA_VERSION + 1
    _journal().write_text(json.dumps(raw) + "\n")
    state_path = _journal().with_suffix(_journal().suffix + ".state")
    state_path.write_text(
        json.dumps(
            {
                "schema": JOURNAL_STATE_SCHEMA,
                "schema_version": JOURNAL_STATE_SCHEMA_VERSION + 1,
                "journal_schema_version": JOURNAL_SCHEMA_VERSION + 1,
                "steps": 1,
                "proposals": 0,
                "last_snapshot": None,
                "size": _journal().stat().st_size,
            }
        )
    )
    journal_before = _journal().read_bytes()
    state_before = state_path.read_bytes()

    with pytest.raises(SnapshotError, match="understands up to"):
        journal.record(TraceEvent("must-not-append"))

    assert _journal().read_bytes() == journal_before
    assert state_path.read_bytes() == state_before


def test_unsupported_state_cache_is_rebuilt_from_current_journal() -> None:
    journal = StepJournal(_journal())
    journal.record(TraceEvent("a"))
    state_path = _journal().with_suffix(_journal().suffix + ".state")
    state = json.loads(state_path.read_text())
    state["schema_version"] = JOURNAL_STATE_SCHEMA_VERSION + 1
    state_path.write_text(json.dumps(state))

    journal.record(TraceEvent("b"))

    assert [record.event.kind for record in StepJournal.load(_journal())] == ["a", "b"]
    rebuilt = json.loads(state_path.read_text())
    assert (rebuilt["schema"], rebuilt["schema_version"], rebuilt["journal_schema_version"]) == (
        JOURNAL_STATE_SCHEMA,
        JOURNAL_STATE_SCHEMA_VERSION,
        JOURNAL_SCHEMA_VERSION,
    )


def test_labels_are_versioned_too() -> None:
    records = [
        StepJournal(_journal()).record(TraceEvent("tool_proposal", {"command": "x"})),
        StepJournal(_journal()).record(TraceEvent("gate_shadow_block", {"rule": "r"})),
    ]
    add_label("s", 1, "fp", "", StepJournal.load(_journal()))
    assert load_labels("s")[1].version == LABEL_SCHEMA_VERSION
    raw = json.loads(labels_path("s").read_text().splitlines()[0])
    assert (raw["schema"], raw["schema_version"], raw["version"]) == (
        LABEL_SCHEMA,
        LABEL_SCHEMA_VERSION,
        LABEL_SCHEMA_VERSION,
    )
    assert records  # the journal really was the labelled thing


def test_older_label_schema_without_rater_remains_readable() -> None:
    StepJournal(_journal()).record(TraceEvent("gate_shadow_block", {"rule": "r"}))
    add_label("s", 0, "fp", "", StepJournal.load(_journal()), rater="alice")
    raw = json.loads(labels_path("s").read_text().splitlines()[0])
    raw["version"] = 1
    raw.pop("schema")
    raw.pop("schema_version")
    raw.pop("rater")
    raw.pop("scope")
    labels_path("s").write_text(json.dumps(raw) + "\n")

    assert load_labels("s")[0].rater == ""
    assert load_labels("s")[0].scope == ""

    add_label("s", 0, "tp", "corrected", StepJournal.load(_journal()), rater="bob")
    rows = [json.loads(line) for line in labels_path("s").read_text().splitlines()]
    assert "schema" not in rows[0]
    assert rows[1]["schema"] == LABEL_SCHEMA


def test_a_newer_label_schema_is_refused() -> None:
    StepJournal(_journal()).record(TraceEvent("gate_shadow_block", {"rule": "r"}))
    add_label("s", 0, "fp", "", StepJournal.load(_journal()))
    raw = json.loads(labels_path("s").read_text().splitlines()[0])
    raw["version"] = LABEL_SCHEMA_VERSION + 5
    raw["schema_version"] = LABEL_SCHEMA_VERSION + 5
    labels_path("s").write_text(json.dumps(raw) + "\n")
    with pytest.raises(LabelError, match="understands up to"):
        load_labels("s")


@pytest.mark.parametrize(
    ("schema", "version", "message"),
    [
        (LABEL_SCHEMA, LABEL_SCHEMA_VERSION + 1, "understands up to"),
        ("future.label", LABEL_SCHEMA_VERSION, "unsupported schema"),
    ],
)
def test_label_append_refuses_unknown_history_without_modifying_it(
    schema: str, version: int, message: str
) -> None:
    StepJournal(_journal()).record(TraceEvent("gate_shadow_block", {"rule": "r"}))
    add_label("s", 0, "fp", "", StepJournal.load(_journal()))
    path = labels_path("s")
    raw = json.loads(path.read_text().splitlines()[0])
    raw["schema"] = schema
    raw["schema_version"] = version
    raw["version"] = version
    path.write_text(json.dumps(raw) + "\n")
    before = path.read_bytes()

    with pytest.raises(LabelError, match=message):
        add_label("s", 0, "tp", "corrected", StepJournal.load(_journal()))

    assert path.read_bytes() == before


def test_label_append_refuses_corrupt_history_without_modifying_it() -> None:
    StepJournal(_journal()).record(TraceEvent("gate_shadow_block", {"rule": "r"}))
    path = labels_path("s")
    path.write_text("not-json\n")
    before = path.read_bytes()

    with pytest.raises(LabelError, match="line 1 is unreadable"):
        add_label("s", 0, "fp", "", StepJournal.load(_journal()))

    assert path.read_bytes() == before
