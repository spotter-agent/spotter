"""Human-facing summaries of durable Spotter intervention provenance."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from spotter.snapshot import StepRecord


@dataclass(frozen=True)
class InterventionSummary:
    intervention_id: str
    review_job_id: str | None = None
    action: str | None = None
    failure_class: str | None = None
    reason: str | None = None
    hypothesis: str | None = None
    model: str | None = None
    confidence: float | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    connection_epoch: int | None = None
    status: str = "DECIDED"
    status_reason: str | None = None
    signal_ids: tuple[str, ...] = ()
    evidence_event_ids: tuple[str, ...] = ()
    updated_at: float | None = None


def summarize_interventions(records: Iterable[StepRecord]) -> tuple[InterventionSummary, ...]:
    ordered = sorted(records, key=lambda record: (record.at is not None, record.at or 0.0))
    decisions: dict[str, Mapping[str, object]] = {}
    jobs: dict[str, Mapping[str, object]] = {}
    summaries: dict[str, InterventionSummary] = {}

    for record in ordered:
        event = record.event
        payload = event.payload
        job_id = _text(payload.get("review_job_id"))
        if event.kind == "review_job_queued" and job_id is not None:
            jobs[job_id] = payload
        elif event.kind == "reviewer_decision" and job_id is not None:
            decisions[job_id] = payload

        intervention_id = _text(payload.get("intervention_id"))
        if intervention_id is None or not event.kind.startswith("control_"):
            continue
        summary = summaries.get(intervention_id, InterventionSummary(intervention_id))
        job_id = job_id or summary.review_job_id
        decision = decisions.get(job_id, {}) if job_id is not None else {}
        job = jobs.get(job_id, {}) if job_id is not None else {}
        identity = event.identity
        confidence = decision.get("confidence")
        epoch = payload.get("target_connection_epoch")
        summaries[intervention_id] = replace(
            summary,
            review_job_id=job_id,
            action=_upper_text(decision.get("decision")) or summary.action,
            failure_class=_text(decision.get("failure_class")) or summary.failure_class,
            reason=_text(decision.get("reason")) or summary.reason,
            hypothesis=_text(decision.get("hypothesis")) or summary.hypothesis,
            model=_text(decision.get("model")) or summary.model,
            confidence=(
                float(confidence)
                if isinstance(confidence, int | float) and not isinstance(confidence, bool)
                else summary.confidence
            ),
            thread_id=(
                identity.thread_id.value
                if identity is not None and identity.thread_id is not None
                else summary.thread_id
            ),
            turn_id=_text(payload.get("target_turn_id")) or summary.turn_id,
            connection_epoch=(
                epoch
                if isinstance(epoch, int) and not isinstance(epoch, bool)
                else summary.connection_epoch
            ),
            status=_status(event.kind, payload),
            status_reason=_text(payload.get("reason_code")) or summary.status_reason,
            signal_ids=_texts(job.get("signal_ids")) or summary.signal_ids,
            evidence_event_ids=_texts(job.get("candidate_event_ids")) or summary.evidence_event_ids,
            updated_at=record.at,
        )
    return tuple(
        sorted(
            summaries.values(),
            key=lambda summary: (summary.updated_at is not None, summary.updated_at or 0.0),
            reverse=True,
        )
    )


def _status(kind: str, payload: Mapping[str, object]) -> str:
    statuses = {
        "control_dispatch_started": "SEND_PENDING",
        "control_rpc_accepted": "RPC_ACCEPTED",
        "control_observed_in_turn": "OBSERVED_IN_TURN",
        "control_observed_outside_target": "OBSERVED_OUTSIDE_TARGET",
    }
    if kind in statuses:
        return statuses[kind]
    outcome = _text(payload.get("outcome"))
    return outcome.upper() if outcome is not None else kind.removeprefix("control_").upper()


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _upper_text(value: object) -> str | None:
    text = _text(value)
    return text.upper() if text is not None else None


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)
