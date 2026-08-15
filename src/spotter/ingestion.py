"""Normalize Codex App Server notifications into durable, runtime-neutral traces."""

import hashlib
import json
import warnings
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from spotter.app_server import AppServerEvent
from spotter.config import McpToolSemantics
from spotter.effects import Classification, classify, effect_event
from spotter.identity import (
    IdentityProvenance,
    RuntimeIdentity,
    RuntimeIdentityRegistry,
    TurnStatus,
)
from spotter.observability import (
    APP_SERVER_ITEM_FIELDS,
    APP_SERVER_METHOD_FIELDS,
    SOURCE_AUDIT_RELATIVE_PATH,
    CoverageStatus,
    SourceAuditStore,
)
from spotter.snapshot import StepJournal, StepRecord
from spotter.trace import TraceEvent, TraceProvenance

_AGENT = "codex"
_SOURCE = "codex_app_server"
_REPLAY_SAFE_METHODS = {
    "thread/started",
    "turn/started",
    "turn/completed",
    "item/started",
    "item/completed",
}


class IngestionError(ValueError):
    """An App Server event cannot be normalized without guessing or conflicts with history."""


class CodexTraceNormalizer:
    """The only layer allowed to know Codex notification and ThreadItem shapes."""

    def __init__(
        self,
        identities: RuntimeIdentityRegistry | None = None,
        mcp_semantics: tuple[McpToolSemantics, ...] = (),
    ) -> None:
        self.identities = identities or RuntimeIdentityRegistry()
        self.mcp_semantics = mcp_semantics

    def normalize(self, event: AppServerEvent) -> TraceEvent:
        params = event.raw.get("params")
        if not isinstance(params, Mapping):
            raise IngestionError(f"{event.method} notification has no object params")
        params = dict(params)
        event_id = _event_id(event.method, params)
        provenance = TraceProvenance(_SOURCE, event.method)

        if event.method == "thread/started":
            thread = _object(params.get("thread"), "thread")
            started_thread_id = _string(thread.get("id"), "thread.id")
            identity = self._identity(started_thread_id)
            return TraceEvent(
                "thread_started",
                _known(thread, *APP_SERVER_METHOD_FIELDS["thread/started"].values()),
                event_id,
                _seconds(thread.get("createdAt")),
                identity,
                provenance=provenance,
            )

        external_thread_id = _optional_string(params.get("threadId"))
        turn_raw = params.get("turn")
        external_turn_id = _optional_string(params.get("turnId"))
        if isinstance(turn_raw, Mapping):
            external_turn_id = _optional_string(turn_raw.get("id")) or external_turn_id

        if event.method == "turn/completed":
            if external_thread_id is None or external_turn_id is None:
                raise IngestionError("turn/completed omitted thread or turn identity")
            completed_turn = _object(turn_raw, "turn")
            outcome = _optional_string(completed_turn.get("status"))
            status = TurnStatus.INTERRUPTED if outcome == "interrupted" else TurnStatus.COMPLETED
            identity, observed_start = self._finish_turn(
                external_thread_id, external_turn_id, status
            )
            payload = _known(completed_turn, *APP_SERVER_METHOD_FIELDS["turn/completed"].values())
            payload["observed_start"] = observed_start
            return TraceEvent(
                "turn_completed",
                payload,
                event_id,
                _seconds(completed_turn.get("completedAt")),
                identity,
                provenance=provenance,
            )

        identity = self._identity(
            external_thread_id,
            external_turn_id,
            observed_turn_start=event.method == "turn/started",
        )

        if event.method == "turn/started":
            if not isinstance(turn_raw, Mapping):
                raise IngestionError("turn/started omitted turn")
            return TraceEvent(
                "turn_started",
                _known(turn_raw, *APP_SERVER_METHOD_FIELDS["turn/started"].values()),
                event_id,
                _seconds(turn_raw.get("startedAt")),
                identity,
                provenance=provenance,
            )
        if event.method == "thread/status/changed":
            return TraceEvent(
                "thread_status",
                _known(params, *APP_SERVER_METHOD_FIELDS["thread/status/changed"].values()),
                event_id,
                identity=identity,
                provenance=provenance,
            )
        if event.method in {
            "thread/archived",
            "thread/unarchived",
            "thread/closed",
            "thread/deleted",
        }:
            return TraceEvent(
                event.method.replace("/", "_"),
                {},
                event_id,
                identity=identity,
                provenance=provenance,
            )
        if event.method in {"item/started", "item/completed"}:
            item = _object(params.get("item"), "item")
            item_id = _string(item.get("id"), "item.id")
            completed = event.method == "item/completed"
            kind, payload = _normalize_item(item, completed, self.mcp_semantics)
            payload["lifecycle"] = "completed" if completed else "started"
            timestamp = _milliseconds(
                params.get("completedAtMs") if completed else params.get("startedAtMs")
            )
            return TraceEvent(
                kind,
                payload,
                event_id,
                timestamp,
                identity,
                operation_id=item_id,
                item_id=item_id,
                provenance=provenance,
            )
        if event.method == "turn/diff/updated":
            return TraceEvent(
                "diff_updated",
                _known(params, *APP_SERVER_METHOD_FIELDS["turn/diff/updated"].values()),
                event_id,
                identity=identity,
                provenance=provenance,
            )
        if event.method == "turn/plan/updated":
            plan = params.get("plan")
            plan_payload: dict[str, Any] = {}
            if isinstance(plan, list):
                plan_payload["steps"] = [
                    _known(step, "step", "status") for step in plan if isinstance(step, Mapping)
                ]
            if isinstance(params.get("explanation"), str):
                plan_payload["explanation"] = params["explanation"]
            return TraceEvent(
                "plan",
                plan_payload,
                event_id,
                identity=identity,
                provenance=provenance,
            )
        if event.method == "thread/tokenUsage/updated":
            return TraceEvent(
                "token_usage",
                _token_usage(params.get("tokenUsage")),
                event_id,
                identity=identity,
                provenance=provenance,
            )
        if event.method == "error":
            return TraceEvent(
                "runtime_error",
                _known(params, "error", "willRetry"),
                event_id,
                identity=identity,
                provenance=provenance,
            )

        # Unknown notifications remain visible without copying an unstable wire payload into IR.
        return TraceEvent(
            "runtime_event_unknown",
            {"method": event.method},
            event_id,
            identity=identity,
            item_id=_optional_string(params.get("itemId")),
            provenance=provenance,
        )

    def _identity(
        self,
        external_thread_id: str | None,
        external_turn_id: str | None = None,
        *,
        observed_turn_start: bool = False,
    ) -> RuntimeIdentity:
        if external_thread_id is None:
            return RuntimeIdentity(None, None, None, IdentityProvenance(agent=_AGENT))
        thread = self.identities.observe_thread(_AGENT, external_thread_id)
        if external_turn_id is None:
            return RuntimeIdentity(thread.id, None, None, thread.provenance)
        turn = self.identities.start_turn(
            thread.id, external_turn_id, observed_start=observed_turn_start
        )
        return self.identities.address_turn(turn.id)

    def _finish_turn(
        self, external_thread_id: str, external_turn_id: str, status: TurnStatus
    ) -> tuple[RuntimeIdentity, bool]:
        thread = self.identities.observe_thread(_AGENT, external_thread_id)
        turn = self.identities.finish_turn(thread.id, external_turn_id, status)
        return self.identities.address_turn(turn.id), turn.observed_start


class AppServerTraceIngestor:
    """Append normalized events once, with restart-safe lifecycle reconciliation."""

    def __init__(
        self, journals_dir: Path, mcp_semantics: tuple[McpToolSemantics, ...] = ()
    ) -> None:
        self.journals_dir = journals_dir
        self.journals_dir.mkdir(parents=True, exist_ok=True)
        self.normalizer = CodexTraceNormalizer(mcp_semantics=mcp_semantics)
        self.source_audit = SourceAuditStore(journals_dir / SOURCE_AUDIT_RELATIVE_PATH)
        self.last_source_audit_error: str | None = None
        self._seen: set[str] = set()
        self._operations: dict[tuple[str, str, str, str], tuple[str, str | None]] = {}
        self._terminal_turns: set[str] = set()
        self._last_at: dict[tuple[str, str, str], float] = {}
        self._last_arrival_seq: dict[int, int] = {}
        self.last_connection_epoch = 0
        # ponytail: recovery is O(all App Server history); #89 should checkpoint per-thread
        # reconciliation state when retained histories become measurably expensive.
        self._recover()

    def ingest(
        self, raw_event: AppServerEvent, *, connection_epoch: int | None = None
    ) -> StepRecord | None:
        event = self.normalizer.normalize(raw_event)
        if connection_epoch is not None:
            event = replace(event, connection_epoch=connection_epoch)
        return self.record_source(raw_event, event)

    def record_source(self, raw_event: AppServerEvent, event: TraceEvent) -> StepRecord | None:
        """Persist normalized IR and a bounded value-free source-shape comparison."""

        record = self.record(event)
        self.audit_source(
            raw_event,
            record.event if record is not None else event,
            disposition="ingested" if record is not None else "deduplicated",
        )
        return record

    def audit_source(
        self,
        raw_event: AppServerEvent,
        event: TraceEvent,
        *,
        disposition: str,
        state_status: CoverageStatus | None = None,
    ) -> None:
        """Retain field shapes without allowing audit I/O to break observation."""

        try:
            self.source_audit.record(
                raw_event,
                event,
                disposition=disposition,
                state_status=state_status,
            )
            self.last_source_audit_error = None
        except (OSError, UnicodeError) as error:
            # The primary journal already contains the event. Losing the optional
            # shape audit must not disconnect observation or skip live-state reduction.
            self.last_source_audit_error = str(error)
            warnings.warn(f"source audit unavailable: {error}", RuntimeWarning, stacklevel=2)

    def record(self, event: TraceEvent) -> StepRecord | None:
        """Append one already-normalized runtime event through the same idempotency path."""

        if event.event_id is not None and event.event_id in self._seen:
            return None
        route = _route(event)
        turn_key = (
            event.identity.turn_id.value if event.identity and event.identity.turn_id else None
        )
        if event.kind == "turn_started" and turn_key in self._terminal_turns:
            return None

        if event.operation_id is not None:
            operation_key = _operation_key(event, route)
            lifecycle = _optional_string(event.payload.get("lifecycle"))
            previous = self._operations.get(operation_key)
            if lifecycle == "started" and previous is not None:
                return None
            if lifecycle == "completed":
                outcome = _optional_string(event.payload.get("status"))
                if previous and previous[0] == "completed":
                    if previous[1] != outcome:
                        raise IngestionError(
                            f"operation {event.operation_id} changed terminal outcome "
                            f"from {previous[1]!r} to {outcome!r}"
                        )
                    return None
                payload = dict(event.payload)
                payload["observed_start"] = previous is not None
                event = replace(event, payload=payload)

        if event.occurred_at is not None:
            ordering_key = _ordering_key(event, route)
            last_at = self._last_at.get(ordering_key)
            if last_at is not None and event.occurred_at < last_at:
                event = replace(event, payload={**event.payload, "out_of_order": True})

        if event.connection_epoch is not None:
            previous_seq = self._last_arrival_seq.get(event.connection_epoch, 0)
            if event.arrival_seq is not None and event.arrival_seq <= previous_seq:
                raise IngestionError(
                    f"arrival sequence {event.arrival_seq} did not advance "
                    f"connection epoch {event.connection_epoch}"
                )
            event = replace(event, arrival_seq=event.arrival_seq or previous_seq + 1)

        journal = StepJournal(self.journals_dir / route)
        record = journal.record(event)
        self._remember(record.event, route)
        effect = effect_event(event)
        if effect is not None:
            effect_record = journal.record(effect)
            self._remember(effect_record.event, route)
        return record

    def append_operational(self, event: TraceEvent, *, observed_at: float) -> StepRecord:
        """Persist pre-normalized telemetry without touching live ingestion indexes.

        This entry point is safe to run in a worker thread. The event-loop owner
        must call :meth:`index_operational` after the append completes.
        """

        return StepJournal(self.journals_dir / _route(event)).record(event, observed_at=observed_at)

    def index_operational(self, record: StepRecord) -> None:
        """Publish a completed worker-thread append to event-loop-owned indexes."""

        self._remember(record.event, _route(record.event))

    def records(self) -> tuple[StepRecord, ...]:
        """Return durable App Server history for conservative daemon hydration."""

        return tuple(
            record
            for path in sorted(self.journals_dir.glob("app-server-*.jsonl"))
            for record in StepJournal.load(path, repair_tail=True)
        )

    def _recover(self) -> None:
        for path in sorted(self.journals_dir.glob("app-server-*.jsonl")):
            for record in StepJournal.load(path, repair_tail=True):
                event = record.event
                self._restore_identity(event)
                self._remember(event, path.name)

    def _restore_identity(self, event: TraceEvent) -> None:
        identity = event.identity
        if identity is None or identity.provenance.agent_thread_id is None:
            return
        thread = self.normalizer.identities.observe_thread(
            identity.provenance.agent, identity.provenance.agent_thread_id
        )
        external_turn_id = identity.provenance.agent_turn_id
        if external_turn_id is None:
            return
        if event.kind == "turn_completed":
            outcome = event.payload.get("status")
            status = TurnStatus.INTERRUPTED if outcome == "interrupted" else TurnStatus.COMPLETED
            self.normalizer.identities.finish_turn(thread.id, external_turn_id, status)
        else:
            self.normalizer.identities.start_turn(
                thread.id,
                external_turn_id,
                observed_start=event.kind == "turn_started",
            )

    def _remember(self, event: TraceEvent, route: str) -> None:
        if event.event_id is not None:
            self._seen.add(event.event_id)
        if event.operation_id is not None:
            lifecycle = _optional_string(event.payload.get("lifecycle"))
            if lifecycle is not None:
                self._operations[_operation_key(event, route)] = (
                    lifecycle,
                    _optional_string(event.payload.get("status")),
                )
        if event.kind == "turn_completed" and event.identity and event.identity.turn_id:
            self._terminal_turns.add(event.identity.turn_id.value)
        if event.occurred_at is not None:
            ordering_key = _ordering_key(event, route)
            self._last_at[ordering_key] = max(
                self._last_at.get(ordering_key, event.occurred_at), event.occurred_at
            )
        if event.connection_epoch is not None:
            self.last_connection_epoch = max(self.last_connection_epoch, event.connection_epoch)
            if event.arrival_seq is not None:
                self._last_arrival_seq[event.connection_epoch] = max(
                    self._last_arrival_seq.get(event.connection_epoch, 0), event.arrival_seq
                )


def _normalize_item(
    item: Mapping[str, Any],
    completed: bool,
    mcp_semantics: tuple[McpToolSemantics, ...],
) -> tuple[str, dict[str, Any]]:
    item_type = _optional_string(item.get("type")) or "unknown"
    if item_type == "userMessage":
        payload: dict[str, Any] = {"content": _user_content(item.get("content"))}
        if (client_id := _optional_string(item.get("clientId"))) is not None:
            payload["client_user_message_id"] = client_id
        return "user_prompt", payload
    if item_type == "agentMessage":
        return "agent_message", _known(item, "text", "phase")
    if item_type == "plan":
        return "plan", _known(item, *APP_SERVER_ITEM_FIELDS["plan"])
    if item_type == "reasoning":
        # App Server exposes both summary and raw reasoning content. Only the summary is Trace IR.
        summary = item.get("summary")
        return "reasoning_summary", {"summary": summary if isinstance(summary, list) else []}
    if item_type == "commandExecution":
        kind = "command_result" if completed else "command_started"
        payload = _known(item, *APP_SERVER_ITEM_FIELDS["commandExecution"])
        payload.update(
            _classification_payload(
                classify("Bash", {"command": payload.get("command"), "cwd": payload.get("cwd")})
            )
        )
        return kind, payload
    if item_type == "fileChange":
        changes = item.get("changes")
        normalized = (
            [
                _known(change, "path", "kind", "diff")
                for change in changes
                if isinstance(change, Mapping)
            ]
            if isinstance(changes, list)
            else []
        )
        payload = {
            "status": item.get("status"),
            "files": [change["path"] for change in normalized if "path" in change],
            "changes": normalized,
        }
        payload.update(
            _classification_payload(classify("apply_patch", {"files": payload["files"]}))
        )
        return ("file_edit" if completed else "file_change_started"), payload
    if item_type == "mcpToolCall":
        kind = "tool_result" if completed else "tool_started"
        payload = _known(item, *APP_SERVER_ITEM_FIELDS["mcpToolCall"])
        server = _optional_string(payload.get("server")) or "unknown"
        tool = _optional_string(payload.get("tool")) or "unknown"
        arguments = payload.get("arguments")
        values = dict(arguments) if isinstance(arguments, Mapping) else {}
        payload.update(
            _classification_payload(classify(f"mcp__{server}__{tool}", values, mcp_semantics))
        )
        return kind, payload
    if item_type == "dynamicToolCall":
        kind = "tool_result" if completed else "tool_started"
        payload = _known(item, *APP_SERVER_ITEM_FIELDS["dynamicToolCall"])
        namespace = _optional_string(payload.get("namespace")) or "unknown"
        tool = _optional_string(payload.get("tool")) or "unknown"
        arguments = payload.get("arguments")
        values = dict(arguments) if isinstance(arguments, Mapping) else {}
        payload.update(_classification_payload(classify(f"dynamic__{namespace}__{tool}", values)))
        return kind, payload
    if item_type == "webSearch":
        return ("search" if completed else "search_started"), _known(
            item, *APP_SERVER_ITEM_FIELDS["webSearch"]
        )
    return ("item_completed" if completed else "item_started"), {"item_type": item_type}


def _classification_payload(value: Classification) -> dict[str, Any]:
    if value.reversibility_class == "B":
        return {
            "reversibility_class": "C",
            "expected_reversibility_class": "B",
            "effect_kind": f"uncheckpointed_{value.kind}",
            "resource": value.resource,
            "reversible": False,
            "checkpoint_required": True,
            "checkpoint_observed": False,
            "effect_classifier": value.classifier_id,
            "effect_reason": "checkpoint_unavailable",
            "effect_confidence": value.parse_confidence,
            "semantic_operation": value.semantic_operation,
        }
    return {
        "reversibility_class": value.reversibility_class,
        "effect_kind": value.kind,
        "resource": value.resource,
        "reversible": value.reversible,
        "effect_classifier": value.classifier_id,
        "effect_reason": value.reason_code,
        "effect_confidence": value.parse_confidence,
        "semantic_operation": value.semantic_operation,
    }


def _event_id(method: str, params: Mapping[str, Any]) -> str | None:
    if method not in _REPLAY_SAFE_METHODS:
        return None
    encoded = json.dumps(
        {"method": method, "params": params},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return f"codex:{hashlib.sha256(encoded).hexdigest()}"


def _route(event: TraceEvent) -> str:
    if event.identity is not None and event.identity.thread_id is not None:
        return f"app-server-{event.identity.thread_id.value}.jsonl"
    return "app-server-unscoped.jsonl"


def _operation_key(event: TraceEvent, route: str) -> tuple[str, str, str, str]:
    turn_id = (
        event.identity.turn_id.value
        if event.identity is not None and event.identity.turn_id is not None
        else ""
    )
    assert event.operation_id is not None
    return route, turn_id, str(event.connection_epoch or ""), event.operation_id


def _ordering_key(event: TraceEvent, route: str) -> tuple[str, str, str]:
    turn_id = (
        event.identity.turn_id.value
        if event.identity is not None and event.identity.turn_id is not None
        else ""
    )
    return route, turn_id, str(event.connection_epoch or "")


def _token_usage(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, Any] = {}
    for group in ("last", "total"):
        raw_group = value.get(group)
        if isinstance(raw_group, Mapping):
            payload[group] = _known(
                raw_group,
                "inputTokens",
                "cachedInputTokens",
                "cacheWriteInputTokens",
                "outputTokens",
                "reasoningOutputTokens",
                "totalTokens",
            )
    if isinstance(value.get("modelContextWindow"), int):
        payload["modelContextWindow"] = value["modelContextWindow"]
    return payload


def _user_content(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        _known(part, "type", "text", "url", "path") for part in value if isinstance(part, Mapping)
    ]


def _known(value: object, *keys: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in keys if key in value and value[key] is not None}


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise IngestionError(f"{name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise IngestionError(f"{name} must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _seconds(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _milliseconds(value: object) -> float | None:
    seconds = _seconds(value)
    return seconds / 1000 if seconds is not None else None
