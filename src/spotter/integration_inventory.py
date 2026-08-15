"""Ownership-aware inspection and cleanup of Spotter integration resources."""

import asyncio
import copy
import hashlib
import json
import os
import stat
import sys
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Any

from spotter.daemon import DaemonError, ManagedServiceManager, RuntimeHealth
from spotter.integration import (
    MANIFEST_SCHEMA,
    MANIFEST_SCHEMA_NAME,
    MANIFEST_SCHEMA_VERSION,
    IntegrationError,
    IntegrationManifest,
    _atomic_write,
    is_spotter_hook,
)
from spotter.paths import RuntimeLayout, secure_dir
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


@dataclass(frozen=True)
class IntegrationInventorySnapshot:
    manifest: IntegrationManifest | None
    resources: tuple[IntegrationResourceInspection, ...]


@dataclass(frozen=True)
class IntegrationPurgeResult:
    resources: tuple[IntegrationResourceInspection, ...]
    outcomes: dict[tuple[str, str], str]
    failures: dict[tuple[str, str], str]


class IntegrationInventory:
    def __init__(self, layout: RuntimeLayout | None = None, codex_home: Path | None = None) -> None:
        self.layout = layout or RuntimeLayout.discover()
        self.codex_home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.manifest_path = self.layout.integration_manifest
        self.lock_path = self.layout.integration_dir / "codex.lock"

    def inspect(self) -> tuple[IntegrationResourceInspection, ...]:
        return self.snapshot().resources

    def snapshot(self) -> IntegrationInventorySnapshot:
        """Read one exact manifest image and inspect every resource it owns."""
        inspections: list[IntegrationResourceInspection] = []
        manifest = self._manifest(inspections)
        inspections.extend(self._integration_directory(manifest))
        if manifest is None:
            inspections.extend(self._unrecorded_hooks(self.codex_home / "hooks.json"))
        else:
            inspections.extend(self._hooks(manifest))
            inspections.extend(self._service(manifest))
            inspections.extend(self._backups(manifest))
        resources = tuple(
            sorted(inspections, key=lambda item: (item.resource_type, item.resource_id))
        )
        return IntegrationInventorySnapshot(manifest, resources)

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
            manifest = IntegrationManifest(**raw)
            if manifest.state not in {"ready", "purged"}:
                raise IntegrationError(
                    f"manifest has unsupported lifecycle state {manifest.state!r}"
                )
        except (OSError, ValueError, TypeError, IntegrationError) as error:
            inspections.append(self._ambiguous("manifest", self.manifest_path, str(error)))
            return None
        reason = (
            "purged ownership tombstone"
            if manifest.state == "purged"
            else "current manifest schema"
        )
        inspections.append(self._safe("manifest", self.manifest_path, reason))
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
                    else:
                        inspections.append(
                            self._ambiguous("lock", entry, "lock has no ownership manifest")
                        )
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
        return self._service_manager(manifest, path).expected_definition()

    def _service_manager(self, manifest: IntegrationManifest, path: Path) -> ManagedServiceManager:
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
        return ManagedServiceManager(platform=platform, registration_path=path, layout=layout)

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


class IntegrationPurger:
    """Remove only a fully verified integration while retaining its lock inode."""

    def __init__(self, inventory: IntegrationInventory | None = None) -> None:
        self.inventory = inventory or IntegrationInventory()

    def purge(self) -> IntegrationPurgeResult:
        preflight = self.inventory.snapshot()
        if preflight.manifest is None or self._has_uncertain(preflight.resources):
            return self._blocked(preflight.resources)

        secure_dir(self.inventory.lock_path.parent)
        try:
            lock_fd = os.open(
                self.inventory.lock_path,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as error:
            raise IntegrationInventoryError(f"lifecycle lock is unsafe: {error}") from error
        lock = os.fdopen(lock_fd, "r+")
        try:
            metadata = os.fstat(lock.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise IntegrationInventoryError("lifecycle lock is not a regular file")
            flock(lock, LOCK_EX)
            snapshot = self.inventory.snapshot()
            if snapshot.manifest is None or self._has_uncertain(snapshot.resources):
                return self._blocked(snapshot.resources)
            return self._apply(snapshot.manifest, snapshot.resources)
        finally:
            flock(lock, LOCK_UN)
            lock.close()

    @staticmethod
    def _has_uncertain(resources: tuple[IntegrationResourceInspection, ...]) -> bool:
        return any(item.confidence != OwnershipConfidence.SAFE_OWNED for item in resources)

    @staticmethod
    def _key(item: IntegrationResourceInspection) -> tuple[str, str]:
        return item.resource_type, item.resource_id

    def _blocked(
        self, resources: tuple[IntegrationResourceInspection, ...]
    ) -> IntegrationPurgeResult:
        outcomes = {
            self._key(item): (
                "already_absent"
                if item.presence == ResourcePresence.ABSENT
                else "skipped_ambiguous"
            )
            for item in resources
        }
        return IntegrationPurgeResult(resources, outcomes, {})

    def _apply(
        self,
        manifest: IntegrationManifest,
        resources: tuple[IntegrationResourceInspection, ...],
    ) -> IntegrationPurgeResult:
        outcomes = {
            self._key(item): (
                "already_absent"
                if item.presence == ResourcePresence.ABSENT
                else (
                    "preserved_synchronization"
                    if item.resource_type in {"lock", "manifest"}
                    else "planned"
                )
            )
            for item in resources
        }
        failures: dict[tuple[str, str], str] = {}
        if manifest.state == "purged":
            return IntegrationPurgeResult(resources, outcomes, failures)

        hooks_before: bytes | None = None
        hooks_changed = False
        hook_path = Path(manifest.hooks_file)
        try:
            hooks_before, hooks_changed = self._remove_hooks(manifest, hook_path)
        except (OSError, ValueError, IntegrationError) as error:
            self._fail_type(resources, outcomes, failures, "host_hook", str(error))
            return IntegrationPurgeResult(resources, outcomes, failures)

        service_item = next((item for item in resources if item.resource_type == "service"), None)
        if service_item is not None and service_item.presence == ResourcePresence.PRESENT:
            try:
                manager = self.inventory._service_manager(
                    manifest, Path(manifest.service_registration or "")
                )
                status = asyncio.run(manager.uninstall())
                if status.health != RuntimeHealth.UNAVAILABLE:
                    raise IntegrationInventoryError(
                        f"managed service removal failed: {status.detail or status.health}"
                    )
                outcomes[self._key(service_item)] = "removed"
            except Exception as error:
                if hooks_changed:
                    self._restore_hooks(hook_path, hooks_before)
                key = self._key(service_item)
                outcomes[key] = "failed_retryable"
                failures[key] = str(error)
                return IntegrationPurgeResult(resources, outcomes, failures)

        for item in resources:
            if item.resource_type != "backup" or item.presence == ResourcePresence.ABSENT:
                continue
            key = self._key(item)
            try:
                self._unlink_verified_backup(Path(item.resource_id))
            except (OSError, IntegrationInventoryError) as error:
                outcomes[key] = "failed_retryable"
                failures[key] = str(error)
            else:
                outcomes[key] = "removed"

        for item in resources:
            if item.resource_type == "host_hook" and item.presence == ResourcePresence.PRESENT:
                outcomes[self._key(item)] = "removed"

        if failures:
            return IntegrationPurgeResult(resources, outcomes, failures)

        tombstone = replace(
            manifest,
            state="purged",
            service_registration=None,
            service_owned=False,
            owned_hooks=[],
            hooks_file_created=False,
            config_path=None,
            legacy_hooks_removed=[],
            legacy_plugins_removed=[],
            config_fingerprint_before=None,
            config_fingerprint_after=None,
            hooks_fingerprint_before=None,
            hooks_fingerprint_after=None,
            backup_paths=[],
            updated_at=datetime.now(UTC).isoformat(),
        )
        try:
            _atomic_write(
                self.inventory.manifest_path,
                (json.dumps(asdict(tombstone), indent=2, sort_keys=True) + "\n").encode(),
            )
        except OSError as error:
            key = ("manifest", str(self.inventory.manifest_path))
            outcomes[key] = "failed_retryable"
            failures[key] = str(error)
        return IntegrationPurgeResult(resources, outcomes, failures)

    def _remove_hooks(self, manifest: IntegrationManifest, path: Path) -> tuple[bytes | None, bool]:
        try:
            before = path.read_bytes()
        except FileNotFoundError:
            return None, False
        raw = json.loads(before)
        if not isinstance(raw, dict) or not isinstance(raw.get("hooks"), dict):
            raise IntegrationError("Codex hooks file has an unsupported shape")
        updated = copy.deepcopy(raw)
        remaining = Counter(self.inventory._hook_key(entry) for entry in manifest.owned_hooks)
        events = updated["hooks"]
        for event, groups in list(events.items()):
            kept_groups = []
            for group in groups:
                kept_hooks = []
                matcher = group.get("matcher")
                for hook in group["hooks"]:
                    key = self.inventory._hook_key(
                        {"event": event, "matcher": matcher, "hook": hook}
                    )
                    if remaining[key]:
                        remaining[key] -= 1
                    else:
                        kept_hooks.append(hook)
                if kept_hooks:
                    group["hooks"] = kept_hooks
                    kept_groups.append(group)
            if kept_groups:
                events[event] = kept_groups
            else:
                events.pop(event, None)
        if manifest.hooks_file_created and updated == {"hooks": {}}:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode())
        return before, True

    @staticmethod
    def _restore_hooks(path: Path, before: bytes | None) -> None:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, before)

    @staticmethod
    def _unlink_verified_backup(path: Path) -> None:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise IntegrationInventoryError("backup is not a regular file")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        if not path.stem.endswith(f"-{digest}"):
            raise IntegrationInventoryError("backup fingerprint changed")
        path.unlink()

    def _fail_type(
        self,
        resources: tuple[IntegrationResourceInspection, ...],
        outcomes: dict[tuple[str, str], str],
        failures: dict[tuple[str, str], str],
        resource_type: str,
        failure: str,
    ) -> None:
        for item in resources:
            if item.resource_type == resource_type:
                key = self._key(item)
                outcomes[key] = "failed_retryable"
                failures[key] = failure
