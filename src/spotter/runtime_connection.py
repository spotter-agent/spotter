"""Daemon-owned App Server reconnect, reconciliation, and epoch fencing."""

import asyncio
import contextlib
import hashlib
import json
import random
import time
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from spotter.app_server import (
    AppServerCapabilities,
    AppServerError,
    AppServerTransportError,
    CapabilityStatus,
    CodexAppServerClient,
)
from spotter.identity import AttachmentId, RuntimeIdentity, ThreadId
from spotter.ingestion import AppServerTraceIngestor, IngestionError
from spotter.observability import state_coverage_status
from spotter.signals import SignalEngine
from spotter.snapshot import StepRecord
from spotter.thread_state import ThreadState, ThreadStateError, ThreadStateStore
from spotter.trace import TraceEvent, TraceProvenance


class RecoveryState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    RECONCILING = "reconciling"
    READY = "ready"
    DEGRADED = "degraded"
    BACKING_OFF = "backing_off"


@dataclass(frozen=True)
class ConnectionIdentity:
    runtime_attachment_id: str
    connection_epoch: int
    endpoint_fingerprint: str
    server_fingerprint: str
    connected_at: float
    capabilities: AppServerCapabilities
    server_changed: bool


@dataclass(frozen=True)
class RecoveryMetrics:
    reconnect_successes: int = 0
    reconnect_failures: int = 0
    observation_gaps: int = 0
    last_recovery_seconds: float | None = None


@dataclass(frozen=True)
class RuntimeControlTarget:
    identity: RuntimeIdentity
    connection_epoch: int


class StaleControlTarget(AppServerError):
    """A live control request no longer names the exact reconciled epoch and turn."""


StateCallback = Callable[[RecoveryState, str | None], None]
ClientFactory = Callable[[str], CodexAppServerClient]


class AppServerRecoveryLoop:
    """Own exactly one App Server connection and conservatively reconcile it."""

    def __init__(
        self,
        endpoint: str,
        journals_dir: Path,
        thread_states: ThreadStateStore,
        *,
        on_state: StateCallback | None = None,
        client_factory: ClientFactory | None = None,
        signals: SignalEngine | None = None,
        initial_backoff: float = 0.1,
        maximum_backoff: float = 30,
    ) -> None:
        if not endpoint.strip():
            raise ValueError("App Server endpoint must be non-empty")
        if initial_backoff < 0 or maximum_backoff < initial_backoff:
            raise ValueError("invalid reconnect backoff")
        self.endpoint = endpoint
        self.thread_states = thread_states
        self.ingestor = AppServerTraceIngestor(journals_dir)
        self.on_state = on_state
        self.client_factory = client_factory or (lambda value: CodexAppServerClient(value))
        self.signals = signals or SignalEngine()
        self.initial_backoff = initial_backoff
        self.maximum_backoff = maximum_backoff
        self.state = RecoveryState.DISCONNECTED
        self.connection: ConnectionIdentity | None = None
        self.metrics = RecoveryMetrics()
        self.last_error: str | None = None
        self.transitions: tuple[RecoveryState, ...] = (self.state,)
        self._connection_epoch = self.ingestor.last_connection_epoch
        self._client: CodexAppServerClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._retry = asyncio.Event()
        self._attachments: dict[ThreadId, AttachmentId] = {}
        self._disconnected_at: float | None = None

        records = self.ingestor.records()
        if records:
            self.thread_states.hydrate(records)
            for candidate, trigger in self.signals.hydrate(records):
                self._record(candidate.to_trace_event(trigger))

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="spotter-app-server-recovery")

    async def close(self) -> None:
        self._stop.set()
        self._retry.set()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._set_state(RecoveryState.DISCONNECTED)

    def retry_now(self) -> None:
        self._retry.set()

    async def steer(self, target: RuntimeControlTarget, text: str) -> Mapping[str, Any]:
        client, thread_id, turn_id = self._validate_target(target)
        return await client.steer(thread_id, turn_id, text)

    async def interrupt(self, target: RuntimeControlTarget) -> Mapping[str, Any]:
        client, thread_id, turn_id = self._validate_target(target)
        return await client.interrupt(thread_id, turn_id)

    async def _run(self) -> None:
        delay = self.initial_backoff
        while not self._stop.is_set():
            self._set_state(RecoveryState.CONNECTING)
            client = self.client_factory(self.endpoint)
            self._client = client
            try:
                async with AsyncExitStack() as stack:
                    await client.connect()
                    stack.push_async_callback(client.disconnect)
                    self._connection_epoch += 1
                    epoch = self._connection_epoch
                    attachment_id = uuid4().hex
                    connected_at = time.time()
                    server_fingerprint = _fingerprint(client.server_info or {})
                    previous_server = (
                        self.connection.server_fingerprint if self.connection is not None else None
                    )
                    self.connection = ConnectionIdentity(
                        attachment_id,
                        epoch,
                        _fingerprint(self.endpoint),
                        server_fingerprint,
                        connected_at,
                        client.capabilities,
                        previous_server is not None and previous_server != server_fingerprint,
                    )
                    consumer = asyncio.create_task(self._consume(client, epoch, attachment_id))
                    stack.push_async_callback(_cancel_task, consumer)
                    self._set_state(RecoveryState.RECONCILING)
                    started = time.monotonic()
                    await self._reconcile(client, epoch, attachment_id)
                    if epoch != self._connection_epoch or self._stop.is_set():
                        continue
                    self.connection = replace(self.connection, capabilities=client.capabilities)
                    self.metrics = replace(
                        self.metrics,
                        reconnect_successes=self.metrics.reconnect_successes + 1,
                        last_recovery_seconds=time.monotonic() - started,
                    )
                    self.last_error = None
                    self._set_state(RecoveryState.READY)
                    delay = self.initial_backoff
                    error = await client.wait_closed()
                    if self._stop.is_set():
                        break
                    raise error or AppServerTransportError("App Server connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = str(error)
                self.metrics = replace(
                    self.metrics,
                    reconnect_failures=self.metrics.reconnect_failures + 1,
                )
                self._disconnected_at = time.time()
                self._set_state(RecoveryState.DEGRADED, self.last_error)
            finally:
                self._client = None
                self._detach_all()
            if self._stop.is_set():
                break
            self._set_state(RecoveryState.BACKING_OFF, self.last_error)
            await self._wait_backoff(delay + random.uniform(0, delay * 0.2))
            delay = min(max(delay * 2, self.initial_backoff), self.maximum_backoff)

    async def _consume(self, client: CodexAppServerClient, epoch: int, attachment_id: str) -> None:
        while epoch == self._connection_epoch and not self._stop.is_set():
            try:
                raw_event = await client.next_event()
                if epoch != self._connection_epoch:
                    return
                event = self.ingestor.normalizer.normalize(raw_event)
                event = replace(
                    event,
                    identity=self._epoch_identity(event.identity, attachment_id),
                    connection_epoch=epoch,
                )
                record = self._record(event)
                state_status = None
                if (
                    record is not None
                    and event.identity is not None
                    and event.identity.thread_id is not None
                ):
                    state = self.thread_states.snapshot(event.identity.thread_id)
                    state_status = state_coverage_status(record.event, state)
                self.ingestor.audit_source(
                    raw_event,
                    record.event if record is not None else event,
                    disposition="ingested" if record is not None else "deduplicated",
                    state_status=state_status,
                )
            except IngestionError as error:
                self.last_error = str(error)
            except AppServerError:
                return
            except Exception as error:
                message = f"App Server event consumer failed: {error}"
                self.last_error = message
                self._set_state(RecoveryState.DEGRADED, message)
                await client.disconnect()
                raise AppServerTransportError(message) from error

    async def _reconcile(
        self,
        client: CodexAppServerClient,
        epoch: int,
        attachment_id: str,
    ) -> None:
        discovered: dict[str, Mapping[str, Any]] = {}
        cursor: str | None = None
        while True:
            page = await client.list_threads(limit=100, cursor=cursor)
            data = page.get("data")
            if not isinstance(data, list):
                raise AppServerTransportError("thread/list returned no data array")
            for value in data:
                if isinstance(value, Mapping) and isinstance(value.get("id"), str):
                    discovered[value["id"]] = value
            cursor = page.get("nextCursor") if isinstance(page.get("nextCursor"), str) else None
            if cursor is None:
                break

        known = {
            state.identity.provenance.agent_thread_id: state
            for state in self.thread_states.snapshots()
            if state.identity.provenance.agent_thread_id is not None
        }
        capabilities = _available_capabilities(client.capabilities)
        ended_at = time.time()
        for external_thread_id in sorted(discovered):
            result = await client.read_thread(external_thread_id, include_turns=True)
            thread = result.get("thread")
            thread = thread if isinstance(thread, Mapping) else discovered[external_thread_id]
            active_turn_id = _active_turn_id(thread)
            identity = self._runtime_identity(external_thread_id, active_turn_id, attachment_id)
            assert identity.thread_id is not None
            previous = known.get(external_thread_id)
            if previous is None or previous.connection_epoch != epoch:
                self._record_gap(
                    identity,
                    previous,
                    epoch,
                    ended_at,
                    (
                        "reconnect"
                        if previous is not None and self.metrics.reconnect_successes > 0
                        else "daemon_restart"
                        if previous is not None
                        else "new_attachment"
                    ),
                )
            self._record(
                TraceEvent(
                    "runtime_reconciled",
                    {
                        "active_turn": active_turn_id is not None,
                        "capabilities": list(capabilities),
                        "runtime_attachment_id": attachment_id,
                    },
                    event_id=f"spotter:reconciled:{identity.thread_id.value}:{epoch}",
                    occurred_at=ended_at,
                    identity=identity,
                    provenance=TraceProvenance("spotterd", "runtime_reconciled"),
                    connection_epoch=epoch,
                )
            )

        for external_thread_id in sorted(known.keys() - discovered.keys()):
            state = known[external_thread_id]
            self._record_gap(
                state.identity,
                state,
                epoch,
                ended_at,
                "daemon_restart" if self.metrics.reconnect_successes == 0 else "reconnect",
            )
            self._record(
                TraceEvent(
                    "runtime_attachment_unavailable",
                    {"runtime_attachment_id": attachment_id},
                    event_id=f"spotter:unavailable:{state.thread_id.value}:{epoch}",
                    occurred_at=ended_at,
                    identity=state.identity,
                    provenance=TraceProvenance("spotterd", "runtime_attachment_unavailable"),
                    connection_epoch=epoch,
                )
            )

    def _record_gap(
        self,
        identity: RuntimeIdentity,
        previous: ThreadState | None,
        epoch: int,
        ended_at: float,
        source: str,
    ) -> None:
        if identity.thread_id is None:
            return
        epoch_before = previous.connection_epoch if previous is not None else None
        self._record(
            TraceEvent(
                "observation_gap",
                {
                    "started_at": self._disconnected_at,
                    "ended_at": ended_at,
                    "epoch_before": epoch_before,
                    "epoch_after": epoch,
                    "backfill_status": "none",
                    "recovery_source": source,
                },
                event_id=f"spotter:gap:{identity.thread_id.value}:{epoch_before}:{epoch}",
                occurred_at=ended_at,
                identity=identity,
                provenance=TraceProvenance("spotterd", "observation_gap"),
                connection_epoch=epoch,
            )
        )
        self.metrics = replace(self.metrics, observation_gaps=self.metrics.observation_gaps + 1)

    def _record(self, event: TraceEvent) -> StepRecord | None:
        record = self.ingestor.record(event)
        if record is None or event.identity is None or event.identity.thread_id is None:
            return record
        state = self.thread_states.observe(record.event)
        for candidate in self.signals.update(record.event, state.version):
            self._record(candidate.to_trace_event(record.event))
        return record

    def _runtime_identity(
        self,
        external_thread_id: str,
        external_turn_id: str | None,
        attachment_id: str,
    ) -> RuntimeIdentity:
        registry = self.ingestor.normalizer.identities
        thread = registry.observe_thread("codex", external_thread_id)
        attachment = registry.attach(thread.id, agent_attachment_id=attachment_id)
        self._attachments[thread.id] = attachment.id
        if external_turn_id is None:
            return RuntimeIdentity(
                thread.id,
                None,
                attachment.id,
                replace(thread.provenance, agent_attachment_id=attachment_id),
            )
        turn = registry.start_turn(thread.id, external_turn_id, observed_start=False)
        return RuntimeIdentity(
            thread.id,
            turn.id,
            attachment.id,
            replace(turn.provenance, agent_attachment_id=attachment_id),
        )

    def _epoch_identity(
        self, identity: RuntimeIdentity | None, attachment_id: str
    ) -> RuntimeIdentity | None:
        if identity is None or identity.thread_id is None:
            return identity
        registry = self.ingestor.normalizer.identities
        attachment = registry.attach(identity.thread_id, agent_attachment_id=attachment_id)
        self._attachments[identity.thread_id] = attachment.id
        return replace(
            identity,
            attachment_id=attachment.id,
            provenance=replace(
                identity.provenance,
                agent_attachment_id=attachment_id,
            ),
        )

    def _validate_target(
        self, target: RuntimeControlTarget
    ) -> tuple[CodexAppServerClient, str, str]:
        identity = target.identity
        client = self._client
        if (
            self.state != RecoveryState.READY
            or self.connection is None
            or client is None
            or target.connection_epoch != self.connection.connection_epoch
            or identity.thread_id is None
            or identity.turn_id is None
        ):
            raise StaleControlTarget("control target is not ready in the current connection epoch")
        try:
            state = self.thread_states.snapshot(identity.thread_id)
        except ThreadStateError as error:
            raise StaleControlTarget("control target names an unknown thread") from error
        if (
            not state.control_ready
            or state.connection_epoch != target.connection_epoch
            or state.active_turn_id != identity.turn_id
        ):
            raise StaleControlTarget("control target no longer names the active reconciled turn")
        thread_id = identity.provenance.agent_thread_id
        turn_id = identity.provenance.agent_turn_id
        if thread_id is None or turn_id is None:
            raise StaleControlTarget("control target has no agent thread/turn provenance")
        return client, thread_id, turn_id

    def _detach_all(self) -> None:
        registry = self.ingestor.normalizer.identities
        for attachment_id in self._attachments.values():
            with contextlib.suppress(Exception):
                registry.detach(attachment_id)
        self._attachments.clear()

    async def _wait_backoff(self, delay: float) -> None:
        self._retry.clear()
        stop = asyncio.create_task(self._stop.wait())
        retry = asyncio.create_task(self._retry.wait())
        try:
            await asyncio.wait({stop, retry}, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await _cancel_task(stop)
            await _cancel_task(retry)

    def _set_state(self, state: RecoveryState, detail: str | None = None) -> None:
        if self.state != state:
            self.state = state
            self.transitions += (state,)
        if self.on_state is not None:
            self.on_state(state, detail)


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _active_turn_id(thread: Mapping[str, Any]) -> str | None:
    active = thread.get("activeTurn")
    active_id = active.get("id") if isinstance(active, Mapping) else None
    if isinstance(active_id, str):
        return active_id
    turns = thread.get("turns")
    if isinstance(turns, list):
        for turn in reversed(turns):
            if not isinstance(turn, Mapping):
                continue
            status = turn.get("status")
            turn_id = turn.get("id")
            if status in {"active", "inProgress", "running"} and isinstance(turn_id, str):
                return turn_id
    return None


def _available_capabilities(capabilities: AppServerCapabilities) -> tuple[str, ...]:
    return tuple(
        name
        for name in ("observation", "thread_query", "steer", "interrupt")
        if getattr(capabilities, name) == CapabilityStatus.AVAILABLE
    )


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
