"""Long-lived Spotter runtime and its versioned local control boundary."""

import argparse
import asyncio
import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Protocol, cast

from spotter.config import GatesConfig
from spotter.gates import Gate, GateDecision
from spotter.paths import secure_dir, spotter_home
from spotter.trace import TraceEvent

PROTOCOL_VERSION = 1
CONTROL_TIMEOUT = 1.0
GATE_TIMEOUT = 0.2
START_TIMEOUT = 5.0
STOP_TIMEOUT = 5.0
_MAX_REQUEST_BYTES = 64 * 1024
_SAFE_UNIX_PATH_BYTES = 100


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
    detail: str | None = None

    @property
    def available(self) -> bool:
        return self.health != RuntimeHealth.UNAVAILABLE


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


def runtime_socket() -> Path:
    home = spotter_home()
    candidate = home / "runtime" / "spotterd.sock"
    if len(os.fsencode(candidate)) <= _SAFE_UNIX_PATH_BYTES:
        return candidate
    digest = hashlib.sha256(os.fsencode(home)).hexdigest()[:12]
    return Path("/tmp") / f"spotter-{os.getuid()}-{digest}" / "spotterd.sock"


class DaemonClient:
    """One-request-per-connection client for the local control protocol."""

    def __init__(
        self, socket_path: Path | None = None, *, timeout: float = CONTROL_TIMEOUT
    ) -> None:
        self.socket_path = socket_path or runtime_socket()
        self.timeout = timeout

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(
                    self.socket_path, limit=_MAX_REQUEST_BYTES
                )
                payload: dict[str, Any] = {"protocol": PROTOCOL_VERSION, "method": method}
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
            )
        except DaemonUnavailable as error:
            return DaemonStatus(RuntimeHealth.UNAVAILABLE, detail=str(error))
        except (DaemonProtocolError, KeyError, ValueError) as error:
            return DaemonStatus(RuntimeHealth.DEGRADED, detail=str(error))

    async def shutdown(self) -> None:
        await self.request("shutdown")

    async def gate(
        self, event: TraceEvent, gates: GatesConfig, root: str | None
    ) -> tuple[GateDecision, float]:
        response = await self.request(
            "gate",
            {
                "proposal": {
                    "command": event.payload.get("command"),
                    "files": event.payload.get("files") or [],
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
        return GateDecision(allowed, rule, reason), float(evaluation_ms)


class DaemonServer:
    """Own the local runtime socket without owning any agent App Server."""

    def __init__(self, socket_path: Path | None = None) -> None:
        self.socket_path = socket_path or runtime_socket()
        self.health = RuntimeHealth.HEALTHY
        self._server: asyncio.AbstractServer | None = None
        self._shutdown = asyncio.Event()
        self._socket_inode: int | None = None
        self._lock: TextIOWrapper | None = None

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

    async def serve(self) -> None:
        await self.start()
        try:
            await self._shutdown.wait()
        finally:
            await self.close()

    async def wait_for_shutdown(self) -> None:
        await self._shutdown.wait()

    async def close(self) -> None:
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

    def set_health(self, health: RuntimeHealth) -> None:
        if health == RuntimeHealth.UNAVAILABLE:
            raise ValueError("a running daemon cannot report itself unavailable")
        self.health = health

    def _release_lock(self) -> None:
        if self._lock is None:
            return
        flock(self._lock, LOCK_UN)
        self._lock.close()
        self._lock = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        shutdown = False
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
            }
            if method == "gate":
                response.update(_evaluate_gate(request.get("params")))
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
        if shutdown:
            self._shutdown.set()


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


class ServiceManager(Protocol):
    """Platform-neutral lifecycle boundary for manual or managed services."""

    async def start(self) -> DaemonStatus: ...

    async def stop(self) -> DaemonStatus: ...

    async def restart(self) -> DaemonStatus: ...

    async def status(self) -> DaemonStatus: ...

    async def uninstall(self) -> DaemonStatus: ...


class ManualServiceManager:
    """Portable process lifecycle used by explicit CLI escape hatches."""

    def __init__(self, socket_path: Path | None = None) -> None:
        self.socket_path = socket_path or runtime_socket()
        self.client = DaemonClient(self.socket_path)

    async def status(self) -> DaemonStatus:
        return await self.client.status()

    async def start(self) -> DaemonStatus:
        current = await self.status()
        if current.available:
            return current

        home = secure_dir(spotter_home())
        logs = secure_dir(home / "logs")
        log_path = logs / "spotterd.log"
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "spotter.daemon"],
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            status = await self.status()
            if status.available:
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
    ) -> None:
        self.socket_path = socket_path or runtime_socket()
        self.client = DaemonClient(self.socket_path)
        self.platform = platform or sys.platform
        self.executable = executable or shutil.which("spotterd")
        if registration_path is not None:
            self.registration_path = registration_path
        elif self.platform == "darwin":
            self.registration_path = Path.home() / "Library/LaunchAgents" / f"{self.LABEL}.plist"
        elif self.platform.startswith("linux"):
            self.registration_path = Path.home() / ".config/systemd/user" / "spotterd.service"
        else:
            raise DaemonError(f"managed services are unsupported on {self.platform}")

    def _program(self) -> list[str]:
        if self.executable:
            return [self.executable]
        return [sys.executable, "-m", "spotter.daemon"]

    def _definition(self) -> bytes:
        home = secure_dir(spotter_home())
        logs = secure_dir(home / "logs")
        if self.platform == "darwin":
            return plistlib.dumps(
                {
                    "Label": self.LABEL,
                    "ProgramArguments": self._program(),
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "EnvironmentVariables": {"SPOTTER_HOME": str(home)},
                    "StandardOutPath": str(logs / "spotterd.log"),
                    "StandardErrorPath": str(logs / "spotterd.log"),
                }
            )
        command = " ".join(json.dumps(part) for part in self._program())
        return (
            "[Unit]\nDescription=Spotter supervision runtime\n\n"
            "[Service]\nType=simple\n"
            f"ExecStart={command}\n"
            f"Environment={json.dumps(f'SPOTTER_HOME={home}')}\n"
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
        return subprocess.run(command, capture_output=True, text=True, check=False)

    def _service_error(self, result: subprocess.CompletedProcess[str]) -> DaemonStatus:
        detail = (result.stderr or result.stdout or "service command failed").strip()[:300]
        return DaemonStatus(RuntimeHealth.UNAVAILABLE, detail=detail)

    async def _wait_ready(self) -> DaemonStatus:
        deadline = asyncio.get_running_loop().time() + START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            status = await self.status()
            if status.available:
                return status
            await asyncio.sleep(0.05)
        return DaemonStatus(
            RuntimeHealth.UNAVAILABLE, detail="managed spotterd did not become ready"
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
            if loaded and changed:
                self._run(["launchctl", "bootout", f"{domain}/{self.LABEL}"])
                loaded = False
            if not loaded:
                result = self._run(["launchctl", "bootstrap", domain, str(self.registration_path)])
            else:
                if current.available:
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
                if current.available and not changed:
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
    parser.parse_args(argv)
    try:
        asyncio.run(DaemonServer().serve())
    except DaemonAlreadyRunning as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
