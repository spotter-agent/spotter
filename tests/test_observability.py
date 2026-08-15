import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from spotter.app_server import AppServerEvent
from spotter.cli import main
from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId
from spotter.ingestion import AppServerTraceIngestor, CodexTraceNormalizer
from spotter.observability import (
    APP_SERVER_ITEM_FIELDS,
    CoverageStatus,
    EvidenceFamily,
    EvidenceTiming,
    ObservabilityError,
    OpportunityStatus,
    SourceAuditStore,
    classify_opportunity,
    measure_observability,
    source_audit_sample,
    state_coverage_status,
)
from spotter.snapshot import StepJournal, StepRecord
from spotter.thread_state import ThreadStateStore
from spotter.trace import TraceEvent, TraceProvenance

FIXTURE = Path(__file__).parent / "fixtures" / "app_server_conformance.json"


def _raw(method: str, params: dict[str, Any]) -> AppServerEvent:
    return AppServerEvent(method, {"method": method, "params": params})


def _cases() -> list[dict[str, Any]]:
    loaded = json.loads(FIXTURE.read_text())
    assert isinstance(loaded, list)
    return loaded


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["name"]))
def test_source_vs_trace_conformance_corpus(case: dict[str, Any]) -> None:
    raw = _raw(str(case["method"]), case["params"])
    event = CodexTraceNormalizer().normalize(raw)
    state = ThreadStateStore().observe(event)
    sample = source_audit_sample(
        raw,
        event,
        state_status=state_coverage_status(event, state),
    )

    assert event.kind == case["expected_kind"]
    assert sample.status == case["expected_status"]
    assert list(sample.families) == case["families"]
    expected_state = {
        "token_usage": CoverageStatus.ADAPTER_DROPPED,
        "runtime_event_unknown": CoverageStatus.UNKNOWN,
        "item_completed": CoverageStatus.OBSERVED_ENCRYPTED,
    }.get(event.kind, CoverageStatus.OBSERVED_EXACT)
    assert sample.state_status == expected_state
    serialized = json.dumps(sample.__dict__)
    assert "SYNTHETIC_PRIVATE_REASONING" not in serialized
    assert "SYNTHETIC_CIPHERTEXT" not in serialized


def test_source_field_present_but_trace_drops_it_is_adapter_loss() -> None:
    raw = _raw(
        "item/completed",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "command-1",
                "type": "commandExecution",
                "command": "pytest",
                "status": "completed",
                "exitCode": 1,
            },
        },
    )
    normalized = CodexTraceNormalizer().normalize(raw)
    dropped = replace(normalized, payload={"command": "pytest", "status": "completed"})

    assert source_audit_sample(raw, dropped).status == CoverageStatus.ADAPTER_DROPPED


def test_normalizer_and_audit_share_the_item_field_whitelist() -> None:
    for item_type, fields in APP_SERVER_ITEM_FIELDS.items():
        raw = _raw(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": f"{item_type}-1",
                    "type": item_type,
                    **dict.fromkeys(fields, "synthetic"),
                },
            },
        )
        event = CodexTraceNormalizer().normalize(raw)

        assert source_audit_sample(raw, event).status == CoverageStatus.OBSERVED_EXACT


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("encryptedContent", CoverageStatus.OBSERVED_ENCRYPTED),
        ("childEncryptedContent", CoverageStatus.OBSERVED_ENCRYPTED),
        ("isEncrypted", CoverageStatus.OBSERVED_ENCRYPTED),
        ("unencryptedContent", CoverageStatus.UNKNOWN),
    ],
)
def test_encrypted_field_names_follow_wire_name_segments(
    field: str, expected: CoverageStatus
) -> None:
    raw = _raw(
        "item/completed",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "future-1",
                "type": "futureItem",
                field: "synthetic",
            },
        },
    )
    event = CodexTraceNormalizer().normalize(raw)

    assert source_audit_sample(raw, event).status == expected


@pytest.mark.parametrize(
    ("evidence", "boundary", "expected"),
    [
        (
            [
                EvidenceTiming(
                    EvidenceFamily.TOOL_OUTCOME, CoverageStatus.OBSERVED_EXACT, state_step=4
                )
            ],
            4,
            OpportunityStatus.VISIBLE_IN_TIME,
        ),
        (
            [
                EvidenceTiming(
                    EvidenceFamily.TOOL_OUTCOME, CoverageStatus.OBSERVED_EXACT, state_step=5
                )
            ],
            4,
            OpportunityStatus.VISIBLE_TOO_LATE,
        ),
        (
            [EvidenceTiming(EvidenceFamily.SUBAGENT_LINEAGE, CoverageStatus.SOURCE_NOT_EXPOSED)],
            4,
            OpportunityStatus.STRUCTURALLY_INVISIBLE,
        ),
        (
            [EvidenceTiming(EvidenceFamily.TOOL_OUTCOME, CoverageStatus.ADAPTER_DROPPED)],
            4,
            OpportunityStatus.LOST_BY_ADAPTER,
        ),
        (
            [EvidenceTiming(EvidenceFamily.TOOL_OUTCOME, CoverageStatus.OBSERVATION_GAP)],
            4,
            OpportunityStatus.LOST_BY_GAP,
        ),
        (
            [EvidenceTiming(EvidenceFamily.REPOSITORY_EXPLORATION, CoverageStatus.NOT_PERFORMED)],
            4,
            OpportunityStatus.UNJUDGEABLE,
        ),
    ],
)
def test_opportunity_classification(
    evidence: list[EvidenceTiming], boundary: int, expected: OpportunityStatus
) -> None:
    assert classify_opportunity(boundary, evidence) == expected


def test_shape_audit_is_bounded_private_and_rejects_corruption(tmp_path: Path) -> None:
    store = SourceAuditStore(tmp_path / "audit.jsonl", max_records=2)
    ingestor = AppServerTraceIngestor(tmp_path / "journals")
    raw = _raw(
        "item/completed",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "reasoning-1",
                "type": "reasoning",
                "summary": ["safe"],
                "content": ["SECRET_VALUE"],
            },
        },
    )
    event = ingestor.normalizer.normalize(raw)
    for _ in range(5):
        store.record(raw, event, disposition="ingested")

    assert len(store.load()) == 2
    assert "SECRET_VALUE" not in store.path.read_text()
    assert store.path.stat().st_mode & 0o777 == 0o600
    with store.path.open("ab") as sink:
        sink.write(b'{"torn":')
    assert len(store.load()) == 2
    store.record(raw, event, disposition="ingested")
    assert len(store.load()) == 3
    store.path.write_text("not-json\n")
    with pytest.raises(ObservabilityError, match="line 1"):
        store.load()


def test_measurement_separates_hook_app_server_source_and_state() -> None:
    identity = RuntimeIdentity(
        ThreadId("thread-1"),
        None,
        None,
        IdentityProvenance("codex", agent_thread_id="external-1"),
    )
    hook = [
        StepRecord(
            0,
            TraceEvent(
                "tool_result",
                {"tool_response": "output without an exit status"},
                provenance=TraceProvenance("codex_hook", "PostToolUse"),
            ),
            None,
        )
    ]
    app_event = TraceEvent(
        "command_result",
        {"command": "pytest", "exitCode": 0},
        event_id="event-1",
        identity=identity,
        provenance=TraceProvenance("codex_app_server", "item/completed"),
        connection_epoch=1,
    )
    app = [
        StepRecord(0, app_event, None),
        StepRecord(
            1,
            TraceEvent("observation_gap", identity=identity, connection_epoch=1),
            None,
        ),
    ]
    raw = _raw(
        "item/completed",
        {
            "threadId": "external-1",
            "turnId": "turn-1",
            "item": {
                "id": "command-1",
                "type": "commandExecution",
                "command": "pytest",
                "exitCode": 0,
            },
        },
    )
    sample = source_audit_sample(
        raw,
        app_event,
        state_status=CoverageStatus.OBSERVED_EXACT,
    )
    report = measure_observability(
        [hook, app], [sample, replace(sample, disposition="deduplicated")]
    )

    assert report.hook_sessions == report.app_server_sessions == 1
    assert report.gaps == 1
    assert report.deduplicated_source_samples == 1
    assert report.trace_family_status["hook"]["tool_outcome"] == {"observed_partial": 1}
    assert report.trace_family_status["app_server"]["tool_outcome"] == {"observed_exact": 1}
    assert report.source_family_status["tool_outcome"] == {"observed_exact": 1}
    assert report.state_family_status["tool_outcome"] == {"observed_exact": 1}


def test_live_ingestion_records_thread_state_preservation(tmp_path: Path) -> None:
    ingestor = AppServerTraceIngestor(tmp_path)
    states = ThreadStateStore()
    raw = _raw(
        "item/completed",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "command-1",
                "type": "commandExecution",
                "command": "pytest",
                "status": "completed",
                "exitCode": 0,
            },
        },
    )
    event = replace(ingestor.normalizer.normalize(raw), connection_epoch=1)
    record = ingestor.record(event)
    assert record is not None
    state = states.observe(record.event)

    ingestor.audit_source(
        raw,
        record.event,
        disposition="ingested",
        state_status=state_coverage_status(record.event, state),
    )

    sample = ingestor.source_audit.load()[0]
    assert sample.status == CoverageStatus.OBSERVED_EXACT
    assert sample.state_status == CoverageStatus.OBSERVED_EXACT


def test_spotter_input_is_intervention_coverage_not_a_dropped_user_goal() -> None:
    raw = _raw(
        "item/completed",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "spotter-input-1",
                "type": "userMessage",
                "content": [{"type": "text", "text": "Verify the retry assumption"}],
                "clientId": "spt-0123456789ab",
            },
        },
    )
    normalized = CodexTraceNormalizer().normalize(raw)
    event = replace(
        normalized,
        payload={
            **normalized.payload,
            "input_origin": "spotter_supervision",
            "intervention_id": "spt-0123456789ab",
            "intervention_relation": "target_turn",
        },
    )
    state = ThreadStateStore().observe(event)
    sample = source_audit_sample(
        raw,
        event,
        state_status=state_coverage_status(event, state),
    )

    assert state.task.goal is None
    assert state.supervision.interventions[-1].provenance.event_id == event.event_id
    assert sample.families == (EvidenceFamily.INTERVENTION_DELIVERY.value,)
    assert sample.state_status == CoverageStatus.OBSERVED_EXACT


def test_observability_cli_does_not_claim_an_unmeasured_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    StepJournal(tmp_path / "sessions" / "hook-session.jsonl").record(
        TraceEvent("user_prompt", {"prompt": "synthetic"})
    )

    assert main(["observability"]) == 0
    output = capsys.readouterr().out
    assert "hook=1, app_server=0" in output
    assert "post-App-Server source ceiling cannot be stated" in output
