"""Long-lived Spotter runtime and its versioned local control boundary."""

import argparse
import asyncio
import json
import math
import os
import plistlib
import resource
import stat
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from io import TextIOWrapper
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from spotter.build_identity import RuntimeComponent, current_build_identity, version_line
from spotter.config import GatesConfig, ReviewerConfig, SpotterConfig
from spotter.gates import Gate, GateDecision
from spotter.identity import ThreadId
from spotter.paths import RuntimeLayout, RuntimeLayoutError, secure_dir
from spotter.protocol import CONTROL_PROTOCOL_VERSION
from spotter.review_executor import ReviewExecutor
from spotter.snapshot import StepRecord
from spotter.thread_state import ThreadState, ThreadStateStore
from spotter.trace import TraceEvent

if TYPE_CHECKING:
    from spotter.runtime_connection import RecoveryState

PROTOCOL_VERSION = CONTROL_PROTOCOL_VERSION
CONTROL_TIMEOUT = 1.0
GATE_TIMEOUT = 0.2
START_TIMEOUT = 5.0
STOP_TIMEOUT = 5.0
SERVICE_COMMAND_TIMEOUT = 10.0
_MAX_REQUEST_BYTES = 64 * 1024
_RESOURCE_SAMPLE_EVERY = 64
_PACKAGE_WATCH_INTERVAL = 0.25
_PACKAGE_MISSING_GRACE = 10.0


class RuntimeHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class DaemonStatus:
    health: RuntimeHealth
    pid: int | None = None
    protocol: int | None = None
    version: str | None = None
    build_id: str | None = None
    detail: str | None = None

    @property
    def available(self) -> bool:
        return self.health != RuntimeHealth.UNAVAILABLE


@dataclass
class _PackageBoundaryMonitor:
    """Require a stable executable to stay absent before treating it as uninstall."""

    missing_grace: float
    missing_since: float | None = None

    def observe(self, *, available: bool, now: float) -> bool:
        if available:
            self.missing_since = None
            return False
        if self.missing_since is None:
            self.missing_since = now
            return self.missing_grace <= 0
        return now - self.missing_since >= self.missing_grace


class DaemonError(RuntimeError):
    """Base error for local runtime control failures."""


class DaemonProtocolError(DaemonError):
    """The daemon returned an invalid or incompatible response."""


class DaemonUnavailable(DaemonError):
    """The daemon control socket could not serve a request."""


class DaemonTimeout(DaemonUnavailable):
    """The daemon did not complete a request within its total deadline."""


class DaemonAlreadyRunning(DaemonError):
    """Another daemon already owns the runtime socket."""


class RuntimeRecovery(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def record_gate_decision(
        self, params: dict[str, object], decision: dict[str, object]
    ) -> None: ...


def runtime_socket(layout: RuntimeLayout | None = None) -> Path:
    return (layout or RuntimeLayout.discover()).control_socket


class DaemonClient:
    """One-request-per-connection client for the local control protocol."""

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        timeout: float = CONTROL_TIMEOUT,
        component: RuntimeComponent = "cli",
    ) -> None:
        self.socket_path = socket_path or runtime_socket()
        self.timeout = timeout
        self.component = component

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(
                    self.socket_path, limit=_MAX_REQUEST_BYTES
                )
                payload: dict[str, Any] = {
                    "protocol": PROTOCOL_VERSION,
                    "method": method,
                    "peer": current_build_identity().peer_metadata(self.component),
                }
                if params is not None:
                    payload["params"] = params
                request = json.dumps(payload, separators=(",", ":"))
                writer.write(request.encode() + b"\n")
                await writer.drain()
                raw = await reader.readline()
                if not raw:
                    raise DaemonProtocolError("daemon closed the control connection")
                response = json.loads(raw)
                if not isinstance(response, dict):
                    raise DaemonProtocolError("daemon response must be an object")
                response = cast(dict[str, Any], response)
                if response.get("protocol") != PROTOCOL_VERSION:
                    raise DaemonProtocolError("incompatible daemon protocol")
                if response.get("ok") is not True:
                    raise DaemonProtocolError(str(response.get("error") or "daemon request failed"))
                return response
        except TimeoutError as error:
            raise DaemonTimeout(f"daemon request timed out after {self.timeout:.3f}s") from error
        except OSError as error:
            raise DaemonUnavailable(str(error)) from error
        except (UnicodeDecodeError, ValueError) as error:
            raise DaemonProtocolError("daemon returned invalid JSON") from error
        finally:
            if writer is not None:
                writer.close()
                with suppress(ConnectionError, OSError):
                    await writer.wait_closed()

    async def status(self) -> DaemonStatus:
        try:
            response = await self.request("ping")
            health = RuntimeHealth(response["health"])
            pid = response.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool):
                raise DaemonProtocolError("daemon returned an invalid pid")
            return DaemonStatus(
                health=health,
                pid=pid,
                protocol=PROTOCOL_VERSION,
                version=(
                    response.get("spotter_version")
                    if isinstance(response.get("spotter_version"), str)
                    else None
                ),
                build_id=(
                    response.get("build_id") if isinstance(response.get("build_id"), str) else None
                ),
                detail=(
                    response.get("detail") if isinstance(response.get("detail"), str) else None
                ),
            )
        except DaemonUnavailable as error:
            return DaemonStatus(RuntimeHealth.UNAVAILABLE, detail=str(error))
        except (DaemonProtocolError, KeyError, ValueError) as error:
            return DaemonStatus(RuntimeHealth.DEGRADED, detail=str(error))

    async def shutdown(self) -> None:
        await self.request("shutdown")

    async def gate(
        self, event: TraceEvent, gates: GatesConfig, root: str | None
    ) -> tuple[GateDecision, float, dict[str, int | float | str] | None]:
        provenance = event.identity.provenance if event.identity is not None else None
        response = await self.request(
            "gate",
            {
                "proposal": {
                    "command": event.payload.get("command"),
                    "files": event.payload.get("files") or [],
                    "tool": event.payload.get("tool"),
                    "tool_use_id": event.payload.get("tool_use_id"),
                    "resource": event.payload.get("resource"),
                },
                "identity": {
                    "thread_id": (
                        provenance.agent_thread_id or provenance.legacy_session_id
                        if provenance is not None
                        else None
                    ),
                    "turn_id": (
                        provenance.agent_turn_id or event.payload.get("turn_id")
                        if provenance is not None
                        else event.payload.get("turn_id")
                    ),
                },
                "gates": {
                    "forbidden_paths": list(gates.forbidden_paths),
                    "block_dependency_changes": gates.block_dependency_changes,
                },
                "root": root,
            },
        )
        raw = response.get("decision")
        evaluation_ms = response.get("evaluation_ms")
        if not isinstance(raw, dict) or not isinstance(evaluation_ms, int | float):
            raise DaemonProtocolError("daemon returned an invalid gate result")
        allowed, rule, reason = raw.get("allowed"), raw.get("rule"), raw.get("reason")
        if not isinstance(allowed, bool):
            raise DaemonProtocolError("daemon returned an invalid gate decision")
        if rule is not None and not isinstance(rule, str):
            raise DaemonProtocolError("daemon returned an invalid gate rule")
        if reason is not None and not isinstance(reason, str):
            raise DaemonProtocolError("daemon returned an invalid gate reason")
        if isinstance(evaluation_ms, bool) or evaluation_ms < 0:
            raise DaemonProtocolError("daemon returned invalid gate timing")
        sample = _resource_sample_from(response.get("runtime_sample"))
        return GateDecision(allowed, rule, reason), float(evaluation_ms), sample


class DaemonServer:
    """Own the local runtime socket without owning any agent App Server."""

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        app_server_endpoint: str | None = None,
        journals_dir: Path | None = None,
        layout: RuntimeLayout | None = None,
        reviewer_config: ReviewerConfig | None = None,
        package_watch_interval: float = _PACKAGE_WATCH_INTERVAL,
        package_missing_grace: float = _PACKAGE_MISSING_GRACE,
    ) -> None:
        self.layout = layout or RuntimeLayout.discover()
        self.socket_path = socket_path or runtime_socket(self.layout)
        self.health = RuntimeHealth.HEALTHY
        self.health_detail: str | None = None
        self._server: asyncio.AbstractServer | None = None
        self._shutdown = asyncio.Event()
        self._socket_inode: int | None = None
        self._lock: TextIOWrapper | None = None
        self.thread_states = ThreadStateStore()
        self.app_server_endpoint = app_server_endpoint
        self.journals_dir = journals_dir or self.layout.user_data_dir / "sessions"
        self.recovery: RuntimeRecovery | None = None
        self.review_executor: ReviewExecutor | None = None
        self.reviewer_config = reviewer_config or ReviewerConfig()
        self._runtime_id = uuid.uuid4().hex
        self._gate_requests = 0
        self._resource_sample_seq = 0
        self._package_watch_interval = package_watch_interval
        self._package_monitor = _PackageBoundaryMonitor(package_missing_grace)
        self._package_watch_task: asyncio.Task[None] | None = None

    def observe_trace(self, event: TraceEvent) -> ThreadState:
        """Increment daemon-owned hot state after adapter normalization and journaling."""

        return self.thread_states.observe(event)

    def thread_state(self, thread_id: ThreadId) -> ThreadState:
        """Immutable reviewer/signal snapshot for one logical thread."""

        return self.thread_states.snapshot(thread_id)

    def hydrate_thread_state(self, records: list[StepRecord]) -> tuple[ThreadState, ...]:
        """Restore history without claiming the old active turn is control-ready."""

        return self.thread_states.hydrate(records)

    async def start(self) -> None:
        _secure_runtime_dir(self.socket_path.parent)
        lock = self.socket_path.with_suffix(".lock").open("a+")
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except OSError as error:
            lock.close()
            raise DaemonAlreadyRunning(f"spotterd already owns {self.socket_path}") from error
        self._lock = lock
        try:
            if self.socket_path.exists():
                status = await DaemonClient(self.socket_path).status()
                if status.available:
                    raise DaemonAlreadyRunning(f"spotterd already owns {self.socket_path}")
                self.socket_path.unlink(missing_ok=True)
            self._server = await asyncio.start_unix_server(
                self._handle,
                path=self.socket_path,
                limit=_MAX_REQUEST_BYTES,
            )
        except OSError as error:
            self._release_lock()
            raise DaemonAlreadyRunning(f"could not own {self.socket_path}: {error}") from error
        except Exception:
            self._release_lock()
            raise
        self.socket_path.chmod(0o600)
        self._socket_inode = self.socket_path.stat().st_ino
        if self.app_server_endpoint is not None:
            # Keep websockets and the App Server stack off the Hook import path.
            from spotter.runtime_connection import AppServerRecoveryLoop

            recovery = AppServerRecoveryLoop(
                self.app_server_endpoint,
                self.journals_dir,
                self.thread_states,
                on_state=self._on_recovery_state,
            )
            self.recovery = recovery
            self.review_executor = ReviewExecutor(
                self.reviewer_config,
                recovery.record_review_event,
                recovery.review_job_is_fresh,
            )
            recovery.set_review_job_callback(self.review_executor.submit)
            await recovery.start()
        if self.layout.daemon_executable is not None:
            self._package_watch_task = asyncio.create_task(self._watch_package_boundary())

    async def serve(self) -> None:
        await self.start()
        try:
            await self._shutdown.wait()
        finally:
            await self.close()

    async def wait_for_shutdown(self) -> None:
        await self._shutdown.wait()

    async def close(self) -> None:
        if self._package_watch_task is not None:
            self._package_watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._package_watch_task
            self._package_watch_task = None
        if self.review_executor is not None:
            await self.review_executor.close()
            self.review_executor = None
        if self.recovery is not None:
            await self.recovery.close()
            self.recovery = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with suppress(OSError):
            if (
                self._socket_inode is not None
                and self.socket_path.stat().st_ino == self._socket_inode
            ):
                self.socket_path.unlink()
        self._socket_inode = None
        self._release_lock()

    async def _watch_package_boundary(self) -> None:
        """Stop cleanly after uninstall without mistaking an upgrade relink for removal."""

        executable = self.layout.daemon_executable
        assert executable is not None
        loop = asyncio.get_running_loop()
        while True:
            if self._package_monitor.observe(
                available=os.access(executable, os.X_OK), now=loop.time()
            ):
                self._shutdown.set()
                return
            await asyncio.sleep(self._package_watch_interval)

    def set_health(self, health: RuntimeHealth, detail: str | None = None) -> None:
        if health == RuntimeHealth.UNAVAILABLE:
            raise ValueError("a running daemon cannot report itself unavailable")
        self.health = health
        self.health_detail = detail

    def _on_recovery_state(self, state: "RecoveryState", detail: str | None) -> None:
        value = state.value
        if value == "ready":
            self.set_health(RuntimeHealth.HEALTHY)
        elif value in {"connecting", "reconciling"}:
            self.set_health(RuntimeHealth.RECOVERING, detail)
        elif value in {"degraded", "backing_off"}:
            self.set_health(RuntimeHealth.DEGRADED, detail)

    def _release_lock(self) -> None:
        if self._lock is None:
            return
        flock(self._lock, LOCK_UN)
        self._lock.close()
        self._lock = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        shutdown = False
        gate_observation: tuple[dict[str, object], dict[str, object]] | None = None
        try:
            raw = await asyncio.wait_for(reader.readline(), CONTROL_TIMEOUT)
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise DaemonProtocolError("request must be an object")
            if request.get("protocol") != PROTOCOL_VERSION:
                raise DaemonProtocolError("incompatible control protocol")
            method = request.get("method")
            if method not in {"ping", "shutdown", "gate"}:
                raise DaemonProtocolError(f"unknown control method: {method}")
            shutdown = method == "shutdown"
            response: dict[str, Any] = {
                "protocol": PROTOCOL_VERSION,
                "ok": True,
                "health": self.health.value,
                "pid": os.getpid(),
                **current_build_identity().peer_metadata("daemon"),
            }
            if self.health_detail is not None:
                response["detail"] = self.health_detail
            if method == "gate":
                params = request.get("params")
                evaluation = _evaluate_gate(params)
                response.update(evaluation)
                decision = evaluation.get("decision")
                if (
                    self.recovery is not None
                    and isinstance(params, dict)
                    and isinstance(decision, dict)
                    and decision.get("allowed") is False
                ):
                    gate_observation = (params, decision)
                if sample := self._maybe_sample_resources():
                    response["runtime_sample"] = sample
        except (
            DaemonProtocolError,
            json.JSONDecodeError,
            OSError,
            TimeoutError,
            ValueError,
        ) as error:
            response = {
                "protocol": PROTOCOL_VERSION,
                "ok": False,
                "error": str(error),
            }
        try:
            writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()
        if gate_observation is not None and self.recovery is not None:
            try:
                self.recovery.record_gate_decision(*gate_observation)
            except Exception as error:  # noqa: BLE001 — optional observation cannot break gating
                self.set_health(RuntimeHealth.DEGRADED, f"gate observation failed: {error}")
        if shutdown:
            self._shutdown.set()

    def _maybe_sample_resources(self) -> dict[str, int | float | str] | None:
        """Bounded resource sample, kept off all but one in 64 gate requests."""

        self._gate_requests += 1
        if self._gate_requests != 1 and self._gate_requests % _RESOURCE_SAMPLE_EVERY:
            return None
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
        except OSError:
            return None  # optional telemetry must never break a gate response
        self._resource_sample_seq += 1
        rss = int(usage.ru_maxrss)
        if sys.platform != "darwin":
            rss *= 1024  # Linux reports KiB; macOS reports bytes.
        return {
            "runtime_id": self._runtime_id,
            "sample_seq": self._resource_sample_seq,
            "cpu_seconds": usage.ru_utime + usage.ru_stime,
            "peak_rss_bytes": rss,
        }


def _evaluate_gate(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DaemonProtocolError("gate params must be an object")
    proposal, gates, root = raw.get("proposal"), raw.get("gates"), raw.get("root")
    if not isinstance(proposal, dict) or not isinstance(gates, dict):
        raise DaemonProtocolError("gate proposal and config must be objects")
    forbidden_paths = gates.get("forbidden_paths")
    block_dependencies = gates.get("block_dependency_changes")
    if (
        not isinstance(forbidden_paths, list)
        or not all(isinstance(path, str) for path in forbidden_paths)
        or not isinstance(block_dependencies, bool)
        or (root is not None and not isinstance(root, str))
    ):
        raise DaemonProtocolError("invalid gate config")

    command, files = proposal.get("command"), proposal.get("files")
    if (
        (command is not None and not isinstance(command, str))
        or not isinstance(files, list)
        or not all(isinstance(path, str) for path in files)
    ):
        decision = GateDecision(
            True, "unsupported_proposal", "fail-open: unsupported proposal shape"
        )
        evaluation_ms = 0.0
    else:
        started = time.perf_counter_ns()
        decision = Gate(tuple(forbidden_paths), block_dependencies, root).check(
            TraceEvent("tool_proposal", dict(proposal))
        )
        evaluation_ms = (time.perf_counter_ns() - started) / 1_000_000
    return {
        "decision": {
            "allowed": decision.allowed,
            "rule": decision.rule,
            "reason": decision.reason,
        },
        "evaluation_ms": evaluation_ms,
    }


def _resource_sample_from(raw: object) -> dict[str, int | float | str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    runtime_id = raw.get("runtime_id")
    sample_seq = raw.get("sample_seq")
    cpu_seconds = raw.get("cpu_seconds")
    peak_rss_bytes = raw.get("peak_rss_bytes")
    if (
        not isinstance(runtime_id, str)
        or not runtime_id
        or not isinstance(sample_seq, int)
        or isinstance(sample_seq, bool)
        or sample_seq <= 0
        or not isinstance(cpu_seconds, int | float)
        or isinstance(cpu_seconds, bool)
        or cpu_seconds < 0
        or not math.isfinite(cpu_seconds)
        or not isinstance(peak_rss_bytes, int)
        or isinstance(peak_rss_bytes, bool)
        or peak_rss_bytes < 0
    ):
        return None
    return {
        "runtime_id": runtime_id,
        "sample_seq": sample_seq,
        "cpu_seconds": float(cpu_seconds),
        "peak_rss_bytes": peak_rss_bytes,
    }


class ServiceManager(Protocol):
    """Platform-neutral lifecycle boundary for manual or managed services."""

    async def start(self) -> DaemonStatus: ...

    async def stop(self) -> DaemonStatus: ...

    async def restart(self) -> DaemonStatus: ...

    async def status(self) -> DaemonStatus: ...

    async def uninstall(self) -> DaemonStatus: ...


class ManualServiceManager:
    """Portable process lifecycle used by explicit CLI escape hatches."""

    def __init__(
        self, socket_path: Path | None = None, *, layout: RuntimeLayout | None = None
    ) -> None:
        self.layout = layout or RuntimeLayout.discover()
        self.socket_path = socket_path or runtime_socket(self.layout)
        self.client = DaemonClient(self.socket_path)

    async def status(self) -> DaemonStatus:
        return await self.client.status()

    async def start(self) -> DaemonStatus:
        current = await self.status()
        installed_build = current_build_identity().build_id
        if current.available and current.build_id == installed_build:
            return current
        if current.available:
            stopped = await self.stop()
            if stopped.available:
                return stopped

        home = secure_dir(self.layout.user_data_dir)
        logs = secure_dir(self.layout.log_dir)
        log_path = logs / "spotterd.log"
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                list(self.layout.daemon_command),
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            status = await self.status()
            if status.available and status.build_id == installed_build:
                return status
            if process.poll() is not None:
                return DaemonStatus(
                    RuntimeHealth.UNAVAILABLE,
                    detail=f"spotterd exited during startup; see {log_path}",
                )
            await asyncio.sleep(0.05)
        process.terminate()
        return DaemonStatus(
            RuntimeHealth.UNAVAILABLE,
            detail=f"spotterd did not become ready; see {log_path}",
        )

    async def stop(self) -> DaemonStatus:
        current = await self.status()
        if not current.available:
            return DaemonStatus(RuntimeHealth.UNAVAILABLE, detail="already stopped")
        try:
            await self.client.shutdown()
        except DaemonError as error:
            return DaemonStatus(RuntimeHealth.DEGRADED, detail=str(error))
        deadline = asyncio.get_running_loop().time() + STOP_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if not (await self.status()).available:
                return DaemonStatus(RuntimeHealth.UNAVAILABLE, detail="stopped")
            await asyncio.sleep(0.05)
        return DaemonStatus(RuntimeHealth.DEGRADED, detail="spotterd did not stop")

    async def restart(self) -> DaemonStatus:
        stopped = await self.stop()
        if stopped.available:
            return stopped
        return await self.start()

    async def uninstall(self) -> DaemonStatus:
        return await self.stop()


class ManagedServiceManager:
    """Install and control a login-scoped launchd or systemd user service."""

    LABEL = "dev.spotter.runtime"

    def __init__(
        self,
        socket_path: Path | None = None,
        *,
        platform: str | None = None,
        registration_path: Path | None = None,
        executable: str | None = None,
        layout: RuntimeLayout | None = None,
    ) -> None:
        self.layout = layout or RuntimeLayout.discover(daemon_executable=executable)
        self.socket_path = socket_path or runtime_socket(self.layout)
        self.client = DaemonClient(self.socket_path)
        self.platform = platform or sys.platform
        self.executable = (
            str(self.layout.daemon_executable)
            if self.layout.daemon_executable is not None
            else None
        )
        if registration_path is not None:
            self.registration_path = registration_path
        else:
            try:
                self.registration_path = self.layout.service_registration(self.platform, self.LABEL)
            except RuntimeLayoutError as error:
                raise DaemonError(str(error)) from error

    def _program(self) -> list[str]:
        try:
            return list(self.layout.persistent_daemon_command())
        except RuntimeLayoutError as error:
            raise DaemonError(str(error)) from error

    def _definition(self) -> bytes:
        home = secure_dir(self.layout.user_data_dir)
        logs = secure_dir(self.layout.log_dir)
        log_path = logs / "spotterd.log"
        if self.platform == "darwin":
            program = self._program()
            return plistlib.dumps(
                {
                    "Label": self.LABEL,
                    "ProgramArguments": program,
                    "RunAtLoad": True,
                    # A package-manager uninstall removes the stable entry
                    # point without calling `spotter teardown`.  Keep the
                    # integration registration repairable, but do not make
                    # launchd retry a removed executable forever.
                    "KeepAlive": {"PathState": {program[0]: True}},
                    "WorkingDirectory": str(home),
                    "EnvironmentVariables": {"SPOTTER_HOME": str(home)},
                    "StandardOutPath": str(log_path),
                    "StandardErrorPath": str(log_path),
                }
            )
        # ponytail: this handles systemd specifier expansion for ordinary paths; use a
        # dedicated systemd escaping helper if arbitrary control characters are supported.
        command = " ".join(json.dumps(part.replace("%", "%%")) for part in self._program())
        environment = json.dumps(f"SPOTTER_HOME={home}".replace("%", "%%"))
        working_directory = json.dumps(str(home).replace("%", "%%"))
        output = json.dumps(f"append:{log_path}".replace("%", "%%"))
        return (
            "[Unit]\nDescription=Spotter supervision runtime\n\n"
            "[Service]\nType=simple\n"
            f"ExecStart={command}\n"
            f"Environment={environment}\n"
            f"WorkingDirectory={working_directory}\n"
            f"StandardOutput={output}\n"
            f"StandardError={output}\n"
            "Restart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n"
        ).encode()

    def _install_definition(self) -> bool:
        content = self._definition()
        if self.registration_path.exists() and self.registration_path.read_bytes() == content:
            return False
        self.registration_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.registration_path.with_suffix(self.registration_path.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.chmod(0o600)
        os.replace(temporary, self.registration_path)
        return True

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=SERVICE_COMMAND_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                command,
                124,
                "",
                f"service command timed out after {SERVICE_COMMAND_TIMEOUT:.0f}s",
            )

    def _service_error(self, result: subprocess.CompletedProcess[str]) -> DaemonStatus:
        detail = (result.stderr or result.stdout or "service command failed").strip()[:300]
        return DaemonStatus(RuntimeHealth.UNAVAILABLE, detail=detail)

    async def _wait_ready(self) -> DaemonStatus:
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        installed_build = current_build_identity().build_id
        while asyncio.get_running_loop().time() < deadline:
            status = await self.status()
            if status.available and status.build_id == installed_build:
                return status
            await asyncio.sleep(0.05)
        return DaemonStatus(
            RuntimeHealth.UNAVAILABLE,
            detail=f"managed spotterd did not become ready with build {installed_build}",
        )

    async def status(self) -> DaemonStatus:
        return await self.client.status()

    async def start(self) -> DaemonStatus:
        current = await self.status()
        if self.platform == "darwin":
            domain = f"gui/{os.getuid()}"
            loaded = self._run(["launchctl", "print", f"{domain}/{self.LABEL}"]).returncode == 0
            if current.available and not loaded:
                return DaemonStatus(
                    RuntimeHealth.DEGRADED,
                    pid=current.pid,
                    detail="spotterd is running outside the managed launchd service; stop it first",
                )
            changed = self._install_definition()
            if loaded and (changed or not current.available):
                removed = self._run(["launchctl", "bootout", f"{domain}/{self.LABEL}"])
                if removed.returncode != 0:
                    return self._service_error(removed)
                loaded = False
            if not loaded:
                result = self._run(["launchctl", "bootstrap", domain, str(self.registration_path)])
            else:
                build_changed = current.build_id != current_build_identity().build_id
                if current.available and not build_changed:
                    return current
                result = self._run(["launchctl", "kickstart", "-k", f"{domain}/{self.LABEL}"])
        else:
            active = (
                self._run(["systemctl", "--user", "is-active", "spotterd.service"]).returncode == 0
            )
            if current.available and not active:
                return DaemonStatus(
                    RuntimeHealth.DEGRADED,
                    pid=current.pid,
                    detail="spotterd is running outside the managed systemd service; stop it first",
                )
            changed = self._install_definition()
            if changed:
                result = self._run(["systemctl", "--user", "daemon-reload"])
                if result.returncode != 0:
                    return self._service_error(result)
            if active:
                build_changed = current.build_id != current_build_identity().build_id
                if current.available and not changed and not build_changed:
                    return current
                result = self._run(["systemctl", "--user", "restart", "spotterd.service"])
            else:
                result = self._run(["systemctl", "--user", "enable", "--now", "spotterd.service"])
        if result.returncode != 0:
            return self._service_error(result)
        return await self._wait_ready()

    async def stop(self) -> DaemonStatus:
        if self.platform == "darwin":
            result = self._run(["launchctl", "bootout", f"gui/{os.getuid()}/{self.LABEL}"])
        else:
            result = self._run(["systemctl", "--user", "stop", "spotterd.service"])
        if result.returncode != 0 and (await self.status()).available:
            return self._service_error(result)
        return DaemonStatus(RuntimeHealth.UNAVAILABLE, detail="stopped")

    async def restart(self) -> DaemonStatus:
        await self.stop()
        return await self.start()

    async def uninstall(self) -> DaemonStatus:
        if self.platform.startswith("linux"):
            stopped = self._run(["systemctl", "--user", "disable", "--now", "spotterd.service"])
            if stopped.returncode != 0 and (await self.status()).available:
                return self._service_error(stopped)
        else:
            status = await self.stop()
            if status.health != RuntimeHealth.UNAVAILABLE:
                return status
        self.registration_path.unlink(missing_ok=True)
        if self.platform.startswith("linux"):
            self._run(["systemctl", "--user", "daemon-reload"])
        return DaemonStatus(RuntimeHealth.UNAVAILABLE, detail="service removed")


def _secure_runtime_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise PermissionError(f"unsafe runtime directory: {path}")
    path.chmod(0o700)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Spotter supervision daemon")
    parser.add_argument("--version", action="version", version=version_line("spotterd"))
    parser.parse_args(argv)
    try:
        layout = RuntimeLayout.discover()
        reviewer_config = _configured_reviewer(layout)
        asyncio.run(
            DaemonServer(
                app_server_endpoint=_configured_app_server_endpoint(layout),
                reviewer_config=reviewer_config,
                layout=layout,
            ).serve()
        )
    except DaemonAlreadyRunning as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


def _configured_app_server_endpoint(layout: RuntimeLayout | None = None) -> str | None:
    path = (layout or RuntimeLayout.discover()).integration_manifest
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    endpoint = raw.get("app_server_endpoint") if isinstance(raw, dict) else None
    return endpoint if isinstance(endpoint, str) and endpoint.strip() else None


def _configured_reviewer(layout: RuntimeLayout | None = None) -> ReviewerConfig:
    runtime_layout = layout or RuntimeLayout.discover()
    manifest_path = runtime_layout.integration_manifest
    try:
        raw = json.loads(manifest_path.read_bytes())
    except (OSError, ValueError):
        raw = {}
    configured = raw.get("config_path") if isinstance(raw, dict) else None
    config_path = Path(configured) if isinstance(configured, str) and configured else None
    default_path = runtime_layout.user_config_dir / "spotter.toml"
    config_path = config_path or (default_path if default_path.exists() else None)
    if config_path is None:
        return ReviewerConfig()
    try:
        return SpotterConfig.from_toml(config_path).reviewer
    except (OSError, ValueError) as error:
        print(
            f"spotterd: unusable reviewer config {config_path} ({error}); signal reviews disabled",
            file=sys.stderr,
        )
        return ReviewerConfig()


if __name__ == "__main__":
    raise SystemExit(main())
