"""Bounded immutable input assembled from a signal and its exact live-state snapshot."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from spotter.thread_state import StateItemKind, StateItemStatus, ThreadState

_GOAL_CHARS = 600
_TEXT_CHARS = 300
_MAX_CONSTRAINTS = 20
_MAX_FILES = 50
_MAX_HYPOTHESES = 20
_MAX_FAILURES = 20
_MAX_GAPS = 10
_MAX_INCONSISTENCIES = 20
_MAX_CANDIDATE_ITEMS = 20

Scalar = str | int | float | bool | None


@dataclass(frozen=True)
class ReviewerInput:
    signal_id: str
    signal_type: str
    severity_hint: int | float | None
    evidence_event_ids: tuple[str, ...]
    involved_resources: tuple[str, ...]
    features: tuple[tuple[str, Scalar], ...]
    goal: str | None
    constraints: tuple[str, ...]
    touched_files: tuple[str, ...]
    edits_since_validation: tuple[str, ...]
    stale_hypotheses: tuple[str, ...]
    recent_failures: tuple[str, ...]
    validation_status: str
    coverage_history: str
    coverage_gaps: tuple[str, ...]
    coverage_inconsistencies: tuple[str, ...]
    truncated_fields: tuple[str, ...]

    def coverage(self) -> dict[str, object]:
        return {
            "goal_present": self.goal is not None,
            "constraints_shown": len(self.constraints),
            "candidate_evidence_shown": len(self.evidence_event_ids),
            "resources_shown": len(self.involved_resources),
            "touched_files_shown": len(self.touched_files),
            "edits_since_validation_shown": len(self.edits_since_validation),
            "stale_hypotheses_shown": len(self.stale_hypotheses),
            "recent_failures_shown": len(self.recent_failures),
            "coverage_gaps_shown": len(self.coverage_gaps),
            "truncated": bool(self.truncated_fields),
            "truncated_fields": list(self.truncated_fields),
        }


def build_reviewer_input(candidate: Mapping[str, object], snapshot: ThreadState) -> ReviewerInput:
    """Keep only the bounded state slice a future reviewer needs for this candidate."""

    truncated: set[str] = set()
    goal = (
        _text(snapshot.task.goal.text, _GOAL_CHARS, "goal", truncated)
        if snapshot.task.goal
        else None
    )
    constraints = _texts(
        [item.text for item in snapshot.task.constraints],
        _MAX_CONSTRAINTS,
        "constraints",
        truncated,
    )
    touched_files = _strings(
        sorted(snapshot.workspace.touched_files), _MAX_FILES, "touched_files", truncated
    )
    edits_since_validation = _strings(
        sorted(snapshot.workspace.edits_since_validation),
        _MAX_FILES,
        "edits_since_validation",
        truncated,
    )
    raw_candidate_resources = candidate.get("involved_resources")
    candidate_resource_values = (
        raw_candidate_resources if isinstance(raw_candidate_resources, list) else []
    )
    candidate_resources = _candidate_strings(
        raw_candidate_resources, "involved_resources", truncated
    )
    candidate_hypothesis_ids = {
        resource.removeprefix("hypothesis:")
        for resource in candidate_resource_values
        if isinstance(resource, str) and resource.startswith("hypothesis:")
    }
    stale_hypotheses = _texts(
        [
            f"{item.id}: {item.text}"
            for item in snapshot.evidence.items
            if item.kind == StateItemKind.HYPOTHESIS
            and item.status == StateItemStatus.STALE
            and item.id in candidate_hypothesis_ids
        ],
        _MAX_HYPOTHESES,
        "stale_hypotheses",
        truncated,
    )
    failures = _texts(
        [f"{item.id}: {item.text}" for item in snapshot.execution.recent_failures],
        _MAX_FAILURES,
        "recent_failures",
        truncated,
    )
    gaps = _strings(
        [
            f"{gap.source_event_id or 'unknown'}:{gap.epoch_before}->{gap.epoch_after}:"
            f"{gap.backfill_status}"
            for gap in snapshot.coverage.gaps
        ],
        _MAX_GAPS,
        "coverage_gaps",
        truncated,
    )
    inconsistencies = _strings(
        snapshot.coverage.inconsistencies,
        _MAX_INCONSISTENCIES,
        "coverage_inconsistencies",
        truncated,
    )
    evidence = _candidate_strings(
        candidate.get("evidence_event_ids"), "evidence_event_ids", truncated
    )
    raw_features = candidate.get("features")
    feature_items = (
        sorted(
            (key, _bounded_scalar(value, "features", truncated))
            for key, value in raw_features.items()
            if isinstance(key, str) and _is_scalar(value)
        )
        if isinstance(raw_features, Mapping)
        else []
    )
    features = tuple(feature_items[:_MAX_CANDIDATE_ITEMS])
    if len(feature_items) > _MAX_CANDIDATE_ITEMS:
        truncated.add("features")
    severity = candidate.get("severity_hint")
    severity_hint = (
        severity if isinstance(severity, int | float) and not isinstance(severity, bool) else None
    )
    return ReviewerInput(
        _required_string(candidate, "signal_id"),
        _required_string(candidate, "signal_type"),
        severity_hint,
        evidence,
        candidate_resources,
        features,
        goal,
        constraints,
        touched_files,
        edits_since_validation,
        stale_hypotheses,
        failures,
        str(snapshot.execution.validation),
        str(snapshot.coverage.history),
        gaps,
        inconsistencies,
        tuple(sorted(truncated)),
    )


def _texts(values: Sequence[str], limit: int, field: str, truncated: set[str]) -> tuple[str, ...]:
    if len(values) > limit:
        truncated.add(field)
    return tuple(_text(value, _TEXT_CHARS, field, truncated) for value in values[-limit:])


def _strings(values: Sequence[str], limit: int, field: str, truncated: set[str]) -> tuple[str, ...]:
    if len(values) > limit:
        truncated.add(field)
    return tuple(_text(value, _TEXT_CHARS, field, truncated) for value in values[-limit:])


def _candidate_strings(value: object, field: str, truncated: set[str]) -> tuple[str, ...]:
    values = [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
    return _strings(values, _MAX_CANDIDATE_ITEMS, field, truncated)


def _text(value: str, limit: int, field: str, truncated: set[str]) -> str:
    flat = " ".join(value.split())
    if len(flat) > limit:
        truncated.add(field)
    return flat[:limit]


def _required_string(candidate: Mapping[str, object], key: str) -> str:
    value = candidate.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"candidate has no {key}")
    return value


def _is_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | int | float | bool)


def _bounded_scalar(value: object, field: str, truncated: set[str]) -> Scalar:
    if isinstance(value, str):
        return _text(value, _TEXT_CHARS, field, truncated)
    assert value is None or isinstance(value, int | float | bool)
    return value
