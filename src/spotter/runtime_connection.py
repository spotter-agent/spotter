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
    AppServerControlError,
    AppServerError,
    AppServerEvent,
    AppServerRpcError,
    AppServerTransportError,
    CapabilityStatus,
    CodexAppServerClient,
    ControlFailureReason,
)
from spotter.config import McpToolSemantics
from spotter.identity import AttachmentId, RuntimeIdentity, ThreadId
from spotter.ingestion import AppServerTraceIngestor, IngestionError
from spotter.observability import state_coverage_status
from spotter.review_scheduler import ReviewerJob, ReviewScheduler
from spotter.reviewer import ReviewerDecision
from spotter.signals import SignalEngine, deterministic_block_equivalence
from spotter.snapshot import StepRecord, capture_receipt_timing
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
    server_version: str | None
    connected_at: float
    capabilities: AppServerCapabilities
    server_changed: bool
    capabilities_changed: bool


@dataclass(frozen=True)
class RecoveryMetrics:
    reconnect_successes: int = 0
    reconnect_failures: int = 0
    observation_gaps: int = 0
    last_recovery_seconds: float | None = None
    control_telemetry_dropped: int = 0
    control_telemetry_errors: int = 0
    control_telemetry_backlog_peak: int = 0


@dataclass(frozen=True)
class RuntimeControlTarget:
    identity: RuntimeIdentity
    connection_epoch: int


@dataclass(frozen=True)
class _QueuedControlEvent:
    event: TraceEvent
    observed_at: float


class StaleControlTarget(AppServerError):
    """A live control request no longer names the exact reconciled epoch and turn."""

    def __init__(self, message: str, reason_code: str = "stale_target") -> None:
        super().__init__(message)
        self.reason_code = reason_code


StateCallback = Callable[[RecoveryState, str | None], None]
ClientFactory = Callable[[str], CodexAppServerClient]
ReviewJobCallback = Callable[[ReviewerJob], None]
TurnBoundaryCallback = Callable[[], object]


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
        review_scheduler: ReviewScheduler | None = None,
        on_review_job: ReviewJobCallback | None = None,
        on_turn_boundary: TurnBoundaryCallback | None = None,
        initial_backoff: float = 0.1,
        maximum_backoff: float = 30,
        control_telemetry_queue_size: int = 256,
        interrupt_settlement_timeout: float = 30.0,
        mcp_semantics: tuple[McpToolSemantics, ...] = (),
        snapshot_on_patch: bool = True,
        config_generation: str = "unversioned",
    ) -> None:
        if not endpoint.strip():
            raise ValueError("App Server endpoint must be non-empty")
        if initial_backoff < 0 or maximum_backoff < initial_backoff:
            raise ValueError("invalid reconnect backoff")
        if control_telemetry_queue_size <= 0:
            raise ValueError("control telemetry queue size must be positive")
        if interrupt_settlement_timeout <= 0:
            raise ValueError("interrupt settlement timeout must be positive")
        self.endpoint = endpoint
        self.thread_states = thread_states
        self.ingestor = AppServerTraceIngestor(
            journals_dir, mcp_semantics, snapshot_on_patch=snapshot_on_patch
        )
        self.on_state = on_state
        self.client_factory = client_factory or (lambda value: CodexAppServerClient(value))
        self.signals = signals or SignalEngine()
        self.review_scheduler = review_scheduler or ReviewScheduler()
        self.config_generation = config_generation
        self.on_review_job = on_review_job
        self.on_turn_boundary = on_turn_boundary
        self.initial_backoff = initial_backoff
        self.maximum_backoff = maximum_backoff
        self.state = RecoveryState.DISCONNECTED
        self.connection: ConnectionIdentity | None = None
        self.metrics = RecoveryMetrics()
        self.last_error: str | None = None
        self.last_control_telemetry_error: str | None = None
        self.transitions: tuple[RecoveryState, ...] = (self.state,)
        self._connection_epoch = self.ingestor.last_connection_epoch
        self._client: CodexAppServerClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._retry = asyncio.Event()
        self._attachments: dict[ThreadId, AttachmentId] = {}
        self._subscribed_threads: set[str] = set()
        self._disconnected_at: float | None = None
        self._control_telemetry_queue_size = control_telemetry_queue_size
        self._control_telemetry_queue: asyncio.Queue[_QueuedControlEvent] | None = None
        self._control_telemetry_writer: asyncio.Task[None] | None = None
        self._control_telemetry_ids: set[str] = set()
        self._control_request_ids: set[str] = set()
        self._control_requests: dict[str, dict[str, object]] = {}
        self._accepted_controls: dict[str, dict[str, object]] = {}
        self._observed_control_ids: set[str] = set()
        self._terminal_turn_status: dict[str, str | None] = {}
        self._interrupt_target_turns: set[str] = set()
        self._interrupt_settlement_timeout = interrupt_settlement_timeout
        self._settled_interrupt_ids: set[str] = set()
        self._interrupt_deadlines: dict[str, asyncio.Task[None]] = {}

        records = self.ingestor.records()
        self._control_telemetry_ids.update(
            record.event.event_id
            for record in records
            if record.event.event_id is not None and record.event.kind.startswith("control_")
        )
        self._control_request_ids.update(
            control_id
            for record in records
            if (control_id := record.event.payload.get("control_id")) is not None
            and isinstance(control_id, str)
            and control_id
        )
        pending_interrupts: dict[str, tuple[RuntimeControlTarget, dict[str, object]]] = {}
        for record in records:
            control_id = record.event.payload.get("control_id")
            if (
                isinstance(control_id, str)
                and control_id
                and record.event.payload.get("intervention_id") == control_id
                and record.event.payload.get("outcome") != "stale"
            ):
                self._control_requests.setdefault(control_id, dict(record.event.payload))
                if record.event.kind == "control_rpc_accepted":
                    self._accepted_controls.setdefault(control_id, dict(record.event.payload))
                elif record.event.kind in {
                    "control_observed_in_turn",
                    "control_observed_outside_target",
                }:
                    self._observed_control_ids.add(control_id)
            # Interrupts carry no intervention_id — that field belongs to the
            # advisory-input correlation the steer path needs — so they are
            # hydrated on their own terms or a restart forgets them entirely.
            if (
                isinstance(control_id, str)
                and control_id
                and record.event.payload.get("control_kind") == "interrupt"
            ):
                if record.event.kind == "control_rpc_accepted":
                    self._accepted_controls.setdefault(control_id, dict(record.event.payload))
                    if record.event.identity is not None:
                        pending_interrupts[control_id] = (
                            RuntimeControlTarget(
                                record.event.identity, record.event.connection_epoch or 0
                            ),
                            dict(record.event.payload),
                        )
                elif record.event.kind == "control_terminal":
                    self._settled_interrupt_ids.add(control_id)
                    pending_interrupts.pop(control_id, None)
        self._pending_interrupt_deadlines = tuple(pending_interrupts.values())
        # A resumed interrupt still needs its target turn watched, or the status
        # lookup would be empty for the one boundary it is waiting on.
        for _, payload in self._pending_interrupt_deadlines:
            target_turn = payload.get("target_turn_id")
            if isinstance(target_turn, str) and target_turn:
                self._interrupt_target_turns.add(target_turn)
        if records:
            self.thread_states.hydrate(records)
            for candidate, trigger in self.signals.hydrate(records):
                self._append_derived(candidate.to_trace_event(trigger))
            for event in self.review_scheduler.hydrate(self.ingestor.records()):
                self._append_derived(event)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        # Armed here rather than during hydration because __init__ has no running
        # loop. A pre-restart interrupt gets a fresh window to settle from a
        # reconnected server, and the deadline still bounds it if none arrives.
        for target, payload in self._pending_interrupt_deadlines:
            self._arm_interrupt_deadline(target, payload)
        self._pending_interrupt_deadlines = ()
        self._task = asyncio.create_task(self._run(), name="spotter-app-server-recovery")

    async def close(self) -> None:
        self._stop.set()
        self._retry.set()
        for deadline in self._interrupt_deadlines.values():
            if not deadline.done():
                deadline.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await deadline
        self._interrupt_deadlines.clear()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self.flush_control_telemetry()
        if self._control_telemetry_writer is not None:
            self._control_telemetry_writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._control_telemetry_writer
            self._control_telemetry_writer = None
            self._control_telemetry_queue = None
        self._set_state(RecoveryState.DISCONNECTED)

    async def flush_control_telemetry(self) -> None:
        """Wait until control telemetry already accepted by the queue is durable."""

        if self._control_telemetry_queue is not None:
            await self._control_telemetry_queue.join()

    def retry_now(self) -> None:
        self._retry.set()

    def set_review_job_callback(self, callback: ReviewJobCallback) -> None:
        self.on_review_job = callback

    def update_hot_config(self, *, snapshot_on_patch: bool, config_generation: str) -> None:
        """Apply settings whose contract permits immediate use by future observations."""

        self.ingestor.snapshot_on_patch = snapshot_on_patch
        self.config_generation = config_generation

    def update_turn_config(
        self,
        *,
        mcp_semantics: tuple[McpToolSemantics, ...],
        snapshot_on_patch: bool,
        config_generation: str,
    ) -> None:
        """Publish a full turn-bound config before normalizing the next turn."""

        self.ingestor.normalizer.mcp_semantics = mcp_semantics
        self.ingestor.snapshot_on_patch = snapshot_on_patch
        self.config_generation = config_generation

    def record_review_event(self, event: TraceEvent) -> StepRecord | None:
        return self._record(event)

    def record_gate_decision(
        self, params: Mapping[str, object], decision: Mapping[str, object]
    ) -> None:
        """Project an exact live Hook gate rejection into the signal timeline."""

        identity = params.get("identity")
        proposal = params.get("proposal")
        if not isinstance(identity, Mapping) or not isinstance(proposal, Mapping):
            return
        external_thread_id = identity.get("thread_id")
        external_turn_id = identity.get("turn_id")
        tool_use_id = proposal.get("tool_use_id")
        rule = decision.get("rule")
        config_generation = params.get("config_generation")
        if not all(
            isinstance(value, str) and value
            for value in (external_thread_id, external_turn_id, tool_use_id, rule)
        ):
            return
        equivalence = deterministic_block_equivalence(rule, proposal)
        if equivalence is None:
            return
        key, resources = equivalence
        states = [
            state
            for state in self.thread_states.snapshots()
            if state.control_ready
            and state.active_turn_id == state.identity.turn_id
            and state.identity.provenance.agent_thread_id == external_thread_id
            and state.identity.provenance.agent_turn_id == external_turn_id
        ]
        if len(states) != 1 or states[0].connection_epoch is None:
            return
        state = states[0]
        source_id = hashlib.sha256(
            f"{state.thread_id.value}:{external_turn_id}:{tool_use_id}".encode()
        ).hexdigest()[:24]
        self._record(
            TraceEvent(
                "deterministic_gate_block",
                {
                    "rule": rule,
                    "tool_use_id": tool_use_id,
                    "equivalence_key": key,
                    "involved_resources": list(resources),
                    "config_generation": (
                        config_generation
                        if isinstance(config_generation, str) and config_generation
                        else "unversioned"
                    ),
                },
                event_id=f"spotter:gate-block:{source_id}",
                occurred_at=time.time(),
                identity=state.identity,
                provenance=TraceProvenance("spotterd", "gate_bridge"),
                connection_epoch=state.connection_epoch,
                config_generation=(
                    config_generation
                    if isinstance(config_generation, str) and config_generation
                    else "unversioned"
                ),
            )
        )

    def review_job_is_fresh(self, job: ReviewerJob) -> bool:
        if self.review_scheduler.get(job.job_id) is None:
            return False
        try:
            state = self.thread_states.snapshot(job.thread_id)
        except ThreadStateError:
            return False
        terminal_answer = state.execution.terminal_answer
        return (
            state.active_turn_id == job.target_turn_id
            and state.connection_epoch == job.target_connection_epoch
            and (
                terminal_answer is None or terminal_answer.provenance.turn_id != job.target_turn_id
            )
        )

    async def steer(
        self,
        target: RuntimeControlTarget,
        text: str,
        *,
        control_id: str | None = None,
        review_job_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self._dispatch_control(
            target,
            "steer",
            text=text,
            control_id=control_id,
            review_job_id=review_job_id,
        )

    async def deliver_review_decision(self, job: ReviewerJob, decision: ReviewerDecision) -> None:
        """Deliver one current-turn advisory without retargeting or retrying it."""

        if job.target_connection_epoch is None:
            raise StaleControlTarget("review job has no controllable connection epoch")
        intervention_id = _intervention_id(job.job_id)
        await self.steer(
            RuntimeControlTarget(job.snapshot.identity, job.target_connection_epoch),
            _review_advisory(decision, intervention_id),
            control_id=intervention_id,
            review_job_id=job.job_id,
        )

    async def interrupt(
        self,
        target: RuntimeControlTarget,
        *,
        control_id: str | None = None,
        review_job_id: str | None = None,
    ) -> Mapping[str, Any]:
        return await self._dispatch_control(
            target,
            "interrupt",
            control_id=control_id,
            review_job_id=review_job_id,
        )

    async def _dispatch_control(
        self,
        target: RuntimeControlTarget,
        control_kind: str,
        *,
        text: str | None = None,
        control_id: str | None = None,
        review_job_id: str | None = None,
    ) -> Mapping[str, Any]:
        request_id = _control_request_id(control_id)
        while control_id is None and request_id in self._control_request_ids:
            request_id = _control_request_id(None)
        payload = _control_payload(target, request_id, control_kind, review_job_id)
        if request_id in self._control_request_ids:
            raise ValueError("control_id must be unique across durable runtime history")
        self._control_request_ids.add(request_id)
        try:
            client, thread_id, turn_id = self._validate_target(
                target, reject_terminal_answer=control_kind == "steer"
            )
        except StaleControlTarget as error:
            self._record_control_event(
                "control_terminal",
                target,
                payload,
                outcome="stale",
                reason_code=error.reason_code,
                error=error,
            )
            raise

        if payload.get("intervention_id") == request_id:
            self._control_requests[request_id] = dict(payload)
        if control_kind == "interrupt":
            # Registered before the RPC: the boundary can be observed before the
            # ack, and that is exactly the case the status lookup exists for.
            target_turn = payload.get("target_turn_id")
            if isinstance(target_turn, str) and target_turn:
                self._interrupt_target_turns.add(target_turn)
        self._record_control_event("control_dispatch_started", target, payload)
        try:
            if control_kind == "steer":
                assert text is not None
                result = await client.steer(
                    thread_id,
                    turn_id,
                    text,
                    client_user_message_id=request_id,
                )
            else:
                result = await client.interrupt(thread_id, turn_id)
        except asyncio.CancelledError as error:
            self._record_control_event(
                "control_terminal",
                target,
                payload,
                outcome="unknown",
                reason_code="cancelled_after_dispatch",
                error=error,
            )
            raise
        except AppServerControlError as error:
            stale = error.reason in {
                ControlFailureReason.NO_ACTIVE_TURN,
                ControlFailureReason.TURN_MISMATCH,
            }
            self._record_control_event(
                "control_terminal",
                target,
                payload,
                outcome="stale" if stale else "failed",
                reason_code=error.reason.value,
                error=error,
            )
            raise
        except AppServerRpcError as error:
            self._record_control_event(
                "control_terminal",
                target,
                payload,
                outcome="failed",
                reason_code="rpc_rejected",
                error=error,
            )
            raise
        except AppServerError as error:
            self._record_control_event(
                "control_terminal",
                target,
                payload,
                outcome="unknown",
                reason_code="acceptance_unknown",
                error=error,
            )
            raise
        finally:
            self._refresh_connection_capabilities(client, target.connection_epoch)

        accepted = dict(payload)
        accepted_turn_id = result.get("turnId")
        if isinstance(accepted_turn_id, str) and accepted_turn_id:
            accepted["accepted_turn_id"] = accepted_turn_id
        self._accepted_controls[request_id] = accepted
        self._record_control_event("control_rpc_accepted", target, accepted)
        self._reconcile_settled_acceptance(target, accepted)
        if control_kind == "interrupt":
            self._arm_interrupt_deadline(target, accepted)
        return result

    def _refresh_connection_capabilities(
        self,
        client: CodexAppServerClient,
        epoch: int,
    ) -> None:
        if (
            client is self._client
            and self.connection is not None
            and self.connection.connection_epoch == epoch
        ):
            self.connection = replace(self.connection, capabilities=client.capabilities)

    def _record_control_event(
        self,
        kind: str,
        target: RuntimeControlTarget,
        payload: Mapping[str, object],
        *,
        outcome: str | None = None,
        reason_code: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        event_payload = dict(payload)
        if outcome is not None:
            event_payload["outcome"] = outcome
        if reason_code is not None:
            event_payload["reason_code"] = reason_code
        if error is not None:
            event_payload["error_type"] = type(error).__name__
        control_id = event_payload["control_id"]
        phase = outcome or kind.removeprefix("control_")
        observed_at, observed_monotonic_ns, monotonic_clock_id = capture_receipt_timing()
        self._enqueue_control_event(
            _QueuedControlEvent(
                TraceEvent(
                    kind,
                    event_payload,
                    event_id=f"spotter:control:{control_id}:{phase}",
                    occurred_at=observed_at,
                    identity=target.identity,
                    provenance=TraceProvenance("spotterd", "runtime_control"),
                    connection_epoch=target.connection_epoch,
                    observed_monotonic_ns=observed_monotonic_ns,
                    monotonic_clock_id=monotonic_clock_id,
                ),
                observed_at,
            )
        )

    def _enqueue_control_event(self, queued: _QueuedControlEvent) -> None:
        if queued.event.config_generation is None:
            queued = replace(
                queued,
                event=replace(queued.event, config_generation=self.config_generation),
            )
        event_id = queued.event.event_id
        if event_id is not None and event_id in self._control_telemetry_ids:
            return
        if self._control_telemetry_queue is None:
            self._control_telemetry_queue = asyncio.Queue(
                maxsize=self._control_telemetry_queue_size
            )
        if self._control_telemetry_writer is None or self._control_telemetry_writer.done():
            self._control_telemetry_writer = asyncio.create_task(
                self._write_control_telemetry(), name="spotter-control-telemetry"
            )
        try:
            self._control_telemetry_queue.put_nowait(queued)
        except asyncio.QueueFull:
            self.metrics = replace(
                self.metrics,
                control_telemetry_dropped=self.metrics.control_telemetry_dropped + 1,
            )
            self.last_control_telemetry_error = "control telemetry queue is full"
            return
        if event_id is not None:
            self._control_telemetry_ids.add(event_id)
        self.metrics = replace(
            self.metrics,
            control_telemetry_backlog_peak=max(
                self.metrics.control_telemetry_backlog_peak,
                self._control_telemetry_queue.qsize(),
            ),
        )

    async def _write_control_telemetry(self) -> None:
        assert self._control_telemetry_queue is not None
        queue = self._control_telemetry_queue
        while True:
            queued = await queue.get()
            try:
                record = await asyncio.to_thread(
                    self.ingestor.append_operational,
                    queued.event,
                    observed_at=queued.observed_at,
                )
                self.ingestor.index_operational(record)
                self.last_control_telemetry_error = None
            except Exception as error:
                self.metrics = replace(
                    self.metrics,
                    control_telemetry_errors=self.metrics.control_telemetry_errors + 1,
                )
                self.last_control_telemetry_error = str(error)
            finally:
                queue.task_done()

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
                    self._subscribed_threads.clear()
                    attachment_id = uuid4().hex
                    connected_at = time.time()
                    server_fingerprint = _fingerprint(client.server_info or {})
                    previous_connection = self.connection
                    previous_server = (
                        previous_connection.server_fingerprint
                        if previous_connection is not None
                        else None
                    )
                    self.connection = ConnectionIdentity(
                        attachment_id,
                        epoch,
                        _fingerprint(self.endpoint),
                        server_fingerprint,
                        (
                            str(client.host_version)
                            if getattr(client, "host_version", None) is not None
                            else None
                        ),
                        connected_at,
                        client.capabilities,
                        previous_server is not None and previous_server != server_fingerprint,
                        False,
                    )
                    consumer = asyncio.create_task(self._consume(client, epoch, attachment_id))
                    stack.push_async_callback(_cancel_task, consumer)
                    self._set_state(RecoveryState.RECONCILING)
                    started = time.monotonic()
                    await self._reconcile(client, epoch, attachment_id)
                    if epoch != self._connection_epoch or self._stop.is_set():
                        continue
                    capability_changes = _capability_changes(
                        previous_connection.capabilities
                        if previous_connection is not None
                        else None,
                        client.capabilities,
                    )
                    self.connection = replace(
                        self.connection,
                        capabilities=client.capabilities,
                        capabilities_changed=bool(capability_changes),
                    )
                    if previous_connection is not None and (
                        self.connection.server_changed or capability_changes
                    ):
                        self._record_runtime_capability_change(
                            previous_connection,
                            self.connection,
                            capability_changes,
                        )
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
                if raw_event.method == "turn/started" and self.on_turn_boundary is not None:
                    self.on_turn_boundary()
                event = self.ingestor.normalizer.normalize(raw_event)
                event = replace(
                    event,
                    identity=self._epoch_identity(event.identity, attachment_id),
                    connection_epoch=epoch,
                )
                event = self._annotate_intervention_input(event)
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
                await self._subscribe_from_event(client, raw_event)
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

    async def _subscribe_from_event(
        self, client: CodexAppServerClient, raw_event: AppServerEvent
    ) -> None:
        if raw_event.method not in {"thread/started", "thread/status/changed", "turn/started"}:
            return
        params = raw_event.raw.get("params")
        if not isinstance(params, Mapping):
            return
        external_thread_id = params.get("threadId")
        if not isinstance(external_thread_id, str):
            thread = params.get("thread")
            external_thread_id = thread.get("id") if isinstance(thread, Mapping) else None
        if (
            not isinstance(external_thread_id, str)
            or not external_thread_id
            or external_thread_id in self._subscribed_threads
        ):
            return
        try:
            await client.resume_thread(external_thread_id)
        except AppServerRpcError as error:
            if _rollout_pending(error):
                return
            raise RuntimeError(
                f"could not subscribe to App Server thread {external_thread_id}"
            ) from error
        self._subscribed_threads.add(external_thread_id)

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
            try:
                result = await client.resume_thread(external_thread_id)
            except AppServerRpcError as error:
                if not _rollout_pending(error):
                    raise
                result = await client.read_thread(external_thread_id, include_turns=True)
            else:
                self._subscribed_threads.add(external_thread_id)
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

    def _record_runtime_capability_change(
        self,
        previous: ConnectionIdentity,
        current: ConnectionIdentity,
        changes: tuple[tuple[str, str, str], ...],
    ) -> None:
        """Persist reconnect identity/capability drift without exposing server details."""
        payload = {
            "epoch_before": previous.connection_epoch,
            "epoch_after": current.connection_epoch,
            "server_changed": current.server_changed,
            "server_fingerprint_before": previous.server_fingerprint,
            "server_fingerprint_after": current.server_fingerprint,
            "capability_changes": [
                {"capability": name, "before": before, "after": after}
                for name, before, after in changes
            ],
        }
        for state in self.thread_states.snapshots():
            if state.connection_epoch != current.connection_epoch:
                continue
            self._record(
                TraceEvent(
                    "runtime_capabilities_changed",
                    payload,
                    event_id=(
                        f"spotter:capabilities:{state.thread_id.value}:{current.connection_epoch}"
                    ),
                    occurred_at=time.time(),
                    identity=state.identity,
                    provenance=TraceProvenance("spotterd", "runtime_capabilities_changed"),
                    connection_epoch=current.connection_epoch,
                )
            )

    def _record(self, event: TraceEvent) -> StepRecord | None:
        if event.config_generation is None:
            event = replace(event, config_generation=self.config_generation)
        record = self.ingestor.record(event)
        if record is None or event.identity is None or event.identity.thread_id is None:
            return record
        try:
            state_before = self.thread_states.snapshot(event.identity.thread_id)
        except ThreadStateError:
            state_before = None
        state = self.thread_states.observe(record.event)
        if (observation := self._intervention_observation(record.event)) is not None:
            self._append_derived(observation)
            control_id = observation.payload["control_id"]
            assert isinstance(control_id, str)
            self._observed_control_ids.add(control_id)
        if record.event.kind == "turn_completed":
            if record.event.identity is not None and record.event.identity.turn_id is not None:
                # Only turns an interrupt is waiting on. Remembering every turn a
                # long-lived daemon ever observes would grow without bound to
                # serve a lookup that a handful of in-flight interrupts need.
                turn_value = record.event.identity.turn_id.value
                if turn_value in self._interrupt_target_turns:
                    status = record.event.payload.get("status")
                    self._terminal_turn_status[turn_value] = (
                        status if isinstance(status, str) else None
                    )
            self._reconcile_unobserved_acceptances(
                record.event, "target_completed_without_observed_input"
            )
            self._reconcile_interrupt_settlement(record.event)
        elif (
            record.event.kind == "agent_message"
            and record.event.payload.get("phase") == "final_answer"
            and record.event.payload.get("lifecycle") == "completed"
        ):
            self._reconcile_unobserved_acceptances(
                record.event, "terminal_answer_without_observed_input"
            )
        self._record_review_transitions(
            self.review_scheduler.update(record.event, state_before, state)
        )
        candidate_events: list[TraceEvent] = []
        for candidate in self.signals.update(record.event, state.version, state):
            candidate_record = self._append_derived(candidate.to_trace_event(record.event))
            if candidate_record is not None:
                candidate_events.append(candidate_record.event)
        if candidate_events:
            current = self.thread_states.snapshot(event.identity.thread_id)
            self._record_review_transitions(
                self.review_scheduler.update_candidates(candidate_events, state, current)
            )
        return record

    def _annotate_intervention_input(self, event: TraceEvent) -> TraceEvent:
        if event.kind != "user_prompt":
            return event
        control_id = event.payload.get("client_user_message_id")
        control = self._control_requests.get(control_id) if isinstance(control_id, str) else None
        if control is None or control.get("intervention_id") != control_id:
            return event
        identity = event.identity
        live_target = False
        if identity is not None and identity.thread_id is not None:
            try:
                state = self.thread_states.snapshot(identity.thread_id)
            except ThreadStateError:
                pass
            else:
                terminal_answer = state.execution.terminal_answer
                live_target = (
                    state.active_turn_id == identity.turn_id
                    and state.connection_epoch == event.connection_epoch
                    and (
                        terminal_answer is None
                        or terminal_answer.provenance.turn_id != identity.turn_id
                    )
                )
        exact_target = (
            identity is not None
            and identity.turn_id is not None
            and identity.turn_id.value == control.get("target_turn_id")
            and event.connection_epoch == control.get("target_connection_epoch")
            and (
                identity.attachment_id is None
                or identity.attachment_id.value == control.get("runtime_attachment_id")
            )
            and live_target
        )
        return replace(
            event,
            payload={
                **event.payload,
                "input_origin": "spotter_supervision",
                "intervention_id": control_id,
                "review_job_id": control.get("review_job_id"),
                "supervision_scope": control.get("supervision_scope"),
                "must_not_become_user_goal": control.get("must_not_become_user_goal"),
                "expires_on": control.get("expires_on"),
                "intervention_relation": "target_turn" if exact_target else "outside_target",
            },
        )

    def _intervention_observation(self, event: TraceEvent) -> TraceEvent | None:
        if (
            event.kind != "user_prompt"
            or event.payload.get("input_origin") != "spotter_supervision"
        ):
            return None
        control_id = event.payload.get("intervention_id")
        if not isinstance(control_id, str) or not control_id:
            return None
        relation = event.payload.get("intervention_relation")
        in_target = relation == "target_turn"
        control = self._control_requests.get(control_id, {})
        payload = {
            **control,
            "control_id": control_id,
            "intervention_id": control_id,
            "outcome": "observed_in_turn" if in_target else "observed_outside_target",
            "observed_input_event_id": event.event_id,
        }
        if not in_target:
            payload["reason_code"] = "expired_advisory_visible"
        return TraceEvent(
            "control_observed_in_turn" if in_target else "control_observed_outside_target",
            payload,
            event_id=(
                f"spotter:control:{control_id}:observed_in_turn"
                if in_target
                else f"spotter:control:{control_id}:observed_outside_target:{event.event_id}"
            ),
            occurred_at=event.occurred_at,
            identity=event.identity,
            provenance=TraceProvenance("spotterd", "control_reconciliation"),
            connection_epoch=event.connection_epoch,
            observed_monotonic_ns=event.observed_monotonic_ns,
            monotonic_clock_id=event.monotonic_clock_id,
        )

    def _reconcile_unobserved_acceptances(self, boundary: TraceEvent, reason_code: str) -> None:
        identity = boundary.identity
        if identity is None or identity.turn_id is None:
            return
        for control_id, payload in self._accepted_controls.items():
            if (
                control_id in self._observed_control_ids
                or payload.get("control_kind") != "steer"
                or payload.get("target_turn_id") != identity.turn_id.value
                or payload.get("target_connection_epoch") != boundary.connection_epoch
            ):
                continue
            target = RuntimeControlTarget(identity, boundary.connection_epoch or 0)
            self._record_control_event(
                "control_terminal",
                target,
                payload,
                outcome="rpc_accepted_only",
                reason_code=reason_code,
            )

    def _reconcile_interrupt_settlement(self, boundary: TraceEvent) -> None:
        """Settle accepted interrupts against the turn's observed terminal status.

        An accepted `turn/interrupt` RPC is not evidence that the turn stopped:
        the target can still finish normally, which is a different outcome from
        an abort and must not be recorded as one.
        """

        identity = boundary.identity
        if identity is None or identity.turn_id is None:
            return
        outcome, reason_code = _interrupt_settlement(boundary.payload.get("status"))
        for payload in self._accepted_controls.values():
            if (
                payload.get("control_kind") != "interrupt"
                or payload.get("target_turn_id") != identity.turn_id.value
                or payload.get("target_connection_epoch") != boundary.connection_epoch
            ):
                continue
            self._settle_interrupt(
                RuntimeControlTarget(identity, boundary.connection_epoch or 0),
                payload,
                outcome=outcome,
                reason_code=reason_code,
            )

    def _release_interrupt_target(self, target_turn: object) -> None:
        """Forget a turn's terminal status once no interrupt is still waiting on it."""

        if not isinstance(target_turn, str) or not target_turn:
            return
        if any(
            payload.get("control_kind") == "interrupt"
            and payload.get("target_turn_id") == target_turn
            and control_id not in self._settled_interrupt_ids
            for control_id, payload in self._accepted_controls.items()
        ):
            return
        self._interrupt_target_turns.discard(target_turn)
        self._terminal_turn_status.pop(target_turn, None)

    def _settle_interrupt(
        self,
        target: RuntimeControlTarget,
        payload: Mapping[str, object],
        *,
        outcome: str,
        reason_code: str,
    ) -> None:
        """Record one terminal interrupt outcome and retire its deadline."""

        control_id = payload.get("control_id")
        if not isinstance(control_id, str) or control_id in self._settled_interrupt_ids:
            return
        self._settled_interrupt_ids.add(control_id)
        deadline = self._interrupt_deadlines.pop(control_id, None)
        if deadline is not None and not deadline.done():
            deadline.cancel()
        self._release_interrupt_target(payload.get("target_turn_id"))
        self._record_control_event(
            "control_terminal",
            target,
            payload,
            outcome=outcome,
            reason_code=reason_code,
        )

    def _arm_interrupt_deadline(
        self, target: RuntimeControlTarget, payload: Mapping[str, object]
    ) -> None:
        """Bound how long an accepted interrupt may stay unsettled.

        A turn that stops emitting events never produces the `turn/completed`
        that would settle its interrupt, so without this the lifecycle would
        wait forever on evidence that is not coming.
        """

        control_id = payload.get("control_id")
        if not isinstance(control_id, str) or control_id in self._settled_interrupt_ids:
            return
        frozen = dict(payload)

        async def expire() -> None:
            await asyncio.sleep(self._interrupt_settlement_timeout)
            self._settle_interrupt(
                target,
                frozen,
                outcome="unknown",
                reason_code="interrupt_settlement_timeout",
            )

        self._interrupt_deadlines[control_id] = asyncio.create_task(
            expire(), name=f"spotter-interrupt-deadline-{control_id}"
        )

    def _reconcile_settled_acceptance(
        self, target: RuntimeControlTarget, payload: Mapping[str, object]
    ) -> None:
        identity = target.identity
        if identity.thread_id is None or identity.turn_id is None:
            return
        try:
            state = self.thread_states.snapshot(identity.thread_id)
        except ThreadStateError:
            return
        interrupting = payload.get("control_kind") == "interrupt"
        if identity.turn_id in state.execution.completed_turns:
            if interrupting:
                # Settled through the same classifier the boundary path uses, so
                # notification-versus-ACK ordering cannot change the outcome.
                outcome, reason_code = _interrupt_settlement(
                    self._terminal_turn_status.get(identity.turn_id.value)
                )
            else:
                outcome = "rpc_accepted_only"
                reason_code = "target_completed_without_observed_input"
        else:
            terminal_answer = state.execution.terminal_answer
            if terminal_answer is None or terminal_answer.provenance.turn_id != identity.turn_id:
                return
            if interrupting:
                # A final answer is not a terminal turn. An interrupt stays
                # unsettled until turn_completed says whether the trajectory
                # actually aborted, because recovery must not start on a turn
                # that may still be running.
                return
            outcome = "rpc_accepted_only"
            reason_code = "terminal_answer_without_observed_input"
        control_id = payload.get("control_id")
        if not isinstance(control_id, str) or control_id in self._observed_control_ids:
            return
        if interrupting:
            self._settle_interrupt(target, payload, outcome=outcome, reason_code=reason_code)
            return
        self._record_control_event(
            "control_terminal",
            target,
            payload,
            outcome=outcome,
            reason_code=reason_code,
        )

    def _record_review_transitions(self, transitions: tuple[TraceEvent, ...]) -> None:
        for transition in transitions:
            self._record(transition)
            job_id = transition.payload.get("review_job_id")
            if (
                transition.kind == "review_job_queued"
                and isinstance(job_id, str)
                and self.on_review_job is not None
                and (job := self.review_scheduler.get(job_id)) is not None
            ):
                self.on_review_job(job)

    def _append_derived(self, event: TraceEvent) -> StepRecord | None:
        """Recover one already-derived event without running producers out of order."""

        if event.config_generation is None:
            event = replace(event, config_generation=self.config_generation)
        record = self.ingestor.record(event)
        if (
            record is not None
            and event.identity is not None
            and event.identity.thread_id is not None
        ):
            self.thread_states.observe(record.event)
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
        self,
        target: RuntimeControlTarget,
        *,
        reject_terminal_answer: bool,
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
        terminal_answer = state.execution.terminal_answer
        if (
            reject_terminal_answer
            and terminal_answer is not None
            and terminal_answer.provenance.turn_id == identity.turn_id
        ):
            raise StaleControlTarget(
                "control target already has a settled terminal answer",
                "terminal_answer_settled",
            )
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


def _control_request_id(value: str | None) -> str:
    if value is None:
        return f"spotter-{uuid4().hex}"
    if not value.strip():
        raise ValueError("control_id must be non-empty")
    return value


def _control_payload(
    target: RuntimeControlTarget,
    control_id: str,
    control_kind: str,
    review_job_id: str | None,
) -> dict[str, object]:
    if review_job_id is not None and not review_job_id.strip():
        raise ValueError("review_job_id must be non-empty")
    identity = target.identity
    payload: dict[str, object] = {
        "control_id": control_id,
        "control_kind": control_kind,
        "target_connection_epoch": target.connection_epoch,
    }
    if identity.turn_id is not None:
        payload["target_turn_id"] = identity.turn_id.value
    if identity.attachment_id is not None:
        payload["runtime_attachment_id"] = identity.attachment_id.value
    if review_job_id is not None:
        payload["review_job_id"] = review_job_id
    if control_kind == "steer":
        payload["client_user_message_id"] = control_id
        if review_job_id is not None:
            payload.update(
                {
                    "intervention_id": control_id,
                    "supervision_scope": "current_turn",
                    "must_not_become_user_goal": True,
                    "expires_on": "target_turn_terminal",
                }
            )
    return payload


def _interrupt_settlement(turn_status: object) -> tuple[str, str]:
    """Classify one accepted interrupt from the turn's observed terminal status.

    Both the boundary path and the post-ACK path route through this, so whether
    the notification or the RPC reply lands first cannot change the outcome.
    """

    if turn_status == "interrupted":
        return "turn_aborted", "observed_interrupted_status"
    return "turn_completed_otherwise", "target_completed_despite_interrupt"


def _intervention_id(review_job_id: str) -> str:
    return f"spt-{hashlib.sha256(review_job_id.encode()).hexdigest()[:12]}"


def _review_advisory(decision: ReviewerDecision, intervention_id: str) -> str:
    action = decision.decision.upper()
    concern = " ".join((decision.hypothesis or decision.reason).split())[:600]
    reason = " ".join(decision.reason.split())[:600]
    guidance = (
        f"Check this assumption with evidence before continuing: {concern}"
        if decision.decision == "verify"
        else f"Re-evaluate the current approach before continuing: {concern}"
    )
    return (
        f"[Spotter / {action} / {intervention_id}]\n"
        "Advisory for the current turn.\n"
        "This is not a new user requirement and does not replace the user's active task.\n\n"
        f"{action}: {guidance}\n"
        f"Reason: {reason}\n\n"
        "After checking this, continue the original user task unless the evidence itself "
        "requires a change."
    )


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


def _rollout_pending(error: AppServerRpcError) -> bool:
    return "no rollout found" in error.message.casefold()


def _available_capabilities(capabilities: AppServerCapabilities) -> tuple[str, ...]:
    return tuple(
        name
        for name in ("observation", "thread_query", "steer", "interrupt")
        if getattr(capabilities, name) == CapabilityStatus.AVAILABLE
    )


def _capability_changes(
    previous: AppServerCapabilities | None,
    current: AppServerCapabilities,
) -> tuple[tuple[str, str, str], ...]:
    if previous is None:
        return ()
    changes: list[tuple[str, str, str]] = []
    for name in ("observation", "thread_query", "steer", "interrupt", "atomic_pre_tool_veto"):
        before = getattr(previous, name)
        after = getattr(current, name)
        # UNKNOWN means the new connection has not probed this surface yet;
        # it is not evidence that a previously available capability vanished.
        if after != CapabilityStatus.UNKNOWN and before != after:
            changes.append((name, before.value, after.value))
    return tuple(changes)


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
