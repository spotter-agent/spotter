import json
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.hook import journal_path
from spotter.opportunities import (
    OPPORTUNITY_SCHEMA,
    OPPORTUNITY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    OpportunityError,
    add_opportunity,
    load_opportunities,
    load_opportunity_history,
    matches,
    opportunities_path,
)
from spotter.snapshot import StepJournal, StepRecord
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))


def _records(session: str = "s1") -> list[StepRecord]:
    journal = StepJournal(journal_path({"session_id": session}))
    for index, kind in enumerate(
        ("tool_proposal", "tool_result", "signal_candidate", "reviewer_decision")
    ):
        journal.record(
            TraceEvent(
                kind,
                {"index": index},
                event_id=f"event-{index}",
            )
        )
    return StepJournal.load(journal_path({"session_id": session}))


def test_opportunity_window_pins_event_identity_and_keeps_independent_raters() -> None:
    records = _records()
    first = add_opportunity(
        "s1",
        "failure-loop",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=2,
        required_evidence=(1, 2, 2),
        note="the second failure made intervention defensible",
        rater="alice",
    )
    add_opportunity(
        "s1",
        "failure-loop",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=2,
        observable_latest=2,
        required_evidence=(2,),
        note="independent judgment",
        rater="bob",
    )
    corrected = add_opportunity(
        "s1",
        "failure-loop",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=1,
        required_evidence=(1,),
        note="corrected latest observable bound",
        rater="alice",
    )

    assert first.version == SCHEMA_VERSION
    raw = json.loads(opportunities_path("s1").read_text().splitlines()[0])
    assert (raw["schema"], raw["schema_version"], raw["version"]) == (
        OPPORTUNITY_SCHEMA,
        OPPORTUNITY_SCHEMA_VERSION,
        OPPORTUNITY_SCHEMA_VERSION,
    )
    assert first.required_evidence[0].event_id == "event-1"
    assert len(first.required_evidence) == 2
    assert matches(first, records)
    assert len(load_opportunity_history("s1")) == 3
    latest = load_opportunities("s1")
    assert latest[("failure-loop", "alice")] == corrected
    assert latest[("failure-loop", "bob")].observable_earliest.step == 2


def test_opportunity_window_rejects_ambiguous_or_invalid_anchors() -> None:
    records = _records()
    with pytest.raises(OpportunityError, match="semantic earliest"):
        add_opportunity(
            "s1",
            "bad-order",
            records,
            semantic_earliest=2,
            semantic_latest=1,
            observable_earliest=1,
            observable_latest=2,
            required_evidence=(1,),
            note="invalid order",
        )
    with pytest.raises(OpportunityError, match="required-evidence"):
        add_opportunity(
            "s1",
            "no-evidence",
            records,
            semantic_earliest=0,
            semantic_latest=1,
            observable_earliest=1,
            observable_latest=2,
            required_evidence=(),
            note="missing evidence",
        )
    legacy = [StepRecord(0, TraceEvent("tool_proposal", {}), None, 1.0)]
    with pytest.raises(OpportunityError, match="stable event id"):
        add_opportunity(
            "s1",
            "legacy",
            legacy,
            semantic_earliest=0,
            semantic_latest=0,
            observable_earliest=0,
            observable_latest=0,
            required_evidence=(0,),
            note="cannot pin this",
        )


def test_opportunity_window_goes_stale_when_anchored_event_changes() -> None:
    records = _records()
    window = add_opportunity(
        "s1",
        "drift",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=2,
        required_evidence=(1,),
        note="pinned evidence",
    )
    drifted = list(records)
    drifted[1] = StepRecord(
        1,
        TraceEvent("tool_result", {"index": 99}, event_id="event-1"),
        None,
        records[1].at,
    )
    assert not matches(window, drifted)


def test_opportunity_loader_refuses_future_schema() -> None:
    records = _records()
    add_opportunity(
        "s1",
        "future",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=2,
        required_evidence=(1,),
        note="future schema fixture",
    )
    path = opportunities_path("s1")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["version"] = SCHEMA_VERSION + 1
    raw["schema_version"] = SCHEMA_VERSION + 1
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    with pytest.raises(OpportunityError, match="understands up to"):
        load_opportunity_history("s1")


def test_opportunity_history_reads_legacy_and_writes_current_schema() -> None:
    records = _records()
    add_opportunity(
        "s1",
        "legacy",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=2,
        required_evidence=(1,),
        note="legacy fixture",
        rater="alice",
    )
    path = opportunities_path("s1")
    legacy = json.loads(path.read_text())
    legacy.pop("schema")
    legacy.pop("schema_version")
    path.write_text(json.dumps(legacy) + "\n")

    assert load_opportunity_history("s1")[0].opportunity_id == "legacy"
    add_opportunity(
        "s1",
        "current",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=2,
        required_evidence=(1,),
        note="current fixture",
        rater="bob",
    )

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert "schema" not in rows[0]
    assert rows[1]["schema"] == OPPORTUNITY_SCHEMA


@pytest.mark.parametrize(
    ("schema", "version", "message"),
    [
        (OPPORTUNITY_SCHEMA, OPPORTUNITY_SCHEMA_VERSION + 1, "understands up to"),
        ("future.intervention_opportunity", OPPORTUNITY_SCHEMA_VERSION, "unsupported schema"),
    ],
)
def test_opportunity_append_refuses_unknown_history_without_modifying_it(
    schema: str, version: int, message: str
) -> None:
    records = _records()
    add_opportunity(
        "s1",
        "future",
        records,
        semantic_earliest=0,
        semantic_latest=1,
        observable_earliest=1,
        observable_latest=2,
        required_evidence=(1,),
        note="future fixture",
    )
    path = opportunities_path("s1")
    raw = json.loads(path.read_text())
    raw["schema"] = schema
    raw["schema_version"] = version
    raw["version"] = version
    path.write_text(json.dumps(raw) + "\n")
    before = path.read_bytes()

    with pytest.raises(OpportunityError, match=message):
        add_opportunity(
            "s1",
            "blocked",
            records,
            semantic_earliest=0,
            semantic_latest=1,
            observable_earliest=1,
            observable_latest=2,
            required_evidence=(1,),
            note="must not append",
        )

    assert path.read_bytes() == before


def test_opportunity_append_refuses_corrupt_history_without_modifying_it() -> None:
    records = _records()
    path = opportunities_path("s1")
    path.write_text("not-json\n")
    before = path.read_bytes()

    with pytest.raises(OpportunityError, match="line 1 is unreadable"):
        add_opportunity(
            "s1",
            "blocked",
            records,
            semantic_earliest=0,
            semantic_latest=1,
            observable_earliest=1,
            observable_latest=2,
            required_evidence=(1,),
            note="must not append",
        )

    assert path.read_bytes() == before


def test_cli_records_opportunity_window(capsys: pytest.CaptureFixture[str]) -> None:
    _records()
    assert (
        main(
            [
                "label-opportunity",
                "--session",
                "s1",
                "--opportunity-id",
                "failure-loop",
                "--semantic-earliest",
                "0",
                "--semantic-latest",
                "1",
                "--observable-earliest",
                "1",
                "--observable-latest",
                "2",
                "--required-evidence",
                "1",
                "--required-evidence",
                "2",
                "--note",
                "two failures were visible",
                "--rater",
                "alice",
            ]
        )
        == 0
    )
    assert "semantic=0..1, observable=1..2" in capsys.readouterr().out
    assert opportunities_path("s1").exists()
