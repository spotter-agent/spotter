"""Immutable live supervision state reduced from runtime-neutral Trace IR."""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from spotter.identity import RuntimeIdentity, ThreadId, TurnId
from spotter.outcomes import outcome_failure
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent

_RECENT_LIMIT = 50
_VALIDATION_SCOPE_LIMIT = 128
_VALIDATION_PATH_CHARS = re.compile(r"[A-Za-z0-9_./-]+")


class StateItemKind(StrEnum):
    GOAL = "goal"
    CONSTRAINT = "constraint"
    OBSERVATION = "observation"
    HYPOTHESIS = "hypothesis"
    VERIFIED_FACT = "verified_fact"
    SUMMARY = "summary"
    INTERVENTION = "intervention"


class StateItemStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class HistoryStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ConditionStatus(StrEnum):
    UNMET = "unmet"
    SATISFIED = "satisfied"
    INVALIDATED = "invalidated"
    UNKNOWN = "unknown"


class ValidationStatus(StrEnum):
    UNKNOWN = "unknown"
    PASSED = "passed"
    FAILED = "failed"
    STALE = "stale"


@dataclass(frozen=True)
class StateProvenance:
    event_id: str | None
    created_at: float | None
    thread_id: ThreadId
    turn_id: TurnId | None
    connection_epoch: int | None
    source: str | None
    config_generation: str | None


@dataclass(frozen=True)
class StateItem:
    id: str
    kind: StateItemKind
    text: str
    provenance: StateProvenance
    status: StateItemStatus = StateItemStatus.ACTIVE
    confidence: float | None = None
    depends_on: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationCondition:
    id: str
    kind: str
    scope: str | None
    required_evidence: tuple[str, ...]
    status: ConditionStatus
    satisfied_by: tuple[str, ...]
    created_from: str
    provenance: StateProvenance


@dataclass(frozen=True)
class ObservationGap:
    started_at: float | None
    ended_at: float | None
    epoch_before: int | None
    epoch_after: int | None
    backfill_status: str
    source_event_id: str | None


@dataclass(frozen=True)
class TaskState:
    goal: StateItem | None = None
    constraints: tuple[StateItem, ...] = ()


@dataclass(frozen=True)
class EvidenceState:
    # Evidence remains addressable for later verification/invalidation. #89 may compact
    # only after persisted conditions and dependencies gain the same retention boundary.
    items: tuple[StateItem, ...] = ()
    conditions: tuple[VerificationCondition, ...] = ()
    stale_hypothesis_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class WorkspaceState:
    repository: str | None = None
    worktree: str | None = None
    touched_files: frozenset[str] = field(default_factory=frozenset)
    edits_since_validation: frozenset[str] = field(default_factory=frozenset)
    checkpoint: str | None = None


@dataclass(frozen=True)
class ExecutionState:
    plan_summary: StateItem | None = None
    terminal_answer: StateItem | None = None
    active_items: frozenset[str] = field(default_factory=frozenset)
    completed_turns: frozenset[TurnId] = field(default_factory=frozenset)
    recent_outcomes: tuple[StateItem, ...] = ()
    recent_failures: tuple[StateItem, ...] = ()
    validation: ValidationStatus = ValidationStatus.UNKNOWN


@dataclass(frozen=True)
class SupervisionState:
    interventions: tuple[StateItem, ...] = ()


@dataclass(frozen=True)
class CoverageState:
    history: HistoryStatus = HistoryStatus.UNKNOWN
    gaps: tuple[ObservationGap, ...] = ()
    unknown_event_count: int = 0
    last_trustworthy_event_id: str | None = None
    trajectory_complete_since: float | None = None
    inconsistencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class ThreadState:
    identity: RuntimeIdentity
    version: int = 0
    last_trace_event_id: str | None = None
    last_arrival_seq: int = 0
    connection_epoch: int | None = None
    active_turn_id: TurnId | None = None
    capabilities: tuple[str, ...] = ()
    control_ready: bool = False
    task: TaskState = field(default_factory=TaskState)
    evidence: EvidenceState = field(default_factory=EvidenceState)
    workspace: WorkspaceState = field(default_factory=WorkspaceState)
    execution: ExecutionState = field(default_factory=ExecutionState)
    supervision: SupervisionState = field(default_factory=SupervisionState)
    coverage: CoverageState = field(default_factory=CoverageState)

    @property
    def thread_id(self) -> ThreadId:
        assert self.identity.thread_id is not None
        return self.identity.thread_id


class ThreadStateError(ValueError):
    """A Trace event cannot be routed or reduced without inventing identity."""


class ThreadStateReducer:
    """Pure deterministic reducer; transport callbacks never mutate state directly."""

    def reduce(self, state: ThreadState | None, event: TraceEvent, arrival_seq: int) -> ThreadState:
        identity = event.identity
        if identity is None or identity.thread_id is None:
            raise ThreadStateError("Trace event has no logical thread identity")
        if state is not None and state.thread_id != identity.thread_id:
            raise ThreadStateError("Trace event belongs to another thread")
        if arrival_seq <= 0 or (state is not None and arrival_seq <= state.last_arrival_seq):
            raise ThreadStateError("arrival sequence must increase monotonically")

        current = state or ThreadState(identity=identity)
        epoch_changed = (
            event.connection_epoch is not None
            and current.connection_epoch is not None
            and event.connection_epoch != current.connection_epoch
        )
        current = replace(
            current,
            identity=identity,
            version=current.version + 1,
            last_trace_event_id=event.event_id,
            last_arrival_seq=arrival_seq,
            connection_epoch=(
                event.connection_epoch
                if event.connection_epoch is not None
                else current.connection_epoch
            ),
            active_turn_id=None if epoch_changed else current.active_turn_id,
            control_ready=False if epoch_changed else current.control_ready,
        )
        current = self._lifecycle(current, event)
        current = self._semantic(current, event)
        return self._coverage(current, event)

    def _lifecycle(self, state: ThreadState, event: TraceEvent) -> ThreadState:
        turn_id = event.identity.turn_id if event.identity else None
        if event.kind == "turn_started" and turn_id is not None:
            if turn_id in state.execution.completed_turns:
                return _inconsistent(state, f"late_start_for_completed_turn:{turn_id.value}")
            if state.active_turn_id is not None and state.active_turn_id != turn_id:
                state = _inconsistent(
                    state,
                    f"turn_started_while_active:{state.active_turn_id.value}:{turn_id.value}",
                )
            terminal_answer = state.execution.terminal_answer
            if terminal_answer is not None and terminal_answer.provenance.turn_id != turn_id:
                terminal_answer = None
            return replace(
                state,
                active_turn_id=turn_id,
                execution=replace(state.execution, terminal_answer=terminal_answer),
            )
        if event.kind == "turn_completed" and turn_id is not None:
            if state.active_turn_id != turn_id:
                state = _inconsistent(state, f"turn_completed_without_start:{turn_id.value}")
            execution = replace(
                state.execution,
                active_items=frozenset(),
                completed_turns=state.execution.completed_turns | {turn_id},
            )
            return replace(state, active_turn_id=None, control_ready=False, execution=execution)
        if event.kind in {"thread_archived", "thread_closed", "thread_deleted"}:
            return replace(state, active_turn_id=None, control_ready=False)
        if event.kind == "runtime_attachment_unavailable":
            return replace(
                state,
                active_turn_id=None,
                capabilities=(),
                control_ready=False,
            )
        # runtime_reconciled is produced by #87. The semantic-only kinds below are
        # reserved for later signal/reviewer producers; Trace IR keeps them transport-neutral.
        if event.kind == "runtime_reconciled":
            capabilities = event.payload.get("capabilities")
            active_turn_value = event.payload.get("active_turn")
            active_turn = (
                turn_id
                if active_turn_value is True
                else None
                if active_turn_value is False
                else state.active_turn_id
            )
            return replace(
                state,
                capabilities=(
                    tuple(sorted(value for value in capabilities if isinstance(value, str)))
                    if isinstance(capabilities, list)
                    else state.capabilities
                ),
                active_turn_id=active_turn,
                control_ready=active_turn is not None,
            )
        return state

    def _semantic(self, state: ThreadState, event: TraceEvent) -> ThreadState:
        provenance = _provenance(state.thread_id, event)
        item_id = event.event_id or f"arrival:{state.last_arrival_seq}"
        state = replace(
            state,
            workspace=_workspace(state.workspace, event, state.workspace.edits_since_validation),
        )
        if event.kind == "user_prompt":
            text = _user_text(event.payload)
            if event.payload.get("input_origin") == "spotter_supervision":
                if not text:
                    return state
                intervention = StateItem(
                    item_id,
                    StateItemKind.INTERVENTION,
                    text,
                    provenance,
                )
                return replace(
                    state,
                    supervision=replace(
                        state.supervision,
                        interventions=_bounded(state.supervision.interventions + (intervention,)),
                    ),
                )
            if text:
                goal = StateItem(item_id, StateItemKind.GOAL, text, provenance)
                previous = state.task.goal
                evidence = state.evidence
                if previous is not None:
                    evidence = replace(
                        evidence,
                        items=evidence.items
                        + (replace(previous, status=StateItemStatus.SUPERSEDED),),
                    )
                return replace(state, task=replace(state.task, goal=goal), evidence=evidence)
        if event.kind == "constraint":
            text = _optional_text(event.payload.get("text"))
            if text:
                constraint = StateItem(item_id, StateItemKind.CONSTRAINT, text, provenance)
                return replace(
                    state,
                    task=replace(state.task, constraints=state.task.constraints + (constraint,)),
                )
        if event.kind in {"reasoning_summary", "hypothesis"}:
            text = _summary_text(event.payload)
            if text:
                depends_on = _string_tuple(event.payload.get("depends_on"))
                evidence_ids = _string_tuple(event.payload.get("evidence_ids"))
                stale_ids = state.evidence.stale_hypothesis_ids
                is_stale = bool(stale_ids & set((*depends_on, *evidence_ids)))
                hypothesis = StateItem(
                    item_id,
                    StateItemKind.HYPOTHESIS,
                    text,
                    provenance,
                    status=StateItemStatus.STALE if is_stale else StateItemStatus.ACTIVE,
                    depends_on=depends_on,
                    evidence_ids=evidence_ids,
                )
                return replace(
                    state,
                    evidence=replace(
                        state.evidence,
                        items=state.evidence.items + (hypothesis,),
                        stale_hypothesis_ids=(stale_ids | {item_id} if is_stale else stale_ids),
                    ),
                )
        if event.kind == "plan":
            text = _summary_text(event.payload)
            if text:
                summary = StateItem(item_id, StateItemKind.SUMMARY, text, provenance)
                return replace(state, execution=replace(state.execution, plan_summary=summary))
        if (
            event.kind == "agent_message"
            and event.payload.get("phase") == "final_answer"
            and event.payload.get("lifecycle") == "completed"
        ):
            text = _optional_text(event.payload.get("text"))
            if text:
                answer = StateItem(item_id, StateItemKind.SUMMARY, text, provenance)
                return replace(
                    state,
                    execution=replace(state.execution, terminal_answer=answer),
                )
        if event.kind == "verification_condition":
            condition_id = _optional_text(event.payload.get("condition_id"))
            kind = _optional_text(event.payload.get("kind"))
            if condition_id and kind:
                condition = VerificationCondition(
                    condition_id,
                    kind,
                    _optional_text(event.payload.get("scope")),
                    _string_tuple(event.payload.get("required_evidence")),
                    ConditionStatus.UNMET,
                    (),
                    _optional_text(event.payload.get("created_from")) or "unknown",
                    provenance,
                )
                existing = next(
                    (item for item in state.evidence.conditions if item.id == condition_id),
                    None,
                )
                if existing is not None:
                    return (
                        state
                        if existing == condition
                        else _inconsistent(
                            state, f"conflicting_verification_condition:{condition_id}"
                        )
                    )
                return replace(
                    state,
                    evidence=replace(
                        state.evidence, conditions=state.evidence.conditions + (condition,)
                    ),
                )
        if event.kind == "verification_satisfied":
            return _satisfy_condition(state, event, provenance, item_id)
        if event.kind == "evidence_invalidated":
            return _invalidate_evidence(state, event.payload.get("evidence_id"))

        active = state.execution.active_items
        operation_id = event.operation_id or event.item_id
        if event.kind in {"command_started", "tool_started", "file_change_started"}:
            if operation_id:
                active = active | {operation_id}
            return replace(state, execution=replace(state.execution, active_items=active))
        if event.kind in {
            "command_result",
            "tool_result",
            "file_edit",
            "diff_updated",
            "search",
            "test_result",
        }:
            text = _outcome_text(event)
            observation = StateItem(item_id, StateItemKind.OBSERVATION, text, provenance)
            if operation_id:
                active = active - {operation_id}
            outcomes = _bounded(state.execution.recent_outcomes + (observation,))
            failures = state.execution.recent_failures
            if _failed(event.payload):
                failures = _bounded(failures + (observation,))
            validation = state.execution.validation
            edits = state.workspace.edits_since_validation
            if (
                event.kind in {"file_edit", "diff_updated"}
                and validation == ValidationStatus.PASSED
            ):
                validation = ValidationStatus.STALE
            if event.kind == "test_result":
                validation = _validation_status(event.payload)
                if validation == ValidationStatus.PASSED:
                    scopes = _validation_scopes(event.payload)
                    edits = frozenset(
                        path
                        for path in edits
                        if not any(_within_scope(path, scope) for scope in scopes)
                    )
            workspace = _workspace(state.workspace, event, edits)
            return replace(
                state,
                evidence=replace(state.evidence, items=state.evidence.items + (observation,)),
                workspace=workspace,
                execution=replace(
                    state.execution,
                    active_items=active,
                    recent_outcomes=outcomes,
                    recent_failures=failures,
                    validation=validation,
                ),
            )
        if event.kind in {"reviewer_decision", "intervention", "gate_block"}:
            intervention = StateItem(
                item_id,
                StateItemKind.INTERVENTION,
                _outcome_text(event),
                provenance,
            )
            return replace(
                state,
                supervision=replace(
                    state.supervision,
                    interventions=_bounded(state.supervision.interventions + (intervention,)),
                ),
            )
        return state

    def _coverage(self, state: ThreadState, event: TraceEvent) -> ThreadState:
        coverage = state.coverage
        if event.kind == "observation_gap":
            gap = ObservationGap(
                _number(event.payload.get("started_at")),
                _number(event.payload.get("ended_at")),
                _integer(event.payload.get("epoch_before")),
                _integer(event.payload.get("epoch_after")),
                _optional_text(event.payload.get("backfill_status")) or "none",
                event.event_id,
            )
            return replace(
                state,
                control_ready=False,
                coverage=replace(
                    coverage, history=HistoryStatus.PARTIAL, gaps=coverage.gaps + (gap,)
                ),
            )
        if event.kind == "runtime_event_unknown":
            coverage = replace(coverage, unknown_event_count=coverage.unknown_event_count + 1)
        if event.payload.get("out_of_order") is True:
            state = _inconsistent(state, f"out_of_order:{event.event_id or state.last_arrival_seq}")
            coverage = state.coverage
        if event.kind == "thread_started" and coverage.history == HistoryStatus.UNKNOWN:
            coverage = replace(
                state.coverage,
                history=HistoryStatus.COMPLETE,
                trajectory_complete_since=event.occurred_at,
            )
        if event.event_id is not None and event.payload.get("out_of_order") is not True:
            coverage = replace(coverage, last_trustworthy_event_id=event.event_id)
        return replace(state, coverage=coverage)


class ThreadStateStore:
    """Single-owner in-memory store intended to live inside the daemon event loop."""

    def __init__(self, reducer: ThreadStateReducer | None = None) -> None:
        self.reducer = reducer or ThreadStateReducer()
        self._states: dict[ThreadId, ThreadState] = {}
        # Exact dedup belongs to the daemon's mutable store, not every immutable snapshot.
        # ponytail: IDs remain for the thread lifetime; #89 may compact them only at a
        # durable replay boundary.
        self._seen_event_ids: dict[ThreadId, set[str]] = {}
        self._arrival_seq = 0

    def observe(self, event: TraceEvent) -> ThreadState:
        if event.identity is None or event.identity.thread_id is None:
            raise ThreadStateError("Trace event has no logical thread identity")
        thread_id = event.identity.thread_id
        seen = self._seen_event_ids.setdefault(thread_id, set())
        if event.event_id is not None and event.event_id in seen:
            return self.snapshot(thread_id)
        self._arrival_seq += 1
        state = self.reducer.reduce(self._states.get(thread_id), event, self._arrival_seq)
        self._states[thread_id] = state
        if event.event_id is not None:
            seen.add(event.event_id)
        return state

    def snapshot(self, thread_id: ThreadId) -> ThreadState:
        try:
            return self._states[thread_id]
        except KeyError as error:
            raise ThreadStateError(f"unknown thread: {thread_id.value}") from error

    def snapshots(self) -> tuple[ThreadState, ...]:
        return tuple(self._states.values())

    def replay(self, events: Iterable[TraceEvent]) -> tuple[ThreadState, ...]:
        for event in events:
            self.observe(event)
        return self.snapshots()

    def hydrate(self, records: Iterable[StepRecord]) -> tuple[ThreadState, ...]:
        """Replay durable history but never claim old live control identity is ready."""

        self.replay(
            record.event
            for record in records
            if record.event.identity is not None and record.event.identity.thread_id is not None
        )
        for thread_id, state in self._states.items():
            self._states[thread_id] = replace(state, active_turn_id=None, control_ready=False)
        return self.snapshots()


def _provenance(thread_id: ThreadId, event: TraceEvent) -> StateProvenance:
    source = event.provenance.source if event.provenance else None
    turn_id = event.identity.turn_id if event.identity else None
    return StateProvenance(
        event.event_id,
        event.occurred_at,
        thread_id,
        turn_id,
        event.connection_epoch,
        source,
        event.config_generation,
    )


def _bounded(items: tuple[StateItem, ...]) -> tuple[StateItem, ...]:
    return items[-_RECENT_LIMIT:]


def _inconsistent(state: ThreadState, message: str) -> ThreadState:
    if message in state.coverage.inconsistencies:
        return state
    return replace(
        state,
        coverage=replace(
            state.coverage,
            history=HistoryStatus.PARTIAL,
            inconsistencies=state.coverage.inconsistencies + (message,),
        ),
    )


def _workspace(
    workspace: WorkspaceState, event: TraceEvent, edits: frozenset[str]
) -> WorkspaceState:
    files = event.payload.get("files")
    observed_files = (
        frozenset(value for value in files if isinstance(value, str))
        if isinstance(files, list)
        else frozenset()
    )
    if event.kind in {"file_edit", "diff_updated"}:
        edits = edits | observed_files
    return replace(
        workspace,
        repository=_optional_text(event.payload.get("repository")) or workspace.repository,
        worktree=(
            _optional_text(event.payload.get("worktree"))
            or _optional_text(event.payload.get("cwd"))
            or workspace.worktree
        ),
        touched_files=workspace.touched_files | observed_files,
        edits_since_validation=edits,
        checkpoint=_optional_text(event.payload.get("checkpoint")) or workspace.checkpoint,
    )


def _satisfy_condition(
    state: ThreadState,
    event: TraceEvent,
    provenance: StateProvenance,
    item_id: str,
) -> ThreadState:
    condition_id = _optional_text(event.payload.get("condition_id"))
    evidence_id = _optional_text(event.payload.get("evidence_id"))
    if condition_id is None or evidence_id is None:
        return _inconsistent(state, "verification_satisfied_without_identity")
    evidence = next(
        (
            item
            for item in state.evidence.items
            if item.id == evidence_id
            and item.kind == StateItemKind.OBSERVATION
            and item.status == StateItemStatus.ACTIVE
        ),
        None,
    )
    if evidence is None:
        return _inconsistent(state, f"unknown_verification_evidence:{evidence_id}")
    conditions = []
    matched = False
    for condition in state.evidence.conditions:
        if condition.id == condition_id:
            matched = True
            condition = replace(
                condition,
                status=ConditionStatus.SATISFIED,
                satisfied_by=tuple(dict.fromkeys(condition.satisfied_by + (evidence_id,))),
            )
        conditions.append(condition)
    if not matched:
        return _inconsistent(state, f"unknown_verification_condition:{condition_id}")
    fact = StateItem(
        item_id,
        StateItemKind.VERIFIED_FACT,
        _optional_text(event.payload.get("text")) or condition_id,
        provenance,
        evidence_ids=(evidence_id,),
    )
    return replace(
        state,
        evidence=replace(
            state.evidence,
            items=state.evidence.items + (fact,),
            conditions=tuple(conditions),
        ),
    )


def _invalidate_evidence(state: ThreadState, raw_evidence_id: object) -> ThreadState:
    evidence_id = _optional_text(raw_evidence_id)
    if evidence_id is None:
        return _inconsistent(state, "evidence_invalidated_without_identity")
    if not any(item.id == evidence_id for item in state.evidence.items):
        return _inconsistent(state, f"unknown_evidence:{evidence_id}")
    stale_ids = {evidence_id}
    changed = True
    while changed:
        changed = False
        for item in state.evidence.items:
            if item.id not in stale_ids and (
                set(item.evidence_ids) & stale_ids or set(item.depends_on) & stale_ids
            ):
                stale_ids.add(item.id)
                changed = True
    items = tuple(
        replace(item, status=StateItemStatus.STALE) if item.id in stale_ids else item
        for item in state.evidence.items
    )
    conditions = tuple(
        replace(condition, status=ConditionStatus.INVALIDATED)
        if set(condition.satisfied_by) & stale_ids
        else condition
        for condition in state.evidence.conditions
    )
    stale_hypothesis_ids = state.evidence.stale_hypothesis_ids | {
        item.id
        for item in items
        if item.kind == StateItemKind.HYPOTHESIS and item.status == StateItemStatus.STALE
    }
    return replace(
        state,
        evidence=replace(
            state.evidence,
            items=items,
            conditions=conditions,
            stale_hypothesis_ids=stale_hypothesis_ids,
        ),
    )


def _user_text(payload: Mapping[str, Any]) -> str | None:
    prompt = _optional_text(payload.get("prompt"))
    if prompt:
        return prompt
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        str(part["text"])
        for part in content
        if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    ]
    return "\n".join(parts) or None


def _summary_text(payload: Mapping[str, Any]) -> str | None:
    for key in ("text", "summary", "explanation"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            text = "\n".join(part for part in value if isinstance(part, str) and part.strip())
            if text:
                return text
    steps = payload.get("steps")
    if isinstance(steps, list):
        text = "\n".join(
            step["step"]
            for step in steps
            if isinstance(step, Mapping) and isinstance(step.get("step"), str) and step["step"]
        )
        return text or None
    return None


def _outcome_text(event: TraceEvent) -> str:
    details = [
        f"{key}={value}"
        for key in ("command", "tool", "status", "exitCode", "exit_code", "success", "rule")
        if isinstance((value := event.payload.get(key)), str | int | bool)
    ]
    text = event.payload.get("text") or event.payload.get("diff")
    if isinstance(text, str) and text:
        details.append(text)
    return f"{event.kind}: {' '.join(details)}" if details else event.kind


def _failed(payload: Mapping[str, Any]) -> bool:
    return outcome_failure(payload) is True


def _validation_status(payload: Mapping[str, Any]) -> ValidationStatus:
    failure = outcome_failure(payload)
    if failure is True:
        return ValidationStatus.FAILED
    if failure is False:
        return ValidationStatus.PASSED
    return ValidationStatus.UNKNOWN


def _validation_scopes(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw = payload.get("validated_paths")
    if not isinstance(raw, list) or len(raw) > _VALIDATION_SCOPE_LIMIT:
        return ()
    scopes: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        scope = value.strip().removeprefix("./").rstrip("/")
        if (
            not scope
            or scope.startswith(("/", "~", "../"))
            or "://" in scope
            or ".." in scope.split("/")
            or not _VALIDATION_PATH_CHARS.fullmatch(scope)
        ):
            continue
        scopes.add(scope)
    return tuple(sorted(scopes))


def _within_scope(path: str, scope: str) -> bool:
    return path == scope or path.startswith(f"{scope}/")


def _string_tuple(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
