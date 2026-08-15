"""Coverage taxonomy and source-vs-Trace IR observability audits."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spotter.outcomes import outcome_failure
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent

if TYPE_CHECKING:
    from spotter.app_server import AppServerEvent
    from spotter.thread_state import StateItem, ThreadState

SOURCE_AUDIT_RELATIVE_PATH = Path("source-audit") / "samples.jsonl"
SOURCE_AUDIT_SCHEMA = "spotter.source_audit"
SOURCE_AUDIT_SCHEMA_VERSION = 1
_FIELD_SEGMENT = re.compile(r"[._-]|(?<=[a-z0-9])(?=[A-Z])")


class ObservabilityError(ValueError):
    """A persisted source audit cannot be trusted."""


class EvidenceFamily(StrEnum):
    USER_GOAL = "user_goal"
    THREAD_TURN_IDENTITY = "thread_turn_identity"
    PLAN_SUMMARY = "plan_summary"
    REASONING_SUMMARY = "reasoning_summary"
    REPOSITORY_EXPLORATION = "repository_exploration"
    COMMAND_TOOL_CALL = "command_tool_call"
    TOOL_OUTCOME = "tool_outcome"
    FILE_EDIT_DIFF = "file_edit_diff"
    MCP_CALL_RESULT = "mcp_call_result"
    SUBAGENT_LINEAGE = "subagent_lineage"
    VALIDATION_OUTCOME = "validation_outcome"
    INTERVENTION_DELIVERY = "intervention_delivery"
    EXTERNAL_EFFECT = "external_effect"
    RUNTIME_USAGE = "runtime_usage"


class CoverageStatus(StrEnum):
    OBSERVED_EXACT = "observed_exact"
    OBSERVED_PARTIAL = "observed_partial"
    OBSERVED_ENCRYPTED = "observed_encrypted"
    OBSERVED_UNCORRELATED = "observed_uncorrelated"
    SOURCE_NOT_EXPOSED = "source_not_exposed"
    ADAPTER_DROPPED = "adapter_dropped"
    OBSERVATION_GAP = "observation_gap"
    LEGACY_UNAVAILABLE = "legacy_unavailable"
    NOT_PERFORMED = "not_performed"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class OpportunityStatus(StrEnum):
    VISIBLE_IN_TIME = "visible_in_time"
    VISIBLE_TOO_LATE = "visible_too_late"
    STRUCTURALLY_INVISIBLE = "structurally_invisible"
    LOST_BY_ADAPTER = "lost_by_adapter"
    LOST_BY_GAP = "lost_by_gap"
    UNJUDGEABLE = "unjudgeable"


@dataclass(frozen=True)
class EvidenceTiming:
    family: EvidenceFamily
    status: CoverageStatus
    source_step: int | None = None
    trace_step: int | None = None
    state_step: int | None = None


def classify_opportunity(
    failure_boundary: int, required_evidence: Iterable[EvidenceTiming]
) -> OpportunityStatus:
    """Classify visibility without reconstructing facts the runtime never exposed."""

    evidence = tuple(required_evidence)
    if not evidence:
        return OpportunityStatus.UNJUDGEABLE
    statuses = {item.status for item in evidence}
    if CoverageStatus.OBSERVATION_GAP in statuses:
        return OpportunityStatus.LOST_BY_GAP
    if statuses & {CoverageStatus.ADAPTER_DROPPED, CoverageStatus.OBSERVED_UNCORRELATED}:
        return OpportunityStatus.LOST_BY_ADAPTER
    if statuses & {CoverageStatus.SOURCE_NOT_EXPOSED, CoverageStatus.OBSERVED_ENCRYPTED}:
        return OpportunityStatus.STRUCTURALLY_INVISIBLE
    if statuses & {
        CoverageStatus.LEGACY_UNAVAILABLE,
        CoverageStatus.NOT_PERFORMED,
        CoverageStatus.UNKNOWN,
    }:
        return OpportunityStatus.UNJUDGEABLE
    available = [item.state_step for item in evidence]
    if any(step is None for step in available):
        return OpportunityStatus.UNJUDGEABLE
    return (
        OpportunityStatus.VISIBLE_IN_TIME
        if max(step for step in available if step is not None) <= failure_boundary
        else OpportunityStatus.VISIBLE_TOO_LATE
    )


@dataclass(frozen=True)
class SourceAuditSample:
    method: str
    source_fields: tuple[str, ...]
    normalized_kind: str
    normalized_fields: tuple[str, ...]
    families: tuple[str, ...]
    status: str
    state_status: str | None
    thread_id: str | None
    connection_epoch: int | None
    disposition: str
    recorded_at: float


_METHOD_FAMILY: dict[str, tuple[EvidenceFamily, ...]] = {
    "thread/started": (EvidenceFamily.THREAD_TURN_IDENTITY,),
    "thread/status/changed": (EvidenceFamily.THREAD_TURN_IDENTITY,),
    "turn/started": (EvidenceFamily.THREAD_TURN_IDENTITY,),
    "turn/completed": (EvidenceFamily.THREAD_TURN_IDENTITY,),
    "turn/plan/updated": (EvidenceFamily.PLAN_SUMMARY,),
    "turn/diff/updated": (EvidenceFamily.FILE_EDIT_DIFF,),
    "thread/tokenUsage/updated": (EvidenceFamily.RUNTIME_USAGE,),
}

_ITEM_FAMILY: dict[str, tuple[EvidenceFamily, ...]] = {
    "userMessage": (EvidenceFamily.USER_GOAL,),
    "plan": (EvidenceFamily.PLAN_SUMMARY,),
    "reasoning": (EvidenceFamily.REASONING_SUMMARY,),
    "commandExecution": (EvidenceFamily.COMMAND_TOOL_CALL, EvidenceFamily.TOOL_OUTCOME),
    "fileChange": (EvidenceFamily.FILE_EDIT_DIFF,),
    "mcpToolCall": (EvidenceFamily.MCP_CALL_RESULT,),
    "dynamicToolCall": (EvidenceFamily.MCP_CALL_RESULT,),
    "webSearch": (EvidenceFamily.REPOSITORY_EXPLORATION,),
}

# Shared with CodexTraceNormalizer so adding a normalized field cannot silently
# leave source-vs-adapter audit sensitivity behind.
APP_SERVER_ITEM_FIELDS: dict[str, tuple[str, ...]] = {
    "commandExecution": (
        "command",
        "cwd",
        "status",
        "aggregatedOutput",
        "exitCode",
        "durationMs",
        "source",
    ),
    "fileChange": ("changes", "status"),
    "mcpToolCall": ("server", "tool", "arguments", "status", "result", "error", "durationMs"),
    "dynamicToolCall": (
        "namespace",
        "tool",
        "arguments",
        "status",
        "success",
        "contentItems",
        "durationMs",
    ),
    "reasoning": ("summary",),
    "plan": ("text",),
    "userMessage": ("content", "clientId"),
    "webSearch": ("query", "action", "results"),
}

APP_SERVER_METHOD_FIELDS: dict[str, dict[str, str]] = {
    "thread/started": {
        "params.thread.cwd": "cwd",
        "params.thread.sessionId": "sessionId",
        "params.thread.status": "status",
        "params.thread.name": "name",
    },
    "thread/status/changed": {"params.status": "status"},
    "turn/started": {"params.turn.status": "status"},
    "turn/completed": {
        "params.turn.status": "status",
        "params.turn.durationMs": "durationMs",
        "params.turn.error": "error",
    },
    "turn/plan/updated": {
        "params.plan": "steps",
        "params.explanation": "explanation",
    },
    "turn/diff/updated": {"params.diff": "diff"},
    "thread/tokenUsage/updated": {
        "params.tokenUsage.last": "last",
        "params.tokenUsage.total": "total",
        "params.tokenUsage.modelContextWindow": "modelContextWindow",
    },
}

_PRESERVED_FIELDS = {
    item_type: {f"params.item.{field}": field for field in fields}
    for item_type, fields in APP_SERVER_ITEM_FIELDS.items()
}
_PRESERVED_FIELDS["userMessage"]["params.item.clientId"] = "client_user_message_id"


def source_audit_sample(
    raw_event: AppServerEvent,
    event: TraceEvent,
    *,
    disposition: str = "ingested",
    state_status: CoverageStatus | None = None,
) -> SourceAuditSample:
    source_fields = tuple(sorted(_field_paths(raw_event.raw)))
    item_type = _item_type(raw_event.raw)
    families = _families_for(raw_event.method, item_type)
    if event.kind == "user_prompt" and event.payload.get("input_origin") == "spotter_supervision":
        families = (EvidenceFamily.INTERVENTION_DELIVERY,)
    status = _source_status(raw_event, item_type, source_fields, event)
    if status == CoverageStatus.OBSERVED_ENCRYPTED:
        # State cannot turn opaque source content into a known fact. Preserve the
        # epistemic limit even when a generic lifecycle marker reached the reducer.
        state_status = CoverageStatus.OBSERVED_ENCRYPTED
    return SourceAuditSample(
        method=raw_event.method,
        source_fields=source_fields,
        normalized_kind=event.kind,
        normalized_fields=tuple(sorted(event.payload)),
        families=tuple(item.value for item in families),
        status=status.value,
        state_status=state_status.value if state_status is not None else None,
        thread_id=(
            event.identity.thread_id.value
            if event.identity is not None and event.identity.thread_id is not None
            else None
        ),
        connection_epoch=event.connection_epoch,
        disposition=disposition,
        recorded_at=time.time(),
    )


class SourceAuditStore:
    """Bounded shape-only source audit; values and reasoning content are never retained."""

    def __init__(self, path: Path, *, max_records: int = 1000) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self.path = path
        self.max_records = max_records
        try:
            self._count = self._sample_count(path.read_bytes()) if path.exists() else 0
        except OSError:
            # The primary observation path must still start. The first attempted
            # audit write and the reporting command surface the filesystem error.
            self._count = 0

    def record(
        self,
        raw_event: AppServerEvent,
        event: TraceEvent,
        *,
        disposition: str,
        state_status: CoverageStatus | None = None,
    ) -> SourceAuditSample:
        sample = source_audit_sample(
            raw_event,
            event,
            disposition=disposition,
            state_status=state_status,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a") as lock:
            flock(lock, LOCK_EX)
            try:
                self._refuse_unknown_container()
                self._repair_tail()
                self._ensure_current_container()
                self.path.chmod(0o600)
                with self.path.open("a", encoding="utf-8") as sink:
                    sink.write(json.dumps(asdict(sample), separators=(",", ":")) + "\n")
                    sink.flush()
                    os.fsync(sink.fileno())
                self._count += 1
                if self._count > self.max_records * 2:
                    self._compact()
            finally:
                flock(lock, LOCK_UN)
        return sample

    def load(self) -> tuple[SourceAuditSample, ...]:
        if not self.path.exists():
            return ()
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a") as lock:
            flock(lock, LOCK_EX)
            try:
                content = self.path.read_text(encoding="utf-8")
                if content and not content.endswith("\n"):
                    content = content.rsplit("\n", 1)[0]
                lines = content.splitlines()
            finally:
                flock(lock, LOCK_UN)
        start_line = 1
        if lines and self._is_metadata(lines[0], line_number=1):
            lines = lines[1:]
            start_line = 2
        return self._parse_samples(lines, start_line=start_line)

    def _parse_samples(
        self, lines: Iterable[str], *, start_line: int
    ) -> tuple[SourceAuditSample, ...]:
        samples = []
        for line_number, line in enumerate(lines, start=start_line):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, Mapping):
                    raise TypeError("sample is not an object")
                epoch = raw.get("connection_epoch")
                if isinstance(epoch, bool) or not isinstance(epoch, int | None):
                    raise TypeError("connection_epoch is not an integer or null")
                thread_id = raw.get("thread_id")
                if not isinstance(thread_id, str | None):
                    raise TypeError("thread_id is not a string or null")
                status = CoverageStatus(_required_string(raw, "status")).value
                raw_state_status = _optional_string(raw, "state_status")
                state_status = (
                    CoverageStatus(raw_state_status).value if raw_state_status is not None else None
                )
                families = _string_tuple(raw, "families")
                for family in families:
                    EvidenceFamily(family)
                disposition = _required_string(raw, "disposition")
                if disposition not in {"ingested", "deduplicated"}:
                    raise ValueError(f"unknown disposition {disposition!r}")
                raw_recorded_at = raw["recorded_at"]
                if isinstance(raw_recorded_at, bool) or not isinstance(
                    raw_recorded_at, int | float
                ):
                    raise TypeError("recorded_at is not numeric")
                recorded_at = float(raw_recorded_at)
                if not math.isfinite(recorded_at):
                    raise ValueError("recorded_at is not finite")
                samples.append(
                    SourceAuditSample(
                        method=_required_string(raw, "method"),
                        source_fields=_string_tuple(raw, "source_fields"),
                        normalized_kind=_required_string(raw, "normalized_kind"),
                        normalized_fields=_string_tuple(raw, "normalized_fields"),
                        families=families,
                        status=status,
                        state_status=state_status,
                        thread_id=thread_id,
                        connection_epoch=epoch,
                        disposition=disposition,
                        recorded_at=recorded_at,
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ObservabilityError(
                    f"{self.path} line {line_number} is not a valid source audit sample: {error}"
                ) from error
        return tuple(samples)

    def _repair_tail(self) -> None:
        if not self.path.exists():
            return
        content = self.path.read_bytes()
        if not content or content.endswith(b"\n"):
            self._count = self._sample_count(content)
            return
        boundary = content.rfind(b"\n") + 1
        with self.path.open("r+b") as sink:
            sink.truncate(boundary)
            sink.flush()
            os.fsync(sink.fileno())
        self._count = self._sample_count(content[:boundary])

    def _refuse_unknown_container(self) -> None:
        """Reject foreign metadata before recovery can mutate its container."""

        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        first = self.path.read_bytes().splitlines()[0]
        try:
            raw = json.loads(first)
        except json.JSONDecodeError:
            return
        if not isinstance(raw, Mapping) or raw.get("record_type") != "metadata":
            return
        try:
            self._validate_metadata(raw)
        except (TypeError, ValueError) as error:
            raise ObservabilityError(
                f"{self.path} line 1 is not valid source audit metadata: {error}"
            ) from error

    def _ensure_current_container(self) -> None:
        metadata = self._metadata_line()
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._replace(metadata)
            self._count = 0
            return
        content = self.path.read_bytes()
        first = content.splitlines()[0]
        try:
            raw = json.loads(first)
        except json.JSONDecodeError as error:
            raise ObservabilityError(
                f"{self.path} line 1 is not a valid source audit container: {error}"
            ) from error
        if isinstance(raw, Mapping) and raw.get("record_type") == "metadata":
            try:
                self._validate_metadata(raw)
            except (TypeError, ValueError) as error:
                raise ObservabilityError(
                    f"{self.path} line 1 is not valid source audit metadata: {error}"
                ) from error
            return
        try:
            legacy_lines = content.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ObservabilityError(f"{self.path} is not valid UTF-8 source audit data") from error
        self._parse_samples(legacy_lines, start_line=1)
        self._replace(metadata + content)

    def _is_metadata(self, line: str, *, line_number: int) -> bool:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return False
        if not isinstance(raw, Mapping) or raw.get("record_type") != "metadata":
            return False
        try:
            self._validate_metadata(raw)
        except (TypeError, ValueError) as error:
            raise ObservabilityError(
                f"{self.path} line {line_number} is not valid source audit metadata: {error}"
            ) from error
        return True

    @staticmethod
    def _validate_metadata(raw: Mapping[str, object]) -> None:
        if raw.get("schema") != SOURCE_AUDIT_SCHEMA:
            raise ValueError(f"unsupported schema {raw.get('schema')!r}")
        version = raw.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise TypeError("schema_version is not an integer")
        if version != SOURCE_AUDIT_SCHEMA_VERSION:
            direction = "newer" if version > SOURCE_AUDIT_SCHEMA_VERSION else "unsupported"
            raise ValueError(
                f"{direction} schema v{version}; "
                f"this build understands v{SOURCE_AUDIT_SCHEMA_VERSION}"
            )

    @staticmethod
    def _metadata_line() -> bytes:
        return (
            json.dumps(
                {
                    "record_type": "metadata",
                    "schema": SOURCE_AUDIT_SCHEMA,
                    "schema_version": SOURCE_AUDIT_SCHEMA_VERSION,
                },
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )

    @staticmethod
    def _sample_count(content: bytes) -> int:
        lines = content.splitlines()
        if not lines:
            return 0
        try:
            first = json.loads(lines[0])
        except json.JSONDecodeError:
            first = None
        metadata = isinstance(first, Mapping) and first.get("record_type") == "metadata"
        return max(0, len(lines) - int(metadata))

    def _replace(self, content: bytes) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as sink:
            sink.write(content)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _compact(self) -> None:
        samples = self.path.read_bytes().splitlines()[1:][-self.max_records :]
        self._replace(self._metadata_line() + b"\n".join(samples) + b"\n")
        self._count = len(samples)


@dataclass(frozen=True)
class ObservabilityReport:
    sessions: int
    hook_sessions: int
    app_server_sessions: int
    events: int
    gaps: int
    unknown_events: int
    deduplicated_source_samples: int
    trace_family_status: Mapping[str, Mapping[str, Mapping[str, int]]]
    source_status: Mapping[str, int]
    source_family_status: Mapping[str, Mapping[str, int]]
    state_family_status: Mapping[str, Mapping[str, int]]


def measure_observability(
    journals: Iterable[Iterable[StepRecord]],
    source_samples: Iterable[SourceAuditSample] = (),
) -> ObservabilityReport:
    source_samples = tuple(source_samples)
    deduplicated_source_samples = sum(
        sample.disposition == "deduplicated" for sample in source_samples
    )
    source_samples = tuple(sample for sample in source_samples if sample.disposition == "ingested")
    trace_family_status: dict[str, dict[str, Counter[str]]] = {
        "hook": {},
        "app_server": {},
    }
    sessions = hook_sessions = app_server_sessions = events = gaps = unknown_events = 0
    for records_iter in journals:
        records = tuple(records_iter)
        sessions += 1
        sources = {
            record.event.provenance.source
            for record in records
            if record.event.provenance is not None
        }
        is_app_server = "codex_app_server" in sources or any(
            record.event.connection_epoch is not None for record in records
        )
        if is_app_server:
            app_server_sessions += 1
        else:
            hook_sessions += 1
        surface = "app_server" if is_app_server else "hook"
        for record in records:
            event = record.event
            events += 1
            if event.kind == "observation_gap":
                gaps += 1
                continue
            if event.kind == "runtime_event_unknown":
                unknown_events += 1
            status = _trace_status(event)
            for trace_family in _trace_families(event):
                trace_family_status[surface].setdefault(trace_family.value, Counter())[
                    status.value
                ] += 1
    source_status = Counter(sample.status for sample in source_samples)
    source_family_status: dict[str, Counter[str]] = {}
    state_family_status: dict[str, Counter[str]] = {}
    for sample in source_samples:
        for family_name in sample.families or ("unclassified",):
            source_family_status.setdefault(family_name, Counter())[sample.status] += 1
            if sample.state_status is not None:
                state_family_status.setdefault(family_name, Counter())[sample.state_status] += 1
    return ObservabilityReport(
        sessions,
        hook_sessions,
        app_server_sessions,
        events,
        gaps,
        unknown_events,
        deduplicated_source_samples,
        {
            surface: {family: dict(counts) for family, counts in sorted(family_status.items())}
            for surface, family_status in trace_family_status.items()
        },
        dict(sorted(source_status.items())),
        {family: dict(counts) for family, counts in sorted(source_family_status.items())},
        {family: dict(counts) for family, counts in sorted(state_family_status.items())},
    )


def render_observability(report: ObservabilityReport) -> str:
    lines = [
        (
            f"sessions: {report.sessions} "
            f"(hook={report.hook_sessions}, app_server={report.app_server_sessions})"
        ),
        (
            f"events: {report.events}; observation_gaps: {report.gaps}; "
            f"unknown_events: {report.unknown_events}"
        ),
        f"deduplicated source notifications excluded: {report.deduplicated_source_samples}",
        "Trace IR evidence-family coverage:",
    ]
    for surface, family_status in report.trace_family_status.items():
        lines.append(f"  {surface}:")
        if not family_status:
            lines.append("    no classified evidence")
        for family, counts in family_status.items():
            summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
            lines.append(f"    {family}: {summary}")
    lines.append("source-vs-adapter samples:")
    if not report.source_status:
        lines.append("  none — post-App-Server source ceiling cannot be stated")
    else:
        for status, count in report.source_status.items():
            lines.append(f"  {status}: {count}")
        lines.append("  by evidence family:")
        for family, counts in report.source_family_status.items():
            summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
            lines.append(f"    {family}: {summary}")
        lines.append("ThreadState preservation:")
        if not report.state_family_status:
            lines.append("  none — source samples have not been reduced through live state")
        for family, counts in report.state_family_status.items():
            summary = ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
            lines.append(f"  {family}: {summary}")
    return "\n".join(lines)


def _field_paths(value: object, prefix: str = "", depth: int = 0) -> set[str]:
    if not isinstance(value, Mapping) or depth >= 3:
        return {prefix} if prefix else set()
    paths: set[str] = set()
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(child, Mapping):
            paths.update(_field_paths(child, path, depth + 1))
        else:
            paths.add(path)
    return paths


def _item_type(raw: Mapping[str, Any]) -> str | None:
    params = raw.get("params")
    item = params.get("item") if isinstance(params, Mapping) else None
    value = item.get("type") if isinstance(item, Mapping) else None
    return value if isinstance(value, str) else None


def _source_status(
    raw_event: AppServerEvent,
    item_type: str | None,
    source_fields: tuple[str, ...],
    event: TraceEvent,
) -> CoverageStatus:
    if any(_is_encrypted_field(field) for field in source_fields):
        return CoverageStatus.OBSERVED_ENCRYPTED
    families = _families_for(raw_event.method, item_type)
    if not families or event.kind == "runtime_event_unknown":
        return CoverageStatus.UNKNOWN
    preservation = _PRESERVED_FIELDS.get(
        item_type or "", APP_SERVER_METHOD_FIELDS.get(raw_event.method, {})
    )
    lost = [
        target
        for source, target in preservation.items()
        if _path_has_value(raw_event.raw, source) and target not in event.payload
    ]
    if lost:
        return CoverageStatus.ADAPTER_DROPPED
    if event.identity is None or event.identity.provenance.agent_thread_id is None:
        return CoverageStatus.OBSERVED_UNCORRELATED
    return CoverageStatus.OBSERVED_EXACT


_TRACE_FAMILY: dict[str, tuple[EvidenceFamily, ...]] = {
    "user_prompt": (EvidenceFamily.USER_GOAL,),
    "thread_started": (EvidenceFamily.THREAD_TURN_IDENTITY,),
    "thread_status": (EvidenceFamily.THREAD_TURN_IDENTITY,),
    "turn_started": (EvidenceFamily.THREAD_TURN_IDENTITY,),
    "turn_completed": (EvidenceFamily.THREAD_TURN_IDENTITY,),
    "plan": (EvidenceFamily.PLAN_SUMMARY,),
    "reasoning_summary": (EvidenceFamily.REASONING_SUMMARY,),
    "search": (EvidenceFamily.REPOSITORY_EXPLORATION,),
    "search_started": (EvidenceFamily.REPOSITORY_EXPLORATION,),
    "command_started": (EvidenceFamily.COMMAND_TOOL_CALL,),
    "command_result": (EvidenceFamily.COMMAND_TOOL_CALL, EvidenceFamily.TOOL_OUTCOME),
    "tool_started": (EvidenceFamily.MCP_CALL_RESULT,),
    "tool_result": (EvidenceFamily.TOOL_OUTCOME,),
    "file_change_started": (EvidenceFamily.FILE_EDIT_DIFF,),
    "file_edit": (EvidenceFamily.FILE_EDIT_DIFF,),
    "diff_updated": (EvidenceFamily.FILE_EDIT_DIFF,),
    "test_result": (EvidenceFamily.VALIDATION_OUTCOME,),
    "reviewer_decision": (EvidenceFamily.INTERVENTION_DELIVERY,),
    "intervention": (EvidenceFamily.INTERVENTION_DELIVERY,),
    "effect_proposed": (EvidenceFamily.EXTERNAL_EFFECT,),
    "effect_observed": (EvidenceFamily.EXTERNAL_EFFECT,),
    "token_usage": (EvidenceFamily.RUNTIME_USAGE,),
}


def _trace_families(event: TraceEvent) -> tuple[EvidenceFamily, ...]:
    if event.kind == "user_prompt" and event.payload.get("input_origin") == "spotter_supervision":
        return (EvidenceFamily.INTERVENTION_DELIVERY,)
    if (
        event.kind == "tool_result"
        and event.provenance is not None
        and event.provenance.source == "codex_app_server"
    ):
        return (EvidenceFamily.MCP_CALL_RESULT, EvidenceFamily.TOOL_OUTCOME)
    if event.kind == "tool_proposal":
        tool = event.payload.get("tool")
        if tool == "apply_patch":
            return (EvidenceFamily.FILE_EDIT_DIFF,)
        return (EvidenceFamily.COMMAND_TOOL_CALL,)
    return _TRACE_FAMILY.get(event.kind, ())


def _trace_status(event: TraceEvent) -> CoverageStatus:
    if event.provenance is None:
        return CoverageStatus.LEGACY_UNAVAILABLE
    if event.provenance.source == "codex_hook":
        if event.kind == "tool_result" and not _hook_outcome_visible(event.payload):
            return CoverageStatus.OBSERVED_PARTIAL
        return CoverageStatus.OBSERVED_EXACT
    if event.kind == "runtime_event_unknown":
        return CoverageStatus.UNKNOWN
    if event.provenance.source == "codex_app_server" and (
        event.identity is None or event.identity.provenance.agent_thread_id is None
    ):
        return CoverageStatus.OBSERVED_UNCORRELATED
    return CoverageStatus.OBSERVED_EXACT


def _hook_outcome_visible(payload: Mapping[str, Any]) -> bool:
    return outcome_failure(payload) is not None


def state_coverage_status(event: TraceEvent, state: ThreadState) -> CoverageStatus:
    """Classify whether normalized evidence survives in live supervisory state."""

    if event.kind == "runtime_event_unknown":
        return CoverageStatus.UNKNOWN
    if event.kind == "thread_started":
        return CoverageStatus.OBSERVED_EXACT
    if event.kind == "token_usage":
        return CoverageStatus.ADAPTER_DROPPED
    if event.kind in {"thread_status", "diff_updated"}:
        return CoverageStatus.OBSERVED_PARTIAL
    if event.kind == "turn_started":
        turn_id = event.identity.turn_id if event.identity is not None else None
        return (
            CoverageStatus.OBSERVED_EXACT
            if turn_id is not None and state.active_turn_id == turn_id
            else CoverageStatus.ADAPTER_DROPPED
        )
    if event.kind == "turn_completed":
        turn_id = event.identity.turn_id if event.identity is not None else None
        return (
            CoverageStatus.OBSERVED_EXACT
            if turn_id is not None and turn_id in state.execution.completed_turns
            else CoverageStatus.ADAPTER_DROPPED
        )
    if event.kind == "user_prompt":
        if event.payload.get("input_origin") == "spotter_supervision":
            return _state_collection_status(event, state.supervision.interventions)
        item = state.task.goal
        return _state_item_status(event, item.provenance.event_id if item else None)
    if event.kind == "plan":
        item = state.execution.plan_summary
        return _state_item_status(event, item.provenance.event_id if item else None)
    if event.kind == "reasoning_summary":
        return _state_evidence_status(event, state)
    if event.kind in {"command_started", "tool_started", "file_change_started"}:
        return (
            CoverageStatus.OBSERVED_EXACT
            if event.operation_id is not None and event.operation_id in state.execution.active_items
            else CoverageStatus.ADAPTER_DROPPED
        )
    if event.kind in {"command_result", "tool_result", "file_edit", "search"}:
        return _state_evidence_status(event, state)
    return CoverageStatus.UNKNOWN


def _families_for(method: str, item_type: str | None) -> tuple[EvidenceFamily, ...]:
    if item_type is not None and item_type in _ITEM_FAMILY:
        return _ITEM_FAMILY[item_type]
    return _METHOD_FAMILY.get(method, ())


def _path_has_value(raw: Mapping[str, Any], path: str) -> bool:
    value: object = raw
    for component in path.split("."):
        if not isinstance(value, Mapping) or component not in value:
            return False
        value = value[component]
    return value is not None


def _is_encrypted_field(field: str) -> bool:
    return any(segment.casefold() == "encrypted" for segment in _FIELD_SEGMENT.split(field))


def _required_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise TypeError(f"{key} is not a non-empty string")
    return value


def _string_tuple(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{key} is not a string list")
    return tuple(value)


def _optional_string(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} is not a string or null")
    return value


def _state_item_status(event: TraceEvent, state_event_id: str | None) -> CoverageStatus:
    if event.event_id is None:
        return CoverageStatus.OBSERVED_PARTIAL
    return (
        CoverageStatus.OBSERVED_EXACT
        if state_event_id == event.event_id
        else CoverageStatus.ADAPTER_DROPPED
    )


def _state_evidence_status(event: TraceEvent, state: ThreadState) -> CoverageStatus:
    return _state_collection_status(event, state.evidence.items)


def _state_collection_status(event: TraceEvent, items: tuple[StateItem, ...]) -> CoverageStatus:
    if event.event_id is None:
        return CoverageStatus.OBSERVED_PARTIAL
    return (
        CoverageStatus.OBSERVED_EXACT
        if any(item.provenance.event_id == event.event_id for item in items)
        else CoverageStatus.ADAPTER_DROPPED
    )
