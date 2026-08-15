"""Durable human annotations for intervention timing windows."""

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from getpass import getuser
from pathlib import Path

from spotter.labels import fingerprint
from spotter.paths import sanitize_session, spotter_home
from spotter.snapshot import StepRecord

SCHEMA_VERSION = 1


class OpportunityError(ValueError):
    """An intervention opportunity annotation is invalid or unreadable."""


@dataclass(frozen=True)
class EventAnchor:
    step: int
    event_id: str
    fingerprint: str


@dataclass(frozen=True)
class OpportunityWindow:
    session: str
    opportunity_id: str
    rater: str
    labeled_at: str
    note: str
    semantic_earliest: EventAnchor
    semantic_latest: EventAnchor
    observable_earliest: EventAnchor
    observable_latest: EventAnchor
    required_evidence: tuple[EventAnchor, ...]
    version: int = SCHEMA_VERSION


def opportunities_path(session: str) -> Path:
    base = spotter_home() / "opportunities"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{sanitize_session(session)}.jsonl"


def add_opportunity(
    session: str,
    opportunity_id: str,
    records: list[StepRecord],
    *,
    semantic_earliest: int,
    semantic_latest: int,
    observable_earliest: int,
    observable_latest: int,
    required_evidence: tuple[int, ...],
    note: str,
    rater: str | None = None,
) -> OpportunityWindow:
    """Append one independently attributable, event-identity-pinned window."""

    identity = opportunity_id.strip()
    if not identity or len(identity) > 200:
        raise OpportunityError("opportunity id must contain at most 200 characters")
    rationale = note.strip()
    if not rationale:
        raise OpportunityError("opportunity annotation requires a rationale in --note")
    rater_id = getuser() if rater is None else rater.strip()
    if not rater_id or len(rater_id) > 200:
        raise OpportunityError("rater must be a non-empty identity of at most 200 characters")
    if semantic_earliest > semantic_latest:
        raise OpportunityError("semantic earliest step must not follow semantic latest step")
    if observable_earliest > observable_latest:
        raise OpportunityError("observable earliest step must not follow observable latest step")
    if not required_evidence:
        raise OpportunityError("at least one --required-evidence step is required")

    window = OpportunityWindow(
        session=session,
        opportunity_id=identity,
        rater=rater_id,
        labeled_at=datetime.now(UTC).isoformat(),
        note=rationale,
        semantic_earliest=_anchor(records, semantic_earliest),
        semantic_latest=_anchor(records, semantic_latest),
        observable_earliest=_anchor(records, observable_earliest),
        observable_latest=_anchor(records, observable_latest),
        required_evidence=tuple(
            _anchor(records, step) for step in dict.fromkeys(required_evidence)
        ),
    )
    with opportunities_path(session).open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(asdict(window), ensure_ascii=False) + "\n")
    return window


def load_opportunity_history(session: str) -> tuple[OpportunityWindow, ...]:
    path = opportunities_path(session)
    if not path.exists():
        return ()
    windows: list[OpportunityWindow] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise TypeError("annotation must be an object")
            version = raw.get("version", 0)
            if not isinstance(version, int) or isinstance(version, bool):
                raise OpportunityError(f"{path.name} line {number} has a non-integer version")
            if version > SCHEMA_VERSION:
                raise OpportunityError(
                    f"{path.name} line {number} was written by schema v{version}; "
                    f"this build understands up to v{SCHEMA_VERSION}"
                )
            if version < 1:
                raise OpportunityError(
                    f"{path.name} line {number} has unsupported schema v{version}"
                )
            evidence = raw["required_evidence"]
            if not isinstance(evidence, list) or not evidence:
                raise TypeError("required_evidence must be a non-empty list")
            window = OpportunityWindow(
                session=_text(raw, "session"),
                opportunity_id=_text(raw, "opportunity_id"),
                rater=_text(raw, "rater"),
                labeled_at=_text(raw, "labeled_at"),
                note=_text(raw, "note"),
                semantic_earliest=_load_anchor(raw, "semantic_earliest"),
                semantic_latest=_load_anchor(raw, "semantic_latest"),
                observable_earliest=_load_anchor(raw, "observable_earliest"),
                observable_latest=_load_anchor(raw, "observable_latest"),
                required_evidence=tuple(_anchor_from_mapping(value) for value in evidence),
                version=version,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise OpportunityError(f"{path.name} line {number} is unreadable ({error})") from error
        if window.session != session:
            raise OpportunityError(
                f"{path.name} line {number} belongs to session {window.session!r}"
            )
        if window.semantic_earliest.step > window.semantic_latest.step:
            raise OpportunityError(f"{path.name} line {number} reverses the semantic window")
        if window.observable_earliest.step > window.observable_latest.step:
            raise OpportunityError(f"{path.name} line {number} reverses the observable window")
        windows.append(window)
    return tuple(windows)


def load_opportunities(session: str) -> dict[tuple[str, str], OpportunityWindow]:
    """Return the latest correction per opportunity and independent rater."""

    latest: dict[tuple[str, str], OpportunityWindow] = {}
    for window in load_opportunity_history(session):
        latest[(window.opportunity_id, window.rater)] = window
    return latest


def matches(window: OpportunityWindow, records: list[StepRecord]) -> bool:
    anchors = (
        window.semantic_earliest,
        window.semantic_latest,
        window.observable_earliest,
        window.observable_latest,
        *window.required_evidence,
    )
    return all(_matches_anchor(anchor, records) for anchor in anchors)


def _anchor(records: list[StepRecord], step: int) -> EventAnchor:
    if not 0 <= step < len(records):
        raise OpportunityError(f"step {step} out of range (journal has {len(records)} steps)")
    record = records[step]
    event_id = record.event.event_id
    if not isinstance(event_id, str) or not event_id:
        raise OpportunityError(
            f"step {step} has no stable event id; it cannot anchor a timing window"
        )
    return EventAnchor(step, event_id, fingerprint(record))


def _matches_anchor(anchor: EventAnchor, records: list[StepRecord]) -> bool:
    if not 0 <= anchor.step < len(records):
        return False
    record = records[anchor.step]
    return record.event.event_id == anchor.event_id and fingerprint(record) == anchor.fingerprint


def _load_anchor(raw: dict[str, object], key: str) -> EventAnchor:
    value = raw[key]
    if not isinstance(value, dict):
        raise TypeError(f"{key} must be an object")
    return _anchor_from_mapping(value)


def _anchor_from_mapping(raw: dict[str, object]) -> EventAnchor:
    step = raw["step"]
    if not isinstance(step, int) or isinstance(step, bool) or step < 0:
        raise TypeError("anchor step must be a non-negative integer")
    return EventAnchor(step, _text(raw, "event_id"), _text(raw, "fingerprint"))


def _text(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} must be a non-empty string")
    return value
