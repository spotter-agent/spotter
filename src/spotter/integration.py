"""Transactional Codex integration ownership and migration."""

import asyncio
import copy
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any, cast

from spotter.build_identity import current_build_identity
from spotter.config import ConfigurationError, SpotterConfig
from spotter.daemon import (
    ManagedServiceManager,
    ManualServiceManager,
    RuntimeHealth,
    ServiceManager,
)
from spotter.doctor import OK, check_roundtrip
from spotter.paths import RuntimeLayout, RuntimeLayoutError, secure_dir

MANIFEST_SCHEMA = 4


class IntegrationError(RuntimeError):
    """The requested integration transaction could not complete safely."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _package_version() -> str:
    return current_build_identity().version


def _integration_generation(layout: RuntimeLayout, config_path: Path | None) -> str:
    """Fence generated host state to one package build and stable layout."""
    payload = {
        "schema": MANIFEST_SCHEMA,
        "build_id": current_build_identity().build_id,
        "bridge_command": layout.bridge_command,
        "daemon_command": layout.daemon_command,
        "package_assets_dir": str(layout.package_assets_dir),
        "config_path": str(config_path) if config_path is not None else None,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.chmod(0o600)
    os.replace(temporary, path)


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
        return [
            "Codex hooks: "
            f"{'update' if self.hooks_changed else 'already current'} {self.hooks_file}",
            f"Legacy Spotter hooks to migrate: {self.legacy_hooks}",
            f"Legacy Spotter plugins to remove: {len(self.legacy_plugins)}",
            f"Runtime: {self.runtime_mode}",
            "App Server: pending external endpoint (#85/#87)",
        ]


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
        self.integration_generation = _integration_generation(self.layout, self.config_path)
        self.verifier = verifier or (
            lambda path: check_roundtrip(path, command=self.layout.bridge_command).status == OK
        )
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

    def _migrate_hooks(self, raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        updated = copy.deepcopy(raw)
        events = cast(dict[str, list[dict[str, Any]]], updated.setdefault("hooks", {}))
        removed: list[dict[str, Any]] = []
        owned = self._owned_hooks()
        for event, groups in list(events.items()):
            kept_groups: list[dict[str, Any]] = []
            for group in groups:
                kept_hooks = []
                for hook in cast(list[dict[str, Any]], group["hooks"]):
                    if self._is_spotter_hook(hook):
                        if not any(
                            (event, group.get("matcher"), hook)
                            == (entry["event"], entry["matcher"], entry["hook"])
                            for entry in owned
                        ):
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
        self,
    ) -> tuple[IntegrationPlan, dict[str, Any], bytes | None, list[dict[str, Any]]]:
        try:
            self.layout.validate_persistent()
        except RuntimeLayoutError as error:
            raise IntegrationError(str(error)) from error
        codex = self._codex_install()
        if not codex.supports_remote or not codex.supports_app_server:
            raise IntegrationError("Codex lacks the required app-server/--remote capability")
        if self.config_path is not None:
            try:
                SpotterConfig.from_toml(self.config_path)
            except (OSError, tomllib.TOMLDecodeError, ConfigurationError) as error:
                raise IntegrationError(f"Spotter config is unusable: {error}") from error
        hooks, before = self._read_hooks()
        migrated, removed = self._migrate_hooks(hooks)
        after = (json.dumps(migrated, indent=2, sort_keys=True) + "\n").encode()
        plan = IntegrationPlan(
            hooks_file=self.hooks_path,
            hooks_changed=before != after,
            legacy_hooks=len(removed),
            legacy_plugins=self._legacy_plugins(),
            runtime_mode="portable" if self.portable else "managed",
            service_registration=self._service_registration(),
            app_server_endpoint=None,
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

    def _verify(self, owned: list[dict[str, Any]]) -> None:
        status = asyncio.run(self.service.status())
        if status.health != RuntimeHealth.HEALTHY:
            raise IntegrationError(
                f"spotterd verification failed: {status.detail or status.health}"
            )
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
            existing = IntegrationManifest.load(self.manifest_path)
            plan, hooks, hooks_before, removed_hooks = self.inspect()
            codex = self._codex_install()
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

            def rollback() -> None:
                if hooks_before is None:
                    self.hooks_path.unlink(missing_ok=True)
                else:
                    _atomic_write(self.hooks_path, hooks_before)
                for selector in reversed(removed_plugins):
                    with suppress(Exception):
                        codex.add_plugin(selector, self.codex_home)
                if config_before is not None:
                    _atomic_write(self.codex_config_path, config_before)
                if not service_preexisting and not service_was_running:
                    with suppress(Exception):
                        asyncio.run(self.service.uninstall())

            try:
                if hooks_before != hooks_after:
                    self._write_hooks(hooks)
                for selector in plan.legacy_plugins:
                    codex.remove_plugin(selector, self.codex_home)
                    removed_plugins.append(selector)
                config_after = (
                    self.codex_config_path.read_bytes() if self.codex_config_path.exists() else None
                )
                started = asyncio.run(self.service.start())
                if started.health != RuntimeHealth.HEALTHY:
                    raise IntegrationError(
                        f"spotterd failed to start: {started.detail or started.health}"
                    )
                owned = self._owned_hooks()
                self._verify(owned)
            except Exception as error:
                rollback()
                raise IntegrationError(f"setup rolled back: {error}") from error

            previous_hooks = existing.legacy_hooks_removed if existing else []
            previous_plugins = existing.legacy_plugins_removed if existing else []
            manifest = IntegrationManifest(
                schema=MANIFEST_SCHEMA,
                state="ready",
                agent="codex",
                setup_by=_package_version(),
                setup_build_id=current_build_identity().build_id,
                integration_generation=self.integration_generation,
                runtime_layout=self.layout.integration_record(),
                agent_path=codex.path,
                agent_version=codex.version,
                codex_home=str(self.codex_home),
                app_server_strategy="pending-external",
                app_server_endpoint=plan.app_server_endpoint,
                runtime_mode=plan.runtime_mode,
                service_registration=plan.service_registration,
                service_owned=(
                    existing.service_owned
                    if existing
                    else (not service_was_running or not self.portable)
                ),
                hooks_file=str(self.hooks_path),
                hooks_file_created=(
                    hooks_before is None if existing is None else existing.hooks_file_created
                ),
                owned_hooks=self._owned_hooks(),
                legacy_hooks_removed=previous_hooks + removed_hooks,
                legacy_plugins_removed=list(
                    dict.fromkeys([*previous_plugins, *plan.legacy_plugins])
                ),
                config_fingerprint_before=(
                    existing.config_fingerprint_before
                    if existing
                    else (_fingerprint(config_before) if config_before is not None else None)
                ),
                config_fingerprint_after=(
                    _fingerprint(config_after) if config_after is not None else None
                ),
                hooks_fingerprint_before=(
                    existing.hooks_fingerprint_before
                    if existing
                    else (_fingerprint(hooks_before) if hooks_before is not None else None)
                ),
                hooks_fingerprint_after=_fingerprint(hooks_after),
                backup_paths=list(
                    dict.fromkeys(
                        [
                            *(existing.backup_paths if existing else []),
                            *(str(path) for path in backups),
                        ]
                    )
                ),
                created_at=existing.created_at if existing else _now(),
                updated_at=_now(),
            )
            try:
                manifest.save(self.manifest_path)
            except Exception as error:
                rollback()
                raise IntegrationError(f"setup rolled back: {error}") from error
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
