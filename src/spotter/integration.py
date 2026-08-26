"""Transactional Codex integration ownership and migration."""

import asyncio
import copy
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
import tomllib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any, cast

from spotter.app_server_endpoint import (
    AppServerEndpointError,
    display_app_server_endpoint,
    normalize_app_server_endpoint,
    redact_app_server_error,
)
from spotter.build_identity import current_build_identity
from spotter.codex_host import CodexHostVersionError, validate_codex_host_version
from spotter.config import ConfigurationError, resolve_config
from spotter.daemon import (
    DaemonStatus,
    ManagedServiceManager,
    ManualServiceManager,
    RuntimeHealth,
    ServiceManager,
)
from spotter.doctor import OK, check_roundtrip
from spotter.paths import RuntimeLayout, RuntimeLayoutError, secure_dir

MANIFEST_SCHEMA_NAME = "spotter.integration_manifest"
MANIFEST_SCHEMA_VERSION = 4
MANIFEST_SCHEMA = MANIFEST_SCHEMA_VERSION


class IntegrationError(RuntimeError):
    """The requested integration transaction could not complete safely."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _package_version() -> str:
    return current_build_identity().version


def _integration_generation(
    layout: RuntimeLayout, config_path: Path | None, runtime_mode: str
) -> str:
    """Fence generated host state to one package build and stable layout."""
    payload = {
        "schema": MANIFEST_SCHEMA,
        "build_id": current_build_identity().build_id,
        "bridge_command": layout.bridge_command,
        "daemon_command": layout.daemon_command,
        "package_assets_dir": str(layout.package_assets_dir),
        "config_path": str(config_path) if config_path is not None else None,
        "runtime_mode": runtime_mode,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as sink:
        sink.write(content)
        sink.flush()
        os.fsync(sink.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def is_spotter_hook(hook: object) -> bool:
    """Recognize executable Spotter Hook commands without matching incidental prose."""
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return False
    command = cast(str, hook["command"])
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    for index, token in enumerate(tokens):
        name = Path(token).name
        if name == "spotter-hook":
            return True
        if name == "spotter" and index + 1 < len(tokens) and tokens[index + 1] == "hook":
            return True
    return False


def _is_known_legacy_hook(hook: object) -> bool:
    """Recognize the repository/plugin bridge shape that predates manifests."""

    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return False
    try:
        tokens = shlex.split(cast(str, hook["command"]))
    except ValueError:
        return False
    if len(tokens) != 1:
        return False
    normalized = tokens[0].replace("\\", "/")
    return normalized.endswith("/scripts/spotter-hook") and (
        "PLUGIN_ROOT" in normalized or "/spotter/" in normalized
    )


@dataclass(frozen=True)
class CodexInstall:
    path: str
    version: str
    supports_remote: bool
    supports_app_server: bool

    @classmethod
    def detect(cls, path: str | None = None) -> "CodexInstall":
        executable = path or shutil.which("codex")
        if executable is None:
            raise IntegrationError("Codex is not installed or is not on PATH")

        def output(arguments: list[str]) -> str:
            try:
                result = subprocess.run(
                    [executable, *arguments],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise IntegrationError(f"could not inspect Codex: {error}") from error
            if result.returncode != 0:
                raise IntegrationError((result.stderr or "Codex inspection failed").strip())
            return result.stdout

        detected_version = output(["--version"]).strip()
        try:
            validate_codex_host_version(detected_version)
        except CodexHostVersionError as error:
            raise IntegrationError(f"unsupported Codex host: {error}") from error
        root_help = output(["--help"])
        app_server_help = output(["app-server", "--help"])
        return cls(
            path=executable,
            version=detected_version,
            supports_remote="--remote" in root_help,
            supports_app_server="--listen" in app_server_help,
        )

    def remove_plugin(self, selector: str, codex_home: Path) -> None:
        result = subprocess.run(
            [self.path, "plugin", "remove", selector, "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise IntegrationError(
                f"could not remove legacy plugin {selector}: "
                f"{(result.stderr or result.stdout).strip()[:300]}"
            )

    def add_plugin(self, selector: str, codex_home: Path) -> None:
        try:
            result = subprocess.run(
                [self.path, "plugin", "add", selector, "--json"],
                capture_output=True,
                text=True,
                env={**os.environ, "CODEX_HOME": str(codex_home)},
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise IntegrationError(
                f"could not restore legacy plugin {selector}: {error}"
            ) from error
        if result.returncode != 0:
            raise IntegrationError(
                f"could not restore legacy plugin {selector}: "
                f"{(result.stderr or result.stdout).strip()[:300]}"
            )


@dataclass(frozen=True)
class IntegrationManifest:
    schema: int
    state: str
    agent: str
    setup_by: str
    agent_path: str
    agent_version: str
    codex_home: str
    app_server_strategy: str
    app_server_endpoint: str | None
    runtime_mode: str
    service_registration: str | None
    service_owned: bool
    hooks_file: str
    hooks_file_created: bool
    owned_hooks: list[dict[str, Any]]
    config_path: str | None = None
    legacy_hooks_removed: list[dict[str, Any]] = field(default_factory=list)
    legacy_plugins_removed: list[str] = field(default_factory=list)
    config_fingerprint_before: str | None = None
    config_fingerprint_after: str | None = None
    hooks_fingerprint_before: str | None = None
    hooks_fingerprint_after: str | None = None
    backup_paths: list[str] = field(default_factory=list)
    setup_build_id: str = "legacy"
    integration_generation: str = "legacy"
    runtime_layout: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    error: str | None = None
    schema_name: str = MANIFEST_SCHEMA_NAME
    schema_version: int = MANIFEST_SCHEMA_VERSION

    @classmethod
    def load(cls, path: Path) -> "IntegrationManifest | None":
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise IntegrationError(f"integration manifest is unreadable: {error}") from error
        if not isinstance(raw, dict):
            raise IntegrationError("integration manifest must be a JSON object")
        schema = raw.get("schema")
        schema_name = raw.get("schema_name")
        schema_version = raw.get("schema_version")
        if schema_name is None and schema_version is None:
            pass
        elif schema_name != MANIFEST_SCHEMA_NAME:
            raise IntegrationError(f"unsupported integration manifest schema {schema_name!r}")
        elif not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise IntegrationError("integration manifest schema_version must be an integer")
        elif schema_version != schema:
            raise IntegrationError("integration manifest has mismatched schema versions")
        if schema in {1, 2, 3}:
            if schema == 1:
                raw["owned_hooks"] = [raw.pop("owned_hook", None)]
            if schema in {1, 2}:
                owned = raw.get("owned_hooks")
                if isinstance(owned, list):
                    hook = next(
                        (
                            entry.get("hook")
                            for entry in owned
                            if isinstance(entry, dict) and isinstance(entry.get("hook"), dict)
                        ),
                        None,
                    )
                    if hook is not None:
                        for event, matcher in (
                            ("SessionStart", None),
                            ("UserPromptSubmit", None),
                            ("PostToolUse", ".*"),
                        ):
                            entry = {"event": event, "matcher": matcher, "hook": hook}
                            if entry not in owned:
                                owned.append(entry)
            raw["schema"] = MANIFEST_SCHEMA
            raw.setdefault("setup_build_id", "legacy")
            raw.setdefault("integration_generation", "legacy")
            raw.setdefault("runtime_layout", {})
        elif schema != MANIFEST_SCHEMA:
            raise IntegrationError(f"unsupported integration manifest schema {raw.get('schema')!r}")
        raw["schema_name"] = MANIFEST_SCHEMA_NAME
        raw["schema_version"] = MANIFEST_SCHEMA_VERSION
        try:
            return cls(**raw)
        except TypeError as error:
            raise IntegrationError(f"integration manifest is invalid: {error}") from error

    def save(self, path: Path) -> None:
        _atomic_write(path, (json.dumps(asdict(self), indent=2, sort_keys=True) + "\n").encode())


@dataclass(frozen=True)
class IntegrationPlan:
    hooks_file: Path
    hooks_changed: bool
    legacy_hooks: int
    legacy_plugins: tuple[str, ...]
    runtime_mode: str
    service_registration: str | None
    app_server_endpoint: str | None

    def lines(self) -> list[str]:
        app_server = (
            f"verify external endpoint {display_app_server_endpoint(self.app_server_endpoint)}"
            if self.app_server_endpoint is not None
            else "pending; pass --endpoint ws://127.0.0.1:4500 when a shared server is running"
        )
        return [
            "Codex hooks: "
            f"{'update' if self.hooks_changed else 'already current'} {self.hooks_file}",
            f"Legacy Spotter hooks to migrate: {self.legacy_hooks}",
            f"Legacy Spotter plugins to remove: {len(self.legacy_plugins)}",
            f"Runtime: {self.runtime_mode}",
            f"App Server: {app_server}",
        ]


def _verify_external_app_server(endpoint: str) -> None:
    """Initialize an external server and require the minimum observation surface."""

    async def verify() -> None:
        # Keep WebSockets off the generated Hook import path.
        from spotter.app_server import CapabilityStatus, CodexAppServerClient, ConnectionState

        client = CodexAppServerClient(endpoint)
        try:
            await client.connect()
            capabilities = client.capabilities
            if (
                client.state != ConnectionState.CONNECTED
                or client.host_version is None
                or capabilities.observation != CapabilityStatus.AVAILABLE
                or capabilities.thread_query != CapabilityStatus.AVAILABLE
            ):
                raise IntegrationError(
                    "App Server does not provide compatible observation and "
                    "thread-query capabilities"
                )
        finally:
            await client.disconnect()

    asyncio.run(verify())


class IntegrationManager:
    """Own exactly the Codex fragments recorded in the integration manifest."""

    def __init__(
        self,
        *,
        codex_home: Path | None = None,
        codex: CodexInstall | None = None,
        service: ServiceManager | None = None,
        portable: bool = False,
        spotter_executable: str | None = None,
        layout: RuntimeLayout | None = None,
        config_path: Path | None = None,
        verifier: Callable[[Path | None], bool] | None = None,
        app_server_endpoint: str | None = None,
        app_server_verifier: Callable[[str], None] | None = None,
        # A changed service definition needs the full unregister/register cycle,
        # and launchd's teardown measured over 8s of `Connection refused` before
        # the socket came back — so a 10s budget lost that race every time and
        # rolled setup back. This is setup, not a hot path; the budget only has
        # to be larger than the platform's restart, and stay bounded.
        app_server_ready_timeout: float = 30.0,
        app_server_poll_interval: float = 0.1,
    ) -> None:
        self.codex_home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.codex = codex
        self._service_explicit = service is not None
        self.layout = layout or RuntimeLayout.discover(cli_executable=spotter_executable)
        self.service = service or (
            ManualServiceManager(layout=self.layout)
            if portable
            else ManagedServiceManager(layout=self.layout)
        )
        self.portable = portable
        self.spotter_executable = (
            str(self.layout.cli_executable) if self.layout.cli_executable is not None else None
        )
        default_config = self.layout.user_config_dir / "spotter.toml"
        self.config_path = config_path or (default_config if default_config.exists() else None)
        self.integration_generation = _integration_generation(
            self.layout, self.config_path, "portable" if portable else "managed"
        )
        self.verifier = verifier or (
            lambda path: check_roundtrip(path, command=self.layout.bridge_command).status == OK
        )
        self.requested_app_server_endpoint = app_server_endpoint
        self.app_server_verifier = app_server_verifier or _verify_external_app_server
        self.app_server_ready_timeout = app_server_ready_timeout
        self.app_server_poll_interval = app_server_poll_interval
        integrations = self.layout.integration_dir
        self.manifest_path = integrations / "codex.json"
        self.lock_path = integrations / "codex.lock"
        self.hooks_path = self.codex_home / "hooks.json"
        self.codex_config_path = self.codex_home / "config.toml"

    def _codex_install(self) -> CodexInstall:
        if self.codex is None:
            self.codex = CodexInstall.detect()
        return self.codex

    def _hook_command(self) -> str:
        try:
            self.layout.validate_persistent()
        except RuntimeLayoutError as error:
            raise IntegrationError(str(error)) from error
        assert self.layout.cli_executable is not None
        executable = shlex.quote(str(self.layout.cli_executable))
        home = shlex.quote(str(self.layout.user_data_dir))
        command = (
            f"[ -x {executable} ] && SPOTTER_HOME={home} {shlex.join(self.layout.bridge_command)}"
        )
        if self.config_path is not None:
            command += f" --config {shlex.quote(str(self.config_path))}"
        command += f" --integration-generation {self.integration_generation}"
        return command + " || true"

    def _owned_hooks(self) -> list[dict[str, Any]]:
        hook = {"type": "command", "command": self._hook_command()}
        return [
            {"event": "PreToolUse", "matcher": ".*", "hook": hook},
            {"event": "SessionStart", "matcher": None, "hook": hook},
            {"event": "UserPromptSubmit", "matcher": None, "hook": hook},
            {"event": "PostToolUse", "matcher": ".*", "hook": hook},
        ]

    @staticmethod
    def _is_spotter_hook(hook: object) -> bool:
        return is_spotter_hook(hook)

    def _read_hooks(self) -> tuple[dict[str, Any], bytes | None]:
        if not self.hooks_path.exists():
            return {"hooks": {}}, None
        content = self.hooks_path.read_bytes()
        try:
            raw = json.loads(content)
        except ValueError as error:
            raise IntegrationError(f"Codex hooks file is invalid JSON: {error}") from error
        if not isinstance(raw, dict) or not isinstance(raw.get("hooks", {}), dict):
            raise IntegrationError("Codex hooks file has an unsupported shape")
        for event, groups in cast(dict[str, object], raw.get("hooks", {})).items():
            if not isinstance(event, str) or not isinstance(groups, list):
                raise IntegrationError("Codex hooks file has an unsupported event shape")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise IntegrationError("Codex hooks file has an unsupported hook group")
        return cast(dict[str, Any], raw), content

    def _migrate_hooks(
        self, raw: dict[str, Any], existing: IntegrationManifest | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        updated = copy.deepcopy(raw)
        events = cast(dict[str, list[dict[str, Any]]], updated.setdefault("hooks", {}))
        removed: list[dict[str, Any]] = []
        owned = self._owned_hooks()
        previously_owned = existing.owned_hooks if existing is not None else []
        for event, groups in list(events.items()):
            kept_groups: list[dict[str, Any]] = []
            for group in groups:
                kept_hooks = []
                for hook in cast(list[dict[str, Any]], group["hooks"]):
                    if self._is_spotter_hook(hook):
                        identity = (event, group.get("matcher"), hook)
                        is_current = existing is not None and any(
                            identity == (entry["event"], entry["matcher"], entry["hook"])
                            for entry in owned
                        )
                        is_recorded = any(
                            isinstance(entry, dict)
                            and identity
                            == (entry.get("event"), entry.get("matcher"), entry.get("hook"))
                            for entry in previously_owned
                        )
                        if not (is_current or is_recorded or _is_known_legacy_hook(hook)):
                            raise IntegrationError(
                                "found an unowned or user-modified Spotter Hook; "
                                "ownership is ambiguous, so setup made no changes"
                            )
                        if not is_current:
                            removed.append(
                                {"event": event, "matcher": group.get("matcher"), "hook": hook}
                            )
                    else:
                        kept_hooks.append(hook)
                if kept_hooks:
                    group["hooks"] = kept_hooks
                    kept_groups.append(group)
            if kept_groups:
                events[event] = kept_groups
            else:
                events.pop(event, None)

        for entry in owned:
            group = {"hooks": [entry["hook"]]}
            if entry["matcher"] is not None:
                group["matcher"] = entry["matcher"]
            events.setdefault(entry["event"], []).append(group)
        return updated, removed

    def _legacy_plugins(self) -> tuple[str, ...]:
        if not self.codex_config_path.exists():
            return ()
        try:
            raw = tomllib.loads(self.codex_config_path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise IntegrationError(f"Codex config is unreadable: {error}") from error
        plugins = raw.get("plugins", {})
        if not isinstance(plugins, dict):
            raise IntegrationError("Codex plugin config has an unsupported shape")
        return tuple(
            sorted(name for name in plugins if name == "spotter" or name.startswith("spotter@"))
        )

    def _service_registration(self) -> str | None:
        path = getattr(self.service, "registration_path", None)
        return str(path) if isinstance(path, Path) else None

    def inspect(
        self, existing: IntegrationManifest | None = None
    ) -> tuple[IntegrationPlan, dict[str, Any], bytes | None, list[dict[str, Any]]]:
        if existing is None and self.manifest_path.exists():
            existing = IntegrationManifest.load(self.manifest_path)
        try:
            self.layout.validate_persistent()
        except RuntimeLayoutError as error:
            raise IntegrationError(str(error)) from error
        codex = self._codex_install()
        try:
            validate_codex_host_version(codex.version)
        except CodexHostVersionError as error:
            raise IntegrationError(f"unsupported Codex host: {error}") from error
        if not codex.supports_remote or not codex.supports_app_server:
            raise IntegrationError("Codex lacks the required app-server/--remote capability")
        try:
            resolved = resolve_config(
                layout=self.layout,
                repository=Path.cwd(),
                explicit_path=self.config_path,
            )
        except (OSError, tomllib.TOMLDecodeError, ConfigurationError) as error:
            raise IntegrationError(f"Spotter config is unusable: {error}") from error
        if resolved.diagnostics:
            raise IntegrationError("Spotter config is unusable: " + "; ".join(resolved.diagnostics))
        hooks, before = self._read_hooks()
        migrated, removed = self._migrate_hooks(hooks, existing)
        after = (json.dumps(migrated, indent=2, sort_keys=True) + "\n").encode()
        endpoint = self.requested_app_server_endpoint
        if endpoint is None and existing is not None:
            endpoint = existing.app_server_endpoint
        if endpoint is not None:
            try:
                endpoint = normalize_app_server_endpoint(endpoint)
            except AppServerEndpointError as error:
                raise IntegrationError(str(error)) from error
        plan = IntegrationPlan(
            hooks_file=self.hooks_path,
            hooks_changed=before != after,
            legacy_hooks=len(removed),
            legacy_plugins=self._legacy_plugins(),
            runtime_mode="portable" if self.portable else "managed",
            service_registration=self._service_registration(),
            app_server_endpoint=endpoint,
        )
        return plan, migrated, before, removed

    def plan(self) -> IntegrationPlan:
        return self.inspect()[0]

    def _backup(self, name: str, content: bytes | None) -> Path | None:
        if content is None:
            return None
        backups = secure_dir(self.layout.user_data_dir / "backups")
        path = backups / f"{name}-{_fingerprint(content)[:12]}.bak"
        if not path.exists():
            _atomic_write(path, content)
        return path

    def _write_hooks(self, hooks: dict[str, Any]) -> bytes:
        content = (json.dumps(hooks, indent=2, sort_keys=True) + "\n").encode()
        _atomic_write(self.hooks_path, content)
        return content

    def _runtime_status(self, endpoint: str | None) -> DaemonStatus:
        def settled(status: DaemonStatus) -> bool:
            # A daemon setup has just restarted is briefly unreachable while it
            # binds its socket, which reports as UNAVAILABLE. Treating that as a
            # terminal verdict failed verification on the very first poll and
            # rolled the whole setup back, every time. The deadline — not the
            # first sample — is what bounds this wait.
            if status.health == RuntimeHealth.UNAVAILABLE:
                return False
            return endpoint is None or status.app_server_state == "ready"

        async def wait_for_status() -> DaemonStatus:
            deadline = time.monotonic() + self.app_server_ready_timeout
            status = await self.service.status()
            while not settled(status):
                if time.monotonic() >= deadline:
                    return status
                await asyncio.sleep(self.app_server_poll_interval)
                status = await self.service.status()
            return status

        status = asyncio.run(wait_for_status())
        if status.health != RuntimeHealth.HEALTHY:
            raise IntegrationError(
                f"spotterd verification failed: {status.detail or status.health}"
            )
        if endpoint is not None:
            capabilities = dict(status.app_server_capabilities or ())
            if status.app_server_state != "ready":
                raise IntegrationError(
                    "spotterd did not connect to the configured App Server before the setup timeout"
                )
            if (
                capabilities.get("observation") != "available"
                or capabilities.get("thread_query") != "available"
            ):
                raise IntegrationError(
                    "spotterd App Server connection lacks observation or thread-query capability"
                )
            try:
                identity = status.app_server_version
                validate_codex_host_version(
                    f"codex-cli {identity}" if identity is not None else None
                )
            except CodexHostVersionError as error:
                raise IntegrationError(
                    f"spotterd connected to an incompatible Codex App Server identity: {error}"
                ) from error
        return status

    def _verify(self, owned: list[dict[str, Any]], endpoint: str | None) -> None:
        self._runtime_status(endpoint)
        current, _ = self._read_hooks()
        matches = []
        for event, groups in cast(dict[str, list[dict[str, Any]]], current["hooks"]).items():
            for group in groups:
                for hook in cast(list[dict[str, Any]], group["hooks"]):
                    if self._is_spotter_hook(hook):
                        matches.append((event, group.get("matcher"), hook))
        expected = [(entry["event"], entry["matcher"], entry["hook"]) for entry in owned]
        if sorted(json.dumps(entry, sort_keys=True) for entry in matches) != sorted(
            json.dumps(entry, sort_keys=True) for entry in expected
        ):
            raise IntegrationError(
                "Codex Hook verification found duplicate or drifted Spotter hooks"
            )
        if not self.verifier(self.config_path):
            raise IntegrationError("synthetic Hook round-trip failed")

    def setup(self) -> IntegrationManifest:
        secure_dir(self.lock_path.parent)
        lock = self.lock_path.open("a+")
        flock(lock, LOCK_EX)
        try:
            manifest_before = (
                self.manifest_path.read_bytes() if self.manifest_path.exists() else None
            )
            existing = IntegrationManifest.load(self.manifest_path)
            plan, hooks, hooks_before, removed_hooks = self.inspect(existing)
            codex = self._codex_install()
            if plan.app_server_endpoint is not None:
                try:
                    self.app_server_verifier(plan.app_server_endpoint)
                except Exception as error:
                    detail = redact_app_server_error(error, plan.app_server_endpoint)
                    raise IntegrationError(
                        f"App Server endpoint verification failed: {detail}"
                    ) from error
            hooks_after = (json.dumps(hooks, indent=2, sort_keys=True) + "\n").encode()
            config_before = (
                self.codex_config_path.read_bytes() if self.codex_config_path.exists() else None
            )
            backups = [
                path
                for path in (
                    self._backup("codex-hooks", hooks_before),
                    self._backup("codex-config", config_before) if plan.legacy_plugins else None,
                )
                if path is not None
            ]
            service_preexisting = existing is not None and existing.state == "ready"
            service_was_running = asyncio.run(self.service.status()).available
            config_after = config_before
            removed_plugins: list[str] = []
            service_attempted = False
            previous_hooks = existing.legacy_hooks_removed if existing else []
            previous_plugins = existing.legacy_plugins_removed if existing else []
            retained = existing if existing is not None and existing.state != "purged" else None

            def rollback() -> str | None:
                if hooks_before is None:
                    self.hooks_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.hooks_path, hooks_before)
                for selector in reversed(removed_plugins):
                    with suppress(Exception):
                        codex.add_plugin(selector, self.codex_home)
                if config_before is not None:
                    _atomic_write(self.codex_config_path, config_before)
                if manifest_before is None:
                    self.manifest_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.manifest_path, manifest_before)
                try:
                    if service_was_running:
                        if not service_attempted:
                            return None
                        restored = asyncio.run(self.service.restart())
                        if not restored.available:
                            return (
                                "could not restart the previous spotterd runtime: "
                                f"{restored.detail or restored.health}"
                            )
                        if existing is not None and existing.state == "ready":
                            self._runtime_status(existing.app_server_endpoint)
                    elif service_preexisting:
                        if not service_attempted:
                            return None
                        stopped = asyncio.run(self.service.stop())
                        if stopped.available:
                            return (
                                "could not restore the previously stopped spotterd state: "
                                f"{stopped.detail or stopped.health}"
                            )
                    else:
                        removed = asyncio.run(self.service.uninstall())
                        if removed.available:
                            return (
                                "could not remove the newly installed spotterd service: "
                                f"{removed.detail or removed.health}"
                            )
                except Exception as error:
                    return f"could not restore the previous spotterd runtime: {error}"
                return None

            try:
                if hooks_before != hooks_after:
                    self._write_hooks(hooks)
                for selector in plan.legacy_plugins:
                    codex.remove_plugin(selector, self.codex_home)
                    removed_plugins.append(selector)
                config_after = (
                    self.codex_config_path.read_bytes() if self.codex_config_path.exists() else None
                )
                manifest = IntegrationManifest(
                    schema=MANIFEST_SCHEMA,
                    state="configuring",
                    agent="codex",
                    setup_by=_package_version(),
                    setup_build_id=current_build_identity().build_id,
                    integration_generation=self.integration_generation,
                    runtime_layout=self.layout.integration_record(),
                    agent_path=codex.path,
                    agent_version=codex.version,
                    codex_home=str(self.codex_home),
                    app_server_strategy=(
                        "external-explicit"
                        if plan.app_server_endpoint is not None
                        else "pending-external"
                    ),
                    app_server_endpoint=plan.app_server_endpoint,
                    runtime_mode=plan.runtime_mode,
                    service_registration=plan.service_registration,
                    service_owned=(
                        retained.service_owned
                        if retained
                        else (not service_was_running or not self.portable)
                    ),
                    hooks_file=str(self.hooks_path),
                    hooks_file_created=(
                        hooks_before is None if retained is None else retained.hooks_file_created
                    ),
                    owned_hooks=self._owned_hooks(),
                    config_path=str(self.config_path) if self.config_path is not None else None,
                    legacy_hooks_removed=previous_hooks + removed_hooks,
                    legacy_plugins_removed=list(
                        dict.fromkeys([*previous_plugins, *plan.legacy_plugins])
                    ),
                    config_fingerprint_before=(
                        retained.config_fingerprint_before
                        if retained
                        else (_fingerprint(config_before) if config_before is not None else None)
                    ),
                    config_fingerprint_after=(
                        _fingerprint(config_after) if config_after is not None else None
                    ),
                    hooks_fingerprint_before=(
                        retained.hooks_fingerprint_before
                        if retained
                        else (_fingerprint(hooks_before) if hooks_before is not None else None)
                    ),
                    hooks_fingerprint_after=_fingerprint(hooks_after),
                    backup_paths=list(
                        dict.fromkeys(
                            [
                                *(retained.backup_paths if retained else []),
                                *(str(path) for path in backups),
                            ]
                        )
                    ),
                    created_at=retained.created_at if retained else _now(),
                    updated_at=_now(),
                )
                manifest.save(self.manifest_path)
                restart_required = retained is not None and (
                    retained.state != "ready"
                    or retained.integration_generation != manifest.integration_generation
                    or retained.app_server_strategy != manifest.app_server_strategy
                    or retained.app_server_endpoint != manifest.app_server_endpoint
                )
                service_attempted = True
                started = asyncio.run(
                    self.service.restart() if restart_required else self.service.start()
                )
                if started.health == RuntimeHealth.UNAVAILABLE or (
                    plan.app_server_endpoint is None and started.health != RuntimeHealth.HEALTHY
                ):
                    raise IntegrationError(
                        f"spotterd failed to start: {started.detail or started.health}"
                    )
                owned = self._owned_hooks()
                self._verify(owned, plan.app_server_endpoint)
                manifest = replace(manifest, state="ready", updated_at=_now())
                manifest.save(self.manifest_path)
            except Exception as error:
                rollback_error = rollback()
                detail = str(error)
                endpoints = [
                    endpoint
                    for endpoint in (
                        plan.app_server_endpoint,
                        existing.app_server_endpoint if existing is not None else None,
                    )
                    if endpoint is not None
                ]
                for endpoint in dict.fromkeys(endpoints):
                    detail = redact_app_server_error(detail, endpoint)
                    if rollback_error is not None:
                        rollback_error = redact_app_server_error(rollback_error, endpoint)
                if rollback_error is not None:
                    detail += f"; rollback incomplete: {rollback_error}"
                raise IntegrationError(f"setup rolled back: {detail}") from error
            return manifest
        finally:
            flock(lock, LOCK_UN)
            lock.close()

    def teardown(self) -> bool:
        if not self.manifest_path.exists():
            return False
        secure_dir(self.lock_path.parent)
        lock = self.lock_path.open("a+")
        flock(lock, LOCK_EX)
        try:
            manifest = IntegrationManifest.load(self.manifest_path)
            if manifest is None:
                return False
            if not self._service_explicit:
                self.service = (
                    ManualServiceManager(layout=self.layout)
                    if manifest.runtime_mode == "portable"
                    else ManagedServiceManager(layout=self.layout)
                )
            hooks, before = self._read_hooks()
            events = cast(dict[str, list[dict[str, Any]]], hooks["hooks"])
            for owned in manifest.owned_hooks:
                event = cast(str, owned["event"])
                groups = events.get(event, [])
                kept_groups = []
                for group in groups:
                    kept = [hook for hook in group["hooks"] if hook != owned["hook"]]
                    if kept:
                        group["hooks"] = kept
                        kept_groups.append(group)
                if kept_groups:
                    events[event] = kept_groups
                else:
                    events.pop(event, None)
            try:
                if manifest.hooks_file_created and hooks == {"hooks": {}}:
                    self.hooks_path.unlink(missing_ok=True)
                else:
                    self._write_hooks(hooks)
                if manifest.service_owned:
                    stopped = asyncio.run(self.service.uninstall())
                    if stopped.health != RuntimeHealth.UNAVAILABLE:
                        raise IntegrationError(
                            f"managed service removal failed: {stopped.detail or stopped.health}"
                        )
            except Exception as error:
                if before is None:
                    self.hooks_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.hooks_path, before)
                raise IntegrationError(f"teardown rolled back: {error}") from error
            self.manifest_path.unlink()
            return True
        finally:
            flock(lock, LOCK_UN)
            lock.close()
