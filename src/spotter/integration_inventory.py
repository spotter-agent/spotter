"""Ownership-aware preview of Spotter integration resources."""

import hashlib
import json
import os
import stat
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spotter.daemon import DaemonError, ManagedServiceManager
from spotter.integration import (
    MANIFEST_SCHEMA,
    MANIFEST_SCHEMA_NAME,
    MANIFEST_SCHEMA_VERSION,
    IntegrationError,
    IntegrationManifest,
    is_spotter_hook,
)
from spotter.paths import RuntimeLayout
from spotter.repository_registry import OwnershipConfidence, ResourcePresence


@dataclass(frozen=True)
class IntegrationResourceInspection:
    resource_type: str
    resource_id: str
    confidence: OwnershipConfidence
    presence: ResourcePresence
    reason: str


class IntegrationInventoryError(RuntimeError):
    """Integration ownership cannot be inspected safely."""


class IntegrationInventory:
    def __init__(self, layout: RuntimeLayout | None = None, codex_home: Path | None = None) -> None:
        self.layout = layout or RuntimeLayout.discover()
        self.codex_home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.manifest_path = self.layout.integration_manifest
        self.lock_path = self.layout.integration_dir / "codex.lock"

    def inspect(self) -> tuple[IntegrationResourceInspection, ...]:
        inspections: list[IntegrationResourceInspection] = []
        manifest = self._manifest(inspections)
        inspections.extend(self._integration_directory(manifest))
        if manifest is None:
            inspections.extend(self._unrecorded_hooks(self.codex_home / "hooks.json"))
        else:
            inspections.extend(self._hooks(manifest))
            inspections.extend(self._service(manifest))
            inspections.extend(self._backups(manifest))
        return tuple(sorted(inspections, key=lambda item: (item.resource_type, item.resource_id)))

    def _manifest(
        self, inspections: list[IntegrationResourceInspection]
    ) -> IntegrationManifest | None:
        try:
            status = self.manifest_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            inspections.append(self._inaccessible("manifest", self.manifest_path, str(error)))
            return None
        if not stat.S_ISREG(status.st_mode):
            inspections.append(
                self._ambiguous("manifest", self.manifest_path, "not a regular file")
            )
            return None
        try:
            raw = json.loads(self.manifest_path.read_bytes())
            if not isinstance(raw, dict) or (
                raw.get("schema") != MANIFEST_SCHEMA
                or raw.get("schema_name") != MANIFEST_SCHEMA_NAME
                or raw.get("schema_version") != MANIFEST_SCHEMA_VERSION
            ):
                raise IntegrationError("manifest does not match the current exact schema")
            manifest = IntegrationManifest.load(self.manifest_path)
            if manifest is None:
                raise IntegrationError("manifest disappeared during inspection")
        except (OSError, ValueError, IntegrationError) as error:
            inspections.append(self._ambiguous("manifest", self.manifest_path, str(error)))
            return None
        inspections.append(self._safe("manifest", self.manifest_path, "current manifest schema"))
        return manifest

    def _integration_directory(
        self, manifest: IntegrationManifest | None
    ) -> list[IntegrationResourceInspection]:
        try:
            entries = tuple(self.layout.integration_dir.iterdir())
        except FileNotFoundError:
            return []
        except OSError as error:
            raise IntegrationInventoryError(
                f"integration directory is inaccessible: {error}"
            ) from error
        inspections: list[IntegrationResourceInspection] = []
        for entry in entries:
            if entry == self.manifest_path:
                continue
            if entry == self.lock_path:
                try:
                    status = entry.stat(follow_symlinks=False)
                except OSError as error:
                    inspections.append(self._inaccessible("lock", entry, str(error)))
                else:
                    if not stat.S_ISREG(status.st_mode):
                        inspections.append(self._ambiguous("lock", entry, "not a regular file"))
                    elif manifest is not None:
                        inspections.append(self._safe("lock", entry, "manifest companion lock"))
                continue
            inspections.append(self._ambiguous("integration_file", entry, "unrecognized entry"))
        return inspections

    def _hooks(self, manifest: IntegrationManifest) -> list[IntegrationResourceInspection]:
        path = Path(manifest.hooks_file)
        try:
            actual = self._hook_entries(path)
        except FileNotFoundError:
            actual = []
        except (OSError, ValueError) as error:
            return [self._ambiguous("hooks_file", path, str(error))]
        expected = Counter(self._hook_key(entry) for entry in manifest.owned_hooks)
        found = Counter(self._hook_key(entry) for entry in actual)
        inspections: list[IntegrationResourceInspection] = []
        for key, count in expected.items():
            for index in range(count):
                present = found[key] > index
                inspections.append(
                    IntegrationResourceInspection(
                        "host_hook",
                        self._hook_id(key, index),
                        OwnershipConfidence.SAFE_OWNED,
                        ResourcePresence.PRESENT if present else ResourcePresence.ABSENT,
                        "exact recorded hook matches"
                        if present
                        else "recorded hook is already absent",
                    )
                )
        remaining = found - expected
        for key, count in remaining.items():
            if is_spotter_hook(json.loads(key[2])):
                for index in range(expected[key], expected[key] + count):
                    inspections.append(
                        IntegrationResourceInspection(
                            "host_hook",
                            self._hook_id(key, index),
                            OwnershipConfidence.AMBIGUOUS,
                            ResourcePresence.PRESENT,
                            "Spotter-like hook is not recorded by the manifest",
                        )
                    )
        return inspections

    def _unrecorded_hooks(self, path: Path) -> list[IntegrationResourceInspection]:
        try:
            entries = self._hook_entries(path)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as error:
            return [self._ambiguous("hooks_file", path, str(error))]
        return [
            IntegrationResourceInspection(
                "host_hook",
                self._hook_id(self._hook_key(entry), index),
                OwnershipConfidence.AMBIGUOUS,
                ResourcePresence.PRESENT,
                "Spotter-like hook has no ownership manifest",
            )
            for index, entry in enumerate(entries)
            if is_spotter_hook(entry.get("hook"))
        ]

    def _service(self, manifest: IntegrationManifest) -> list[IntegrationResourceInspection]:
        if not manifest.service_owned or manifest.service_registration is None:
            return []
        path = Path(manifest.service_registration)
        try:
            status = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return [self._safe("service", path, "recorded service is already absent", absent=True)]
        except OSError as error:
            return [self._inaccessible("service", path, str(error))]
        if not stat.S_ISREG(status.st_mode):
            return [self._ambiguous("service", path, "service registration is not a regular file")]
        try:
            expected = self._service_definition(manifest, path)
            actual = path.read_bytes()
        except (OSError, IntegrationInventoryError, DaemonError) as error:
            return [self._ambiguous("service", path, str(error))]
        if actual != expected:
            return [
                self._ambiguous("service", path, "service definition differs from manifest layout")
            ]
        return [self._safe("service", path, "exact service definition matches")]

    def _backups(self, manifest: IntegrationManifest) -> list[IntegrationResourceInspection]:
        inspections = []
        for raw_path in manifest.backup_paths:
            path = Path(raw_path)
            try:
                status = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                inspections.append(
                    self._safe("backup", path, "recorded backup is absent", absent=True)
                )
                continue
            except OSError as error:
                inspections.append(self._inaccessible("backup", path, str(error)))
                continue
            if not stat.S_ISREG(status.st_mode):
                inspections.append(self._ambiguous("backup", path, "backup is not a regular file"))
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            except OSError as error:
                inspections.append(self._inaccessible("backup", path, str(error)))
                continue
            if not path.stem.endswith(f"-{digest}"):
                inspections.append(self._ambiguous("backup", path, "backup fingerprint changed"))
            else:
                inspections.append(self._safe("backup", path, "backup fingerprint matches"))
        return inspections

    @staticmethod
    def _hook_entries(path: Path) -> list[dict[str, Any]]:
        status = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("hooks file is not a regular file")
        raw = json.loads(path.read_bytes())
        hooks = raw.get("hooks") if isinstance(raw, dict) else None
        if not isinstance(hooks, dict):
            raise ValueError("hooks file has an unsupported shape")
        entries = []
        for event, groups in hooks.items():
            if not isinstance(event, str) or not isinstance(groups, list):
                raise ValueError("hooks file has an unsupported event shape")
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    raise ValueError("hooks file has an unsupported hook group")
                for hook in group["hooks"]:
                    entries.append({"event": event, "matcher": group.get("matcher"), "hook": hook})
        return entries

    @staticmethod
    def _hook_key(entry: dict[str, Any]) -> tuple[str, str | None, str]:
        return (
            str(entry.get("event")),
            entry.get("matcher") if isinstance(entry.get("matcher"), str) else None,
            json.dumps(entry.get("hook"), sort_keys=True, separators=(",", ":")),
        )

    @staticmethod
    def _hook_id(key: tuple[str, str | None, str], index: int) -> str:
        digest = hashlib.sha256(key[2].encode()).hexdigest()[:12]
        return f"{key[0]}:{key[1] or '-'}:{digest}:{index}"

    def _service_definition(self, manifest: IntegrationManifest, path: Path) -> bytes:
        record = manifest.runtime_layout
        required = ("user_config_dir", "user_data_dir", "integration_dir", "runtime_dir", "log_dir")
        if any(not isinstance(record.get(key), str) for key in required):
            raise IntegrationInventoryError("manifest lacks a reconstructable runtime layout")
        daemon = record.get("daemon_executable")
        if not isinstance(daemon, str):
            raise IntegrationInventoryError("manifest lacks the daemon executable")
        cli = record.get("cli_executable")
        layout = RuntimeLayout(
            cli_executable=Path(cli) if isinstance(cli, str) else None,
            daemon_executable=Path(daemon),
            package_assets_dir=self.layout.package_assets_dir,
            user_config_dir=Path(record["user_config_dir"]),
            user_data_dir=Path(record["user_data_dir"]),
            integration_dir=Path(record["integration_dir"]),
            runtime_dir=Path(record["runtime_dir"]),
            log_dir=Path(record["log_dir"]),
            user_home=self.layout.user_home,
            system_config_home=self.layout.system_config_home,
        )
        platform = (
            "darwin"
            if path.suffix == ".plist"
            else ("linux" if path.suffix == ".service" else sys.platform)
        )
        return ManagedServiceManager(
            platform=platform, registration_path=path, layout=layout
        ).expected_definition()

    @staticmethod
    def _safe(
        resource_type: str, path: Path, reason: str, *, absent: bool = False
    ) -> IntegrationResourceInspection:
        return IntegrationResourceInspection(
            resource_type,
            str(path),
            OwnershipConfidence.SAFE_OWNED,
            ResourcePresence.ABSENT if absent else ResourcePresence.PRESENT,
            reason,
        )

    @staticmethod
    def _ambiguous(resource_type: str, path: Path, reason: str) -> IntegrationResourceInspection:
        return IntegrationResourceInspection(
            resource_type,
            str(path),
            OwnershipConfidence.AMBIGUOUS,
            ResourcePresence.PRESENT,
            reason,
        )

    @staticmethod
    def _inaccessible(resource_type: str, path: Path, reason: str) -> IntegrationResourceInspection:
        return IntegrationResourceInspection(
            resource_type,
            str(path),
            OwnershipConfidence.INACCESSIBLE,
            ResourcePresence.UNKNOWN,
            reason,
        )
