"""Cheap incremental candidates derived from runtime-neutral Trace IR."""

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from statistics import median

from spotter.identity import RuntimeIdentity, ThreadId
from spotter.outcomes import outcome_failure
from spotter.snapshot import StepRecord
from spotter.thread_state import ThreadState, ThreadStateStore
from spotter.trace import TraceEvent, TraceProvenance

_OUTCOME_KINDS = {"command_result", "tool_result", "file_edit", "test_result"}
_TERMINAL_KINDS = {
    "observation_gap",
    "runtime_attachment_unavailable",
    "thread_archived",
    "thread_closed",
    "thread_deleted",
    "turn_completed",
}
_FRONTIER_RESET_KINDS = {
    "command_result",
    "diff_updated",
    "evidence_invalidated",
    "file_edit",
    "file_read",
    "hypothesis",
    "reasoning_summary",
    "search",
    "test_result",
    "verification_satisfied",
}
_READ_VERBS = ("describe", "fetch", "find", "get", "list", "open", "read", "search", "view")
_READ_RESOURCE_KEYS = {
    "file": "file",
    "file_path": "file",
    "path": "file",
    "repository": "repository",
    "resource": "resource",
    "scope": "resource",
    "uri": "uri",
    "url": "url",
}
_EVIDENCE_LIMIT = 20
_FRONTIER_LIMIT = 128
_BLOCK_LIMIT = 128
_SCOPE_LIMIT = 128
_SCOPE_TEXT_LIMIT = 4096
_PATH_CHARS = re.compile(r"[A-Za-z0-9_./-]+")
_CAUSAL_ACTION_KINDS = {
    "command_started",
    "file_change_started",
    "tool_proposal",
    "tool_started",
}
_HYPOTHESIS_LIMIT = 128
_HYPOTHESIS_ID_LIMIT = 300
_BUDGET_BASELINE_SAMPLES = 3
_BUDGET_BASELINE_LIMIT = 20
_BUDGET_PERCENT_LIMIT = 1_000_000


class SignalType(StrEnum):
    BUDGET_ANOMALY = "budget_anomaly"
    EDITS_WITHOUT_VALIDATION = "edits_without_validation"
    FAILURE_STREAK = "failure_streak"
    RECURRENCE_AFTER_DETERMINISTIC_BLOCK = "recurrence_after_deterministic_block"
    REPEATED_EQUIVALENT_TOOL_CALL = "repeated_equivalent_tool_call"
    REPEATED_READ_NO_FRONTIER = "repeated_read_no_frontier"
    STALE_HYPOTHESIS_REUSE = "stale_hypothesis_reuse"
    TOUCHED_SCOPE_GROWTH = "touched_scope_growth"


class SignalStatus(StrEnum):
    ACTIVE = "active"
    COOLED_DOWN = "cooled_down"
    RESOLVED = "resolved"
    STALE = "stale"


@dataclass(frozen=True)
class SignalCandidate:
    signal_id: str
    signal_type: SignalType
    thread_id: ThreadId
    turn_id: str | None
    connection_epoch: int | None
    state_version: int
    first_seen_at: float | None
    last_seen_at: float | None
    severity_hint: int
    evidence_event_ids: tuple[str, ...]
    involved_resources: tuple[str, ...]
    status: SignalStatus
    source_event_id: str
    features: tuple[tuple[str, int], ...]

    def to_trace_event(self, trigger: TraceEvent) -> TraceEvent:
        suffix = hashlib.sha256(f"{self.status}:{self.source_event_id}".encode()).hexdigest()[:16]
        return TraceEvent(
            (
                "signal_candidate_suppressed"
                if self.status == SignalStatus.COOLED_DOWN
                else "signal_candidate"
            ),
            {
                "signal_id": self.signal_id,
                "signal_type": self.signal_type,
                "thread_id": self.thread_id.value,
                "turn_id": self.turn_id,
                "target_connection_epoch": self.connection_epoch,
                "state_version": self.state_version,
                "first_seen_at": self.first_seen_at,
                "last_seen_at": self.last_seen_at,
                "severity_hint": self.severity_hint,
                "evidence_event_ids": list(self.evidence_event_ids),
                "involved_resources": list(self.involved_resources),
                "features": dict(self.features),
                "status": self.status,
                "source_event_id": self.source_event_id,
            },
            event_id=f"spotter:signal:{self.signal_id}:{suffix}",
            occurred_at=trigger.occurred_at,
            identity=trigger.identity,
            provenance=TraceProvenance("spotterd", "signal_engine"),
            connection_epoch=trigger.connection_epoch,
        )


@dataclass(frozen=True)
class _SignalStreak:
    signal_id: str
    signal_type: SignalType
    feature: str
    key: str
    identity: RuntimeIdentity
    connection_epoch: int | None
    first_seen_at: float | None
    last_seen_at: float | None
    count: int
    evidence_event_ids: tuple[str, ...]
    resources: tuple[str, ...]
    emitted: bool = False


@dataclass(frozen=True)
class _ReadFrontier:
    identity: RuntimeIdentity
    connection_epoch: int | None
    observations: frozenset[str]


@dataclass(frozen=True)
class _DeclaredScope:
    identity: RuntimeIdentity
    connection_epoch: int | None
    paths: tuple[str, ...]


@dataclass(frozen=True)
class _DurationBaseline:
    identity: RuntimeIdentity
    connection_epoch: int | None
    values_ms: tuple[float, ...]


class SignalEngine:
    """Incremental signal state; candidates are evidence, never semantic verdicts."""

    def __init__(
        self,
        *,
        failure_threshold: int = 2,
        repeated_tool_threshold: int = 3,
        no_frontier_threshold: int = 3,
        scope_growth_threshold: int = 3,
        unvalidated_edit_threshold: int = 3,
        context_window_threshold_percent: int = 80,
        duration_anomaly_factor_percent: int = 300,
    ) -> None:
        if failure_threshold < 2:
            raise ValueError("failure signal threshold must be >= 2")
        if repeated_tool_threshold < 2:
            raise ValueError("repeated tool signal threshold must be >= 2")
        if no_frontier_threshold < 2:
            raise ValueError("no-frontier read signal threshold must be >= 2")
        if scope_growth_threshold < 2:
            raise ValueError("scope-growth signal threshold must be >= 2")
        if unvalidated_edit_threshold < 2:
            raise ValueError("unvalidated-edit signal threshold must be >= 2")
        if not 1 <= context_window_threshold_percent <= 100:
            raise ValueError("context-window threshold must be between 1 and 100")
        if duration_anomaly_factor_percent <= 100:
            raise ValueError("duration anomaly factor must be > 100")
        self.failure_threshold = failure_threshold
        self.repeated_tool_threshold = repeated_tool_threshold
        self.no_frontier_threshold = no_frontier_threshold
        self.scope_growth_threshold = scope_growth_threshold
        self.unvalidated_edit_threshold = unvalidated_edit_threshold
        self.context_window_threshold_percent = context_window_threshold_percent
        self.duration_anomaly_factor_percent = duration_anomaly_factor_percent
        self._failure_streaks: dict[ThreadId, _SignalStreak] = {}
        self._repeated_tools: dict[ThreadId, _SignalStreak] = {}
        self._read_streaks: dict[ThreadId, _SignalStreak] = {}
        self._read_frontiers: dict[ThreadId, _ReadFrontier] = {}
        self._blocked_actions: dict[tuple[ThreadId, str], _SignalStreak] = {}
        self._declared_scopes: dict[ThreadId, _DeclaredScope] = {}
        self._scope_growths: dict[ThreadId, _SignalStreak] = {}
        self._unvalidated_edits: dict[ThreadId, _SignalStreak] = {}
        self._unvalidated_sources: dict[ThreadId, dict[str, str]] = {}
        self._stale_hypothesis_reuses: dict[tuple[ThreadId, str], _SignalStreak] = {}
        self._budget_streaks: dict[tuple[ThreadId, str], _SignalStreak] = {}
        self._duration_baselines: dict[tuple[ThreadId, str], _DurationBaseline] = {}
        self._seen_event_ids: dict[ThreadId, set[str]] = {}

    def update(
        self,
        event: TraceEvent,
        state_version: int,
        state: ThreadState | None = None,
    ) -> tuple[SignalCandidate, ...]:
        identity = event.identity
        if identity is None or identity.thread_id is None or event.event_id is None:
            return ()
        thread_id = identity.thread_id
        seen = self._seen_event_ids.setdefault(thread_id, set())
        if event.event_id in seen:
            return ()
        seen.add(event.event_id)

        candidates: list[SignalCandidate] = []
        streak = self._failure_streaks.get(thread_id)
        repeated = self._repeated_tools.get(thread_id)
        reading = self._read_streaks.get(thread_id)
        frontier = self._read_frontiers.get(thread_id)
        declared_scope = self._declared_scopes.get(thread_id)
        scope_growth = self._scope_growths.get(thread_id)
        unvalidated_edits = self._unvalidated_edits.get(thread_id)
        blocked_keys = [key for key in self._blocked_actions if key[0] == thread_id]
        stale_reuse_keys = [key for key in self._stale_hypothesis_reuses if key[0] == thread_id]
        budget_keys = [key for key in self._budget_streaks if key[0] == thread_id]
        duration_keys = [key for key in self._duration_baselines if key[0] == thread_id]
        if streak is not None and _target_changed(streak, event):
            candidates.extend(self._finish(streak, SignalStatus.STALE, event, state_version))
            self._failure_streaks.pop(thread_id, None)
            streak = None
        if repeated is not None and _target_changed(repeated, event):
            candidates.extend(self._finish(repeated, SignalStatus.STALE, event, state_version))
            self._repeated_tools.pop(thread_id, None)
            repeated = None
        if reading is not None and _target_changed(reading, event):
            candidates.extend(self._finish(reading, SignalStatus.STALE, event, state_version))
            self._read_streaks.pop(thread_id, None)
            reading = None
        if frontier is not None and _target_changed(frontier, event):
            self._read_frontiers.pop(thread_id, None)
        if declared_scope is not None and _target_changed(declared_scope, event):
            self._declared_scopes.pop(thread_id, None)
        if scope_growth is not None and _target_changed(scope_growth, event):
            candidates.extend(self._finish(scope_growth, SignalStatus.STALE, event, state_version))
            self._scope_growths.pop(thread_id, None)
        if unvalidated_edits is not None and _target_changed(unvalidated_edits, event):
            candidates.extend(
                self._finish(unvalidated_edits, SignalStatus.STALE, event, state_version)
            )
            self._unvalidated_edits.pop(thread_id, None)
            self._unvalidated_sources.pop(thread_id, None)
        for blocked_key in blocked_keys:
            blocked = self._blocked_actions[blocked_key]
            if _target_changed(blocked, event):
                candidates.extend(self._finish(blocked, SignalStatus.STALE, event, state_version))
                self._blocked_actions.pop(blocked_key)
        for stale_reuse_key in stale_reuse_keys:
            stale_reuse = self._stale_hypothesis_reuses[stale_reuse_key]
            if _target_changed(stale_reuse, event):
                candidates.extend(
                    self._finish(stale_reuse, SignalStatus.STALE, event, state_version)
                )
                self._stale_hypothesis_reuses.pop(stale_reuse_key)
        for budget_key in budget_keys:
            budget = self._budget_streaks[budget_key]
            if _target_changed(budget, event):
                candidates.extend(self._finish(budget, SignalStatus.STALE, event, state_version))
                self._budget_streaks.pop(budget_key)
        for duration_key in duration_keys:
            baseline = self._duration_baselines[duration_key]
            if _target_changed(baseline, event):
                self._duration_baselines.pop(duration_key)
        if event.kind in _TERMINAL_KINDS:
            if streak is not None:
                candidates.extend(self._finish(streak, SignalStatus.STALE, event, state_version))
                self._failure_streaks.pop(thread_id, None)
            if repeated is not None:
                candidates.extend(self._finish(repeated, SignalStatus.STALE, event, state_version))
                self._repeated_tools.pop(thread_id, None)
            if reading is not None:
                candidates.extend(self._finish(reading, SignalStatus.STALE, event, state_version))
                self._read_streaks.pop(thread_id, None)
            self._read_frontiers.pop(thread_id, None)
            self._declared_scopes.pop(thread_id, None)
            scope_growth = self._scope_growths.pop(thread_id, None)
            if scope_growth is not None:
                candidates.extend(
                    self._finish(scope_growth, SignalStatus.STALE, event, state_version)
                )
            unvalidated_edits = self._unvalidated_edits.pop(thread_id, None)
            self._unvalidated_sources.pop(thread_id, None)
            if unvalidated_edits is not None:
                candidates.extend(
                    self._finish(unvalidated_edits, SignalStatus.STALE, event, state_version)
                )
            for blocked_key in tuple(self._blocked_actions):
                if blocked_key[0] != thread_id:
                    continue
                blocked = self._blocked_actions.pop(blocked_key)
                candidates.extend(self._finish(blocked, SignalStatus.STALE, event, state_version))
            for stale_reuse_key in tuple(self._stale_hypothesis_reuses):
                if stale_reuse_key[0] != thread_id:
                    continue
                stale_reuse = self._stale_hypothesis_reuses.pop(stale_reuse_key)
                candidates.extend(
                    self._finish(stale_reuse, SignalStatus.STALE, event, state_version)
                )
            for budget_key in tuple(self._budget_streaks):
                if budget_key[0] != thread_id:
                    continue
                budget = self._budget_streaks.pop(budget_key)
                candidates.extend(self._finish(budget, SignalStatus.STALE, event, state_version))
            for duration_key in tuple(self._duration_baselines):
                if duration_key[0] == thread_id:
                    self._duration_baselines.pop(duration_key)
            return tuple(candidates)

        candidates.extend(self._update_budget_anomaly(event, state_version))
        candidates.extend(self._update_scope_growth(event, state_version))
        candidates.extend(self._update_unvalidated_edits(event, state_version))
        candidates.extend(self._update_stale_hypothesis_reuse(event, state_version, state))
        candidates.extend(self._update_block_recurrence(event, state_version))
        candidates.extend(self._update_repeated_tool(event, state_version))
        candidates.extend(self._update_no_frontier_reads(event, state_version))
        if event.kind not in _OUTCOME_KINDS:
            return tuple(candidates)

        key, resources = _equivalence(event)
        failure = outcome_failure(event.payload)
        if key is None or failure is None:
            if streak is not None:
                candidates.extend(self._finish(streak, SignalStatus.STALE, event, state_version))
                self._failure_streaks.pop(thread_id, None)
            return tuple(candidates)
        if failure is False:
            if streak is not None:
                candidates.extend(self._finish(streak, SignalStatus.RESOLVED, event, state_version))
                self._failure_streaks.pop(thread_id, None)
            return tuple(candidates)
        if streak is not None and streak.key != key:
            candidates.extend(self._finish(streak, SignalStatus.STALE, event, state_version))
            streak = None

        if streak is None:
            streak = _SignalStreak(
                _signal_id(event, SignalType.FAILURE_STREAK, key),
                SignalType.FAILURE_STREAK,
                "consecutive_failures",
                key,
                identity,
                event.connection_epoch,
                event.occurred_at,
                event.occurred_at,
                1,
                (event.event_id,),
                resources,
            )
        else:
            streak = replace(
                streak,
                last_seen_at=event.occurred_at,
                count=streak.count + 1,
                evidence_event_ids=(streak.evidence_event_ids + (event.event_id,))[
                    -_EVIDENCE_LIMIT:
                ],
            )
        self._failure_streaks[thread_id] = streak
        if streak.count >= self.failure_threshold:
            status = SignalStatus.COOLED_DOWN if streak.emitted else SignalStatus.ACTIVE
            candidates.append(_candidate(streak, status, event.event_id, state_version))
            if not streak.emitted:
                self._failure_streaks[thread_id] = replace(streak, emitted=True)
        return tuple(candidates)

    def _update_budget_anomaly(
        self, event: TraceEvent, state_version: int
    ) -> tuple[SignalCandidate, ...]:
        return (
            *self._update_context_budget(event, state_version),
            *self._update_duration_budget(event, state_version),
        )

    def _update_context_budget(
        self, event: TraceEvent, state_version: int
    ) -> tuple[SignalCandidate, ...]:
        if event.kind != "token_usage":
            return ()
        assert event.identity is not None and event.identity.thread_id is not None
        assert event.event_id is not None
        if event.identity.turn_id is None:
            return ()
        total_group = event.payload.get("total")
        total = (
            _nonnegative_int(total_group.get("totalTokens"))
            if isinstance(total_group, Mapping)
            else None
        )
        window = _positive_int(event.payload.get("modelContextWindow"))
        if total is None or window is None:
            return ()
        percent = min(_BUDGET_PERCENT_LIMIT, total * 100 // window)
        thread_id = event.identity.thread_id
        key = (thread_id, "context-window")
        streak = self._budget_streaks.get(key)
        if percent < self.context_window_threshold_percent:
            if streak is None:
                return ()
            self._budget_streaks.pop(key)
            return self._finish(streak, SignalStatus.RESOLVED, event, state_version)
        if streak is None:
            streak = _SignalStreak(
                _signal_id(event, SignalType.BUDGET_ANOMALY, key[1]),
                SignalType.BUDGET_ANOMALY,
                "context_window_utilization_percent",
                key[1],
                event.identity,
                event.connection_epoch,
                event.occurred_at,
                event.occurred_at,
                percent,
                (event.event_id,),
                ("budget:context-window",),
                True,
            )
            status = SignalStatus.ACTIVE
        else:
            streak = replace(
                streak,
                last_seen_at=event.occurred_at,
                count=percent,
                evidence_event_ids=(streak.evidence_event_ids + (event.event_id,))[
                    -_EVIDENCE_LIMIT:
                ],
            )
            status = SignalStatus.COOLED_DOWN
        self._budget_streaks[key] = streak
        candidate = _candidate(streak, status, event.event_id, state_version)
        return (
            replace(
                candidate,
                features=(
                    ("context_window_utilization_percent", percent),
                    ("model_context_window_tokens", window),
                    ("total_tokens", total),
                ),
            ),
        )

    def _update_duration_budget(
        self, event: TraceEvent, state_version: int
    ) -> tuple[SignalCandidate, ...]:
        if event.kind not in _OUTCOME_KINDS:
            return ()
        assert event.identity is not None and event.identity.thread_id is not None
        assert event.event_id is not None
        if event.identity.turn_id is None:
            return ()
        duration = _duration_ms(event.payload.get("durationMs"))
        equivalence, resources = _equivalence(event)
        if duration is None or equivalence is None or not _resources(event.payload):
            return ()
        thread_id = event.identity.thread_id
        baseline_key = (thread_id, f"duration:{equivalence}")
        baseline = self._duration_baselines.get(baseline_key)
        values = baseline.values_ms if baseline is not None else ()
        if len(values) < _BUDGET_BASELINE_SAMPLES or (reference := median(values)) <= 0:
            self._duration_baselines[baseline_key] = _DurationBaseline(
                event.identity,
                event.connection_epoch,
                (*values, duration)[-_BUDGET_BASELINE_LIMIT:],
            )
            return ()
        percent = min(_BUDGET_PERCENT_LIMIT, int(duration * 100 / reference))
        streak = self._budget_streaks.get(baseline_key)
        if percent < self.duration_anomaly_factor_percent:
            finished = (
                self._finish(streak, SignalStatus.RESOLVED, event, state_version)
                if streak is not None
                else ()
            )
            self._budget_streaks.pop(baseline_key, None)
            self._duration_baselines[baseline_key] = _DurationBaseline(
                event.identity,
                event.connection_epoch,
                (*values, duration)[-_BUDGET_BASELINE_LIMIT:],
            )
            return finished
        if streak is None:
            streak = _SignalStreak(
                _signal_id(event, SignalType.BUDGET_ANOMALY, baseline_key[1]),
                SignalType.BUDGET_ANOMALY,
                "duration_vs_median_percent",
                baseline_key[1],
                event.identity,
                event.connection_epoch,
                event.occurred_at,
                event.occurred_at,
                percent,
                (event.event_id,),
                ("budget:duration", *resources),
                True,
            )
            status = SignalStatus.ACTIVE
        else:
            streak = replace(
                streak,
                last_seen_at=event.occurred_at,
                count=percent,
                evidence_event_ids=(streak.evidence_event_ids + (event.event_id,))[
                    -_EVIDENCE_LIMIT:
                ],
            )
            status = SignalStatus.COOLED_DOWN
        self._budget_streaks[baseline_key] = streak
        candidate = _candidate(streak, status, event.event_id, state_version)
        return (
            replace(
                candidate,
                features=(
                    ("duration_vs_median_percent", percent),
                    ("duration_baseline_samples", len(values)),
                    ("duration_baseline_median_ms", round(reference)),
                ),
            ),
        )

    def _update_stale_hypothesis_reuse(
        self,
        event: TraceEvent,
        state_version: int,
        state: ThreadState | None,
    ) -> tuple[SignalCandidate, ...]:
        if state is None or event.kind not in _CAUSAL_ACTION_KINDS:
            return ()
        assert event.identity is not None and event.identity.thread_id is not None
        assert event.event_id is not None
        referenced = _hypothesis_ids(event.payload.get("hypothesis_ids"))
        if not referenced:
            return ()
        stale = state.evidence.stale_hypothesis_ids
        thread_id = event.identity.thread_id
        candidates: list[SignalCandidate] = []
        action_resources = _resources(event.payload)
        for hypothesis_id in sorted(set(referenced) & stale):
            key = (thread_id, hypothesis_id)
            streak = self._stale_hypothesis_reuses.get(key)
            resources = tuple(dict.fromkeys((f"hypothesis:{hypothesis_id}", *action_resources)))
            if streak is None:
                streak = _SignalStreak(
                    _signal_id(
                        event,
                        SignalType.STALE_HYPOTHESIS_REUSE,
                        hypothesis_id,
                    ),
                    SignalType.STALE_HYPOTHESIS_REUSE,
                    "causally_linked_actions",
                    hypothesis_id,
                    event.identity,
                    event.connection_epoch,
                    event.occurred_at,
                    event.occurred_at,
                    1,
                    (event.event_id,),
                    resources,
                    True,
                )
                status = SignalStatus.ACTIVE
            else:
                streak = replace(
                    streak,
                    last_seen_at=event.occurred_at,
                    count=streak.count + 1,
                    evidence_event_ids=(streak.evidence_event_ids + (event.event_id,))[
                        -_EVIDENCE_LIMIT:
                    ],
                    resources=tuple(dict.fromkeys((*streak.resources, *action_resources)))[
                        :_SCOPE_LIMIT
                    ],
                )
                status = SignalStatus.COOLED_DOWN
            self._stale_hypothesis_reuses[key] = streak
            candidates.append(_candidate(streak, status, event.event_id, state_version))
        return tuple(candidates)

    def _update_unvalidated_edits(
        self, event: TraceEvent, state_version: int
    ) -> tuple[SignalCandidate, ...]:
        assert event.identity is not None and event.identity.thread_id is not None
        assert event.event_id is not None
        thread_id = event.identity.thread_id
        streak = self._unvalidated_edits.get(thread_id)
        if event.kind == "test_result":
            if outcome_failure(event.payload) is not False:
                return ()
            scopes = _normalized_paths(event.payload.get("validated_paths"))
            if streak is None or not scopes:
                return ()
            remaining = tuple(
                resource
                for resource in streak.resources
                if not any(_within_scope(resource.removeprefix("file:"), scope) for scope in scopes)
            )
            if remaining == streak.resources:
                return ()
            sources = {
                resource: source_event_id
                for resource, source_event_id in self._unvalidated_sources.get(
                    thread_id, {}
                ).items()
                if resource in remaining
            }
            self._unvalidated_sources[thread_id] = sources
            evidence_event_ids = tuple(dict.fromkeys(sources.values()))[-_EVIDENCE_LIMIT:]
            if len(remaining) >= self.unvalidated_edit_threshold:
                streak = replace(
                    streak,
                    last_seen_at=event.occurred_at,
                    count=len(remaining),
                    evidence_event_ids=evidence_event_ids,
                    resources=remaining,
                )
                self._unvalidated_edits[thread_id] = streak
                return (
                    (_candidate(streak, SignalStatus.COOLED_DOWN, event.event_id, state_version),)
                    if streak.emitted
                    else ()
                )
            finished = self._finish(streak, SignalStatus.RESOLVED, event, state_version)
            if remaining:
                self._unvalidated_edits[thread_id] = _SignalStreak(
                    _signal_id(
                        event,
                        SignalType.EDITS_WITHOUT_VALIDATION,
                        "remaining-unvalidated-files",
                    ),
                    SignalType.EDITS_WITHOUT_VALIDATION,
                    "files_without_validation",
                    "unvalidated-files",
                    event.identity,
                    event.connection_epoch,
                    event.occurred_at,
                    event.occurred_at,
                    len(remaining),
                    evidence_event_ids,
                    remaining,
                )
            else:
                self._unvalidated_edits.pop(thread_id, None)
                self._unvalidated_sources.pop(thread_id, None)
            return finished
        if event.kind not in {"file_edit", "diff_updated"}:
            return ()
        if event.kind == "file_edit" and outcome_failure(event.payload) is not False:
            return ()
        resources = tuple(f"file:{path}" for path in _normalized_paths(event.payload.get("files")))
        if not resources:
            return ()
        known = set(streak.resources) if streak is not None else set()
        new_resources = set(resources) - known
        if not new_resources:
            return ()
        combined = (
            tuple((*streak.resources, *sorted(new_resources)))[:_SCOPE_LIMIT]
            if streak is not None
            else tuple(sorted(new_resources))[:_SCOPE_LIMIT]
        )
        if streak is not None and combined == streak.resources:
            return ()
        sources = self._unvalidated_sources.setdefault(thread_id, {})
        for resource in combined:
            if resource in new_resources:
                sources[resource] = event.event_id
        if streak is None:
            streak = _SignalStreak(
                _signal_id(event, SignalType.EDITS_WITHOUT_VALIDATION, "unvalidated-files"),
                SignalType.EDITS_WITHOUT_VALIDATION,
                "files_without_validation",
                "unvalidated-files",
                event.identity,
                event.connection_epoch,
                event.occurred_at,
                event.occurred_at,
                len(combined),
                (event.event_id,),
                combined,
            )
        else:
            streak = replace(
                streak,
                last_seen_at=event.occurred_at,
                count=len(combined),
                evidence_event_ids=(streak.evidence_event_ids + (event.event_id,))[
                    -_EVIDENCE_LIMIT:
                ],
                resources=combined,
            )
        self._unvalidated_edits[thread_id] = streak
        if streak.count < self.unvalidated_edit_threshold:
            return ()
        status = SignalStatus.COOLED_DOWN if streak.emitted else SignalStatus.ACTIVE
        candidate = _candidate(streak, status, event.event_id, state_version)
        if not streak.emitted:
            self._unvalidated_edits[thread_id] = replace(streak, emitted=True)
        return (candidate,)

    def _update_scope_growth(
        self, event: TraceEvent, state_version: int
    ) -> tuple[SignalCandidate, ...]:
        assert event.identity is not None and event.identity.thread_id is not None
        assert event.event_id is not None
        thread_id = event.identity.thread_id
        if event.kind == "user_prompt":
            growth = self._scope_growths.pop(thread_id, None)
            paths = _declared_paths(event.payload)
            if paths:
                self._declared_scopes[thread_id] = _DeclaredScope(
                    event.identity, event.connection_epoch, paths
                )
            else:
                self._declared_scopes.pop(thread_id, None)
            return (
                self._finish(growth, SignalStatus.STALE, event, state_version)
                if growth is not None
                else ()
            )
        if event.kind not in {"file_edit", "diff_updated"}:
            return ()
        if event.kind == "file_edit" and outcome_failure(event.payload) is not False:
            return ()
        scope = self._declared_scopes.get(thread_id)
        if scope is None:
            return ()
        files = _normalized_paths(event.payload.get("files"))
        outside = {
            f"file:{path}"
            for path in files
            if not any(_within_scope(path, expected) for expected in scope.paths)
        }
        if not outside:
            return ()
        growth = self._scope_growths.get(thread_id)
        known = set(growth.resources) if growth is not None else set()
        new_resources = outside - known
        if not new_resources:
            return ()
        resources = (
            tuple((*growth.resources, *sorted(new_resources)))[:_SCOPE_LIMIT]
            if growth
            else tuple(sorted(new_resources))[:_SCOPE_LIMIT]
        )
        if growth is not None and resources == growth.resources:
            return ()
        if growth is None:
            growth = _SignalStreak(
                _signal_id(event, SignalType.TOUCHED_SCOPE_GROWTH, "outside-declared-scope"),
                SignalType.TOUCHED_SCOPE_GROWTH,
                "files_outside_declared_scope",
                "outside-declared-scope",
                event.identity,
                event.connection_epoch,
                event.occurred_at,
                event.occurred_at,
                len(resources),
                (event.event_id,),
                resources,
            )
        else:
            growth = replace(
                growth,
                last_seen_at=event.occurred_at,
                count=len(resources),
                evidence_event_ids=(growth.evidence_event_ids + (event.event_id,))[
                    -_EVIDENCE_LIMIT:
                ],
                resources=resources,
            )
        self._scope_growths[thread_id] = growth
        if growth.count < self.scope_growth_threshold:
            return ()
        status = SignalStatus.COOLED_DOWN if growth.emitted else SignalStatus.ACTIVE
        candidate = _candidate(growth, status, event.event_id, state_version)
        if not growth.emitted:
            self._scope_growths[thread_id] = replace(growth, emitted=True)
        return (candidate,)

    def _update_block_recurrence(
        self, event: TraceEvent, state_version: int
    ) -> tuple[SignalCandidate, ...]:
        if event.kind != "deterministic_gate_block":
            return ()
        assert event.identity is not None and event.identity.thread_id is not None
        assert event.event_id is not None
        key = event.payload.get("equivalence_key")
        raw_resources = event.payload.get("involved_resources")
        if not isinstance(key, str) or not key or not isinstance(raw_resources, list):
            return ()
        resources = tuple(
            sorted(
                {resource for resource in raw_resources if isinstance(resource, str) and resource}
            )
        )
        if not resources:
            return ()

        thread_id = event.identity.thread_id
        blocked_key = (thread_id, key)
        blocked = self._blocked_actions.get(blocked_key)
        candidates: list[SignalCandidate] = []
        if blocked is None:
            thread_keys = [item for item in self._blocked_actions if item[0] == thread_id]
            if len(thread_keys) >= _BLOCK_LIMIT:
                evicted = self._blocked_actions.pop(thread_keys[0])
                candidates.extend(self._finish(evicted, SignalStatus.STALE, event, state_version))
            blocked = _SignalStreak(
                _signal_id(event, SignalType.RECURRENCE_AFTER_DETERMINISTIC_BLOCK, key),
                SignalType.RECURRENCE_AFTER_DETERMINISTIC_BLOCK,
                "equivalent_blocked_attempts",
                key,
                event.identity,
                event.connection_epoch,
                event.occurred_at,
                event.occurred_at,
                1,
                (event.event_id,),
                resources,
            )
        else:
            blocked = replace(
                blocked,
                last_seen_at=event.occurred_at,
                count=blocked.count + 1,
                evidence_event_ids=(blocked.evidence_event_ids + (event.event_id,))[
                    -_EVIDENCE_LIMIT:
                ],
            )
        self._blocked_actions[blocked_key] = blocked
        if blocked.count >= 2:
            status = SignalStatus.COOLED_DOWN if blocked.emitted else SignalStatus.ACTIVE
            candidates.append(_candidate(blocked, status, event.event_id, state_version))
            if not blocked.emitted:
                self._blocked_actions[blocked_key] = replace(blocked, emitted=True)
        return tuple(candidates)

    def _update_repeated_tool(
        self, event: TraceEvent, state_version: int
    ) -> tuple[SignalCandidate, ...]:
        assert event.identity is not None and event.identity.thread_id is not None
        assert event.event_id is not None
        thread_id = event.identity.thread_id
        repeated = self._repeated_tools.get(thread_id)
        if event.kind != "tool_result":
            if repeated is None or event.kind not in _OUTCOME_KINDS:
                return ()
            self._repeated_tools.pop(thread_id, None)
            return self._finish(repeated, SignalStatus.STALE, event, state_version)

        if outcome_failure(event.payload) is not False:
            if repeated is None:
                return ()
            self._repeated_tools.pop(thread_id, None)
            return self._finish(repeated, SignalStatus.STALE, event, state_version)

        key, resources = _tool_call_equivalence(event)
        candidates: list[SignalCandidate] = []
        if key is None:
            if repeated is not None:
                candidates.extend(self._finish(repeated, SignalStatus.STALE, event, state_version))
                self._repeated_tools.pop(thread_id, None)
            return tuple(candidates)
        if repeated is not None and repeated.key != key:
            candidates.extend(self._finish(repeated, SignalStatus.STALE, event, state_version))
            repeated = None

        if repeated is None:
            repeated = _SignalStreak(
                _signal_id(event, SignalType.REPEATED_EQUIVALENT_TOOL_CALL, key),
                SignalType.REPEATED_EQUIVALENT_TOOL_CALL,
                "consecutive_equivalent_calls",
                key,
                event.identity,
                event.connection_epoch,
                event.occurred_at,
                event.occurred_at,
                1,
                (event.event_id,),
                resources,
            )
        else:
            repeated = replace(
                repeated,
                last_seen_at=event.occurred_at,
                count=repeated.count + 1,
                evidence_event_ids=(repeated.evidence_event_ids + (event.event_id,))[
                    -_EVIDENCE_LIMIT:
                ],
            )
        self._repeated_tools[thread_id] = repeated
        if repeated.count >= self.repeated_tool_threshold:
            status = SignalStatus.COOLED_DOWN if repeated.emitted else SignalStatus.ACTIVE
            candidates.append(_candidate(repeated, status, event.event_id, state_version))
            if not repeated.emitted:
                self._repeated_tools[thread_id] = replace(repeated, emitted=True)
        return tuple(candidates)

    def _update_no_frontier_reads(
        self, event: TraceEvent, state_version: int
    ) -> tuple[SignalCandidate, ...]:
        assert event.identity is not None and event.identity.thread_id is not None
        assert event.event_id is not None
        thread_id = event.identity.thread_id
        reading = self._read_streaks.get(thread_id)
        observation = _read_observation(event)
        if observation is None:
            if event.kind not in _FRONTIER_RESET_KINDS and event.kind != "tool_result":
                return ()
            if reading is None:
                return ()
            self._read_streaks.pop(thread_id, None)
            return self._finish(reading, SignalStatus.STALE, event, state_version)

        resources, observation_key = observation
        frontier = self._read_frontiers.get(thread_id)
        known = frontier.observations if frontier is not None else frozenset()
        if observation_key not in known:
            self._read_frontiers[thread_id] = _ReadFrontier(
                event.identity,
                event.connection_epoch,
                frozenset((*sorted(known)[-(_FRONTIER_LIMIT - 1) :], observation_key)),
            )
            if reading is None:
                return ()
            self._read_streaks.pop(thread_id, None)
            return self._finish(reading, SignalStatus.STALE, event, state_version)

        if reading is None:
            reading = _SignalStreak(
                _signal_id(event, SignalType.REPEATED_READ_NO_FRONTIER, "known-resources"),
                SignalType.REPEATED_READ_NO_FRONTIER,
                "reads_without_frontier_expansion",
                "known-resources",
                event.identity,
                event.connection_epoch,
                event.occurred_at,
                event.occurred_at,
                1,
                (event.event_id,),
                resources,
            )
        else:
            reading = replace(
                reading,
                last_seen_at=event.occurred_at,
                count=reading.count + 1,
                evidence_event_ids=(reading.evidence_event_ids + (event.event_id,))[
                    -_EVIDENCE_LIMIT:
                ],
                resources=tuple(sorted(set(reading.resources).union(resources))),
            )
        self._read_streaks[thread_id] = reading
        if reading.count < self.no_frontier_threshold:
            return ()
        status = SignalStatus.COOLED_DOWN if reading.emitted else SignalStatus.ACTIVE
        candidate = _candidate(reading, status, event.event_id, state_version)
        if not reading.emitted:
            self._read_streaks[thread_id] = replace(reading, emitted=True)
        return (candidate,)

    def hydrate(
        self, records: Iterable[StepRecord]
    ) -> tuple[tuple[SignalCandidate, TraceEvent], ...]:
        """Rebuild state and return derived events missing after an interrupted append."""

        self._failure_streaks.clear()
        self._repeated_tools.clear()
        self._read_streaks.clear()
        self._read_frontiers.clear()
        self._blocked_actions.clear()
        self._declared_scopes.clear()
        self._scope_growths.clear()
        self._unvalidated_edits.clear()
        self._unvalidated_sources.clear()
        self._stale_hypothesis_reuses.clear()
        self._budget_streaks.clear()
        self._duration_baselines.clear()
        self._seen_event_ids.clear()
        states = ThreadStateStore()
        pending: dict[str, tuple[SignalCandidate, TraceEvent]] = {}
        for record in records:
            event = record.event
            identity = event.identity
            if identity is None or identity.thread_id is None:
                continue
            state = states.observe(event)
            if event.kind in {"signal_candidate", "signal_candidate_suppressed"}:
                if event.event_id is not None:
                    pending.pop(event.event_id, None)
                continue
            for candidate in self.update(event, state.version, state):
                derived = candidate.to_trace_event(event)
                assert derived.event_id is not None
                pending[derived.event_id] = (candidate, event)
        return tuple(pending.values())

    def _finish(
        self,
        streak: _SignalStreak,
        status: SignalStatus,
        event: TraceEvent,
        state_version: int,
    ) -> tuple[SignalCandidate, ...]:
        if not streak.emitted or event.event_id is None:
            return ()
        return (_candidate(streak, status, event.event_id, state_version),)


def _candidate(
    streak: _SignalStreak,
    status: SignalStatus,
    source_event_id: str,
    state_version: int,
) -> SignalCandidate:
    turn_id = streak.identity.turn_id.value if streak.identity.turn_id is not None else None
    assert streak.identity.thread_id is not None
    return SignalCandidate(
        streak.signal_id,
        streak.signal_type,
        streak.identity.thread_id,
        turn_id,
        streak.connection_epoch,
        state_version,
        streak.first_seen_at,
        streak.last_seen_at,
        streak.count,
        streak.evidence_event_ids,
        streak.resources,
        status,
        source_event_id,
        ((streak.feature, streak.count),),
    )


def _target_changed(
    streak: _SignalStreak | _ReadFrontier | _DeclaredScope | _DurationBaseline,
    event: TraceEvent,
) -> bool:
    identity = event.identity
    return bool(
        identity is not None
        and (
            identity.turn_id != streak.identity.turn_id
            or (
                event.connection_epoch is not None
                and streak.connection_epoch is not None
                and event.connection_epoch != streak.connection_epoch
            )
        )
    )


def _equivalence(event: TraceEvent) -> tuple[str | None, tuple[str, ...]]:
    resources = _resources(event.payload)
    if event.kind == "test_result" and not resources:
        resources = ("validation",)
    if not resources:
        return None, ()
    family = {
        "command_result": "command",
        "file_edit": "file_change",
        "test_result": "test",
        "tool_result": "tool",
    }[event.kind]
    return f"{family}:{'|'.join(resources)}", resources


def _resources(payload: Mapping[str, object]) -> tuple[str, ...]:
    resources: set[str] = set()
    files = payload.get("files")
    if isinstance(files, list):
        resources.update(f"file:{value}" for value in files if isinstance(value, str) and value)
    resource = payload.get("resource") or payload.get("scope")
    if isinstance(resource, str) and resource:
        resources.add(f"resource:{resource}")
    server = payload.get("server") or payload.get("namespace")
    tool = payload.get("tool")
    if isinstance(server, str) and server and isinstance(tool, str) and tool:
        resources.add(f"tool:{server}/{tool}")
    return tuple(sorted(resources))


def deterministic_block_equivalence(
    rule: object, proposal: Mapping[str, object]
) -> tuple[str, tuple[str, ...]] | None:
    """Return a conservative semantic key for one deterministic gate rejection."""

    if not isinstance(rule, str) or not rule:
        return None
    files = proposal.get("files")
    resources = (
        tuple(sorted({f"file:{path}" for path in files if isinstance(path, str) and path}))
        if isinstance(files, list)
        else ()
    )
    if not resources:
        # The rule is the only proven semantic operation for command gates.
        # In particular this keeps supported shell wrappers equivalent without
        # guessing at arbitrary command text.
        resources = (f"gate-rule:{rule}",)
    return f"{rule}:{'|'.join(resources)}", resources


def _declared_paths(payload: Mapping[str, object]) -> tuple[str, ...]:
    expected = _normalized_paths(payload.get("expected_paths"))
    prompt = payload.get("prompt")
    texts = [prompt] if isinstance(prompt, str) else []
    content = payload.get("content")
    if isinstance(content, list):
        texts.extend(
            part["text"]
            for part in content
            if isinstance(part, Mapping) and isinstance(part.get("text"), str)
        )
    if sum(len(text) for text in texts) > _SCOPE_TEXT_LIMIT:
        return expected
    for text in texts:
        for raw in text.split():
            token = raw.strip("`'\"()[]{}<>,:;").rstrip(".")
            if "/" in token and _PATH_CHARS.fullmatch(token):
                expected = tuple(sorted(set(expected) | set(_normalized_paths([token]))))
    return expected


def _normalized_paths(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > _SCOPE_LIMIT:
        return ()
    paths: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        path = value.strip().removeprefix("./")
        if (
            not path
            or path.startswith(("/", "~", "../"))
            or "://" in path
            or not _PATH_CHARS.fullmatch(path)
            or ".." in path.split("/")
        ):
            continue
        paths.add(path.rstrip("/") or path)
    return tuple(sorted(paths))


def _hypothesis_ids(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > _HYPOTHESIS_LIMIT:
        return ()
    return tuple(
        dict.fromkeys(
            value.strip()
            for value in raw
            if isinstance(value, str)
            and value.strip()
            and len(value.strip()) <= _HYPOTHESIS_ID_LIMIT
        )
    )


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _duration_ms(value: object) -> float | None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    duration = float(value)
    return duration if duration >= 0 and math.isfinite(duration) else None


def _within_scope(path: str, expected: str) -> bool:
    return path == expected or path.startswith(f"{expected.rstrip('/')}/")


def _tool_call_equivalence(event: TraceEvent) -> tuple[str | None, tuple[str, ...]]:
    resources = _resources(event.payload)
    if len(resources) != 1 or not resources[0].startswith("tool:"):
        return None, ()
    try:
        arguments = json.dumps(
            event.payload.get("arguments", {}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None, ()
    digest = hashlib.sha256(arguments.encode()).hexdigest()
    return f"{resources[0]}:{digest}", resources


def _read_observation(event: TraceEvent) -> tuple[tuple[str, ...], str] | None:
    if event.kind != "tool_result" or outcome_failure(event.payload) is not False:
        return None
    tool = event.payload.get("tool")
    if not isinstance(tool, str):
        return None
    name = tool.rsplit("__", 1)[-1].rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()
    if not any(name == verb or name.startswith((f"{verb}_", f"{verb}-")) for verb in _READ_VERBS):
        return None
    arguments = event.payload.get("arguments")
    if not isinstance(arguments, Mapping):
        return None
    resources = tuple(
        sorted(
            {
                f"{kind}:{value.strip()}"
                for key, value in arguments.items()
                if (kind := _READ_RESOURCE_KEYS.get(str(key).lower())) is not None
                and isinstance(value, str)
                and value.strip()
            }
        )
    )
    if not resources:
        return None
    evidence = (
        event.payload["result"] if "result" in event.payload else event.payload.get("contentItems")
    )
    if "result" not in event.payload and "contentItems" not in event.payload:
        return None
    try:
        observation = json.dumps(
            (resources, evidence, arguments),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return None
    return resources, hashlib.sha256(observation.encode()).hexdigest()


def _signal_id(event: TraceEvent, signal_type: SignalType, key: str) -> str:
    assert event.identity is not None and event.identity.thread_id is not None
    turn_id = event.identity.turn_id.value if event.identity.turn_id is not None else "unknown"
    return hashlib.sha256(
        (
            f"{event.identity.thread_id.value}:{turn_id}:{event.connection_epoch}:"
            f"{signal_type}:{key}:{event.event_id}"
        ).encode()
    ).hexdigest()
