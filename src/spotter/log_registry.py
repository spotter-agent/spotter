"""Durable ownership records for mutable log files created by Spotter."""

import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fcntl import LOCK_EX, flock
from pathlib import Path

from spotter.build_identity import current_build_identity
from spotter.paths import RuntimeLayout, secure_dir
from spotter.repository_registry import OwnershipConfidence, ResourcePresence

LOG_REGISTRY_SCHEMA = "spotter.log_registry"
LOG_REGISTRY_SCHEMA_VERSION = 1


class LogRegistryError(RuntimeError):
    """The log registry is incompatible, corrupt, or cannot be updated."""


@dataclass(frozen=True)
class OwnedLog:
    resource_type: str
    resource_id: str
    owner: str
    generation_id: str
    created_by_spotter_version: str
    created_at: str
    expected_path: str
    identity_path: str
    device: int
    inode: int


@dataclass(frozen=True)
class LogResourceInspection:
    resource_id: str
    expected_path: str
    identity_path: str | None
    generation_id: str | None
    confidence: OwnershipConfidence
    presence: ResourcePresence
    size_bytes: int | None
    reason: str
    owned: OwnedLog | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _string(raw: dict[str, object], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise LogRegistryError(f"{context}.{key} must be a non-empty string")
    return value


def _integer(raw: dict[str, object], key: str, context: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LogRegistryError(f"{context}.{key} must be a non-negative integer")
    return value


def _owned_log(raw: object, index: int) -> OwnedLog:
    context = f"log registry resource {index}"
    if not isinstance(raw, dict):
        raise LogRegistryError(f"{context} must be an object")
    parsed = OwnedLog(
        resource_type=_string(raw, "resource_type", context),
        resource_id=_string(raw, "resource_id", context),
        owner=_string(raw, "owner", context),
        generation_id=_string(raw, "generation_id", context),
        created_by_spotter_version=_string(raw, "created_by_spotter_version", context),
        created_at=_string(raw, "created_at", context),
        expected_path=_string(raw, "expected_path", context),
        identity_path=_string(raw, "identity_path", context),
        device=_integer(raw, "device", context),
        inode=_integer(raw, "inode", context),
    )
    if parsed.resource_type != "log" or parsed.owner != "spotter":
        raise LogRegistryError(f"{context} has unsupported ownership metadata")
    return parsed


def _atomic_write(path: Path, resources: tuple[OwnedLog, ...]) -> None:
    secure_dir(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
            json.dump(
                {
                    "schema": LOG_REGISTRY_SCHEMA,
                    "schema_version": LOG_REGISTRY_SCHEMA_VERSION,
                    "resources": [asdict(resource) for resource in resources],
                },
                sink,
                indent=2,
                sort_keys=True,
            )
            sink.write("\n")
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class LogRegistry:
    """Claim exact log paths without adopting pre-existing or replaced files."""

    def __init__(self, path: Path | None = None, *, log_dir: Path | None = None) -> None:
        layout = RuntimeLayout.discover()
        self.log_dir = (log_dir or layout.log_dir).absolute()
        self.path = path or self.log_dir / "ownership.json"
        self.lock_path = self.path.with_suffix(".lock")
        self.identity_dir = self.log_dir / ".ownership"

    def load(self) -> tuple[OwnedLog, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise LogRegistryError(f"log registry is unreadable: {error}") from error
        if not isinstance(raw, dict):
            raise LogRegistryError("log registry must be a JSON object")
        if raw.get("schema") != LOG_REGISTRY_SCHEMA:
            raise LogRegistryError(f"unsupported log registry schema {raw.get('schema')!r}")
        version = raw.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise LogRegistryError("log registry schema_version must be an integer")
        if version != LOG_REGISTRY_SCHEMA_VERSION:
            direction = "newer" if version > LOG_REGISTRY_SCHEMA_VERSION else "unsupported"
            raise LogRegistryError(
                f"{direction} log registry schema v{version}; this build understands "
                f"v{LOG_REGISTRY_SCHEMA_VERSION}"
            )
        raw_resources = raw.get("resources")
        if not isinstance(raw_resources, list):
            raise LogRegistryError("log registry resources must be a list")
        resources = tuple(_owned_log(item, index) for index, item in enumerate(raw_resources))
        reserved = {self.path.absolute(), self.lock_path.absolute()}
        for resource in resources:
            expected = Path(resource.expected_path)
            identity_path = Path(resource.identity_path)
            if (
                not expected.is_absolute()
                or expected.parent != self.log_dir
                or expected in reserved
                or not identity_path.is_absolute()
                or identity_path.parent != self.identity_dir
            ):
                raise LogRegistryError(
                    f"log registry resource path is outside its ownership boundary: {expected}"
                )
        resource_ids = [item.resource_id for item in resources]
        expected_paths = [item.expected_path for item in resources]
        identity_paths = [item.identity_path for item in resources]
        if (
            len(resource_ids) != len(set(resource_ids))
            or len(expected_paths) != len(set(expected_paths))
            or len(identity_paths) != len(set(identity_paths))
        ):
            raise LogRegistryError("log registry contains duplicate resources")
        return resources

    def claim(self, path: Path, resource_id: str) -> bool:
        """Create and record a new log, or verify an existing exact claim.

        ``False`` leaves an existing unrecorded/replaced path untouched and
        unowned. This lets logging continue without ever turning a filename
        match into deletion authority.
        """
        expected = path.absolute()
        if expected.parent != self.log_dir or expected in {
            self.path.absolute(),
            self.lock_path.absolute(),
        }:
            raise LogRegistryError(f"log path is outside the owned log directory: {expected}")
        if not resource_id:
            raise LogRegistryError("log resource id must be non-empty")

        secure_dir(self.log_dir)
        with self.lock_path.open("a+") as lock:
            flock(lock.fileno(), LOCK_EX)
            resources = self.load()
            matches = tuple(item for item in resources if item.resource_id == resource_id)
            if len(matches) > 1:
                raise LogRegistryError(f"duplicate log resource id {resource_id!r}")
            existing = matches[0] if matches else None
            previous = existing
            if existing is not None and existing.expected_path != str(expected):
                return False

            try:
                status = expected.stat(follow_symlinks=False)
            except FileNotFoundError:
                if existing is not None:
                    anchor, ambiguous = self._anchor_status(existing)
                    if ambiguous:
                        return False
                    if anchor is not None:
                        try:
                            os.link(existing.identity_path, expected, follow_symlinks=False)
                        except OSError:
                            return False
                        return True
                    existing = None
                status = self._create(expected)
            except OSError as error:
                raise LogRegistryError(f"could not inspect log path {expected}: {error}") from error
            else:
                if existing is None:
                    return False

            if not stat.S_ISREG(status.st_mode):
                return False
            if existing is not None:
                anchor, ambiguous = self._anchor_status(existing)
                return (
                    not ambiguous
                    and anchor is not None
                    and (
                        existing.device,
                        existing.inode,
                    )
                    == (status.st_dev, status.st_ino)
                )

            identity = current_build_identity()
            generation_id = str(uuid.uuid4())
            identity_path = secure_dir(self.identity_dir) / generation_id
            try:
                os.link(expected, identity_path, follow_symlinks=False)
            except OSError:
                return False
            claimed = OwnedLog(
                resource_type="log",
                resource_id=resource_id,
                owner="spotter",
                generation_id=generation_id,
                created_by_spotter_version=identity.version,
                created_at=_now(),
                expected_path=str(expected),
                identity_path=str(identity_path),
                device=status.st_dev,
                inode=status.st_ino,
            )
            updated = tuple(
                claimed if item.resource_id == resource_id else item for item in resources
            )
            if previous is None:
                updated += (claimed,)
            _atomic_write(self.path, updated)
            return True

    def inspect(
        self, resources: tuple[OwnedLog, ...] | None = None
    ) -> tuple[LogResourceInspection, ...]:
        """Inspect exact owned anchors and surface every unowned log-dir entry."""
        owned_logs = resources if resources is not None else self.load()
        inspections: list[LogResourceInspection] = []
        recognized_paths: set[Path] = set()
        known_anchors = {Path(resource.identity_path) for resource in owned_logs}

        for resource in owned_logs:
            expected = Path(resource.expected_path)
            anchor, ambiguous = self._anchor_status(resource)
            if ambiguous:
                inspections.append(
                    self._inspection(
                        resource,
                        OwnershipConfidence.AMBIGUOUS,
                        ResourcePresence.UNKNOWN,
                        None,
                        "identity anchor no longer matches its ownership record",
                    )
                )
                recognized_paths.add(expected)
                continue
            if anchor is None:
                try:
                    expected_status = expected.stat(follow_symlinks=False)
                except FileNotFoundError:
                    inspections.append(
                        self._inspection(
                            resource,
                            OwnershipConfidence.SAFE_OWNED,
                            ResourcePresence.ABSENT,
                            0,
                            "owned log and identity anchor are already absent",
                        )
                    )
                except OSError:
                    inspections.append(
                        self._inspection(
                            resource,
                            OwnershipConfidence.INACCESSIBLE,
                            ResourcePresence.UNKNOWN,
                            None,
                            "identity anchor is absent and the log path is inaccessible",
                        )
                    )
                else:
                    inspections.append(
                        self._inspection(
                            resource,
                            OwnershipConfidence.AMBIGUOUS,
                            ResourcePresence.PRESENT,
                            expected_status.st_size
                            if stat.S_ISREG(expected_status.st_mode)
                            else None,
                            "log path exists without its identity anchor",
                        )
                    )
                recognized_paths.add(expected)
                continue

            public_reason = "public log path is absent"
            try:
                expected_status = expected.stat(follow_symlinks=False)
            except FileNotFoundError:
                recognized_paths.add(expected)
            except OSError:
                public_reason = "public log path is inaccessible and will not be touched"
            else:
                if (expected_status.st_dev, expected_status.st_ino) == (
                    resource.device,
                    resource.inode,
                ):
                    recognized_paths.add(expected)
                    public_reason = "public log path and identity anchor match"
                else:
                    public_reason = "public log path was replaced and will not be touched"
            inspections.append(
                self._inspection(
                    resource,
                    OwnershipConfidence.SAFE_OWNED,
                    ResourcePresence.PRESENT if anchor.st_size else ResourcePresence.ABSENT,
                    anchor.st_size,
                    f"exact owned identity anchor; {public_reason}",
                )
            )

        reserved = {self.path.absolute(), self.lock_path.absolute(), self.identity_dir}
        if self.log_dir.exists():
            try:
                entries = tuple(self.log_dir.iterdir())
            except OSError as error:
                raise LogRegistryError(f"log directory is inaccessible: {error}") from error
            for entry in entries:
                absolute = entry.absolute()
                if absolute in reserved or absolute in recognized_paths:
                    continue
                inspections.append(self._unowned_inspection(absolute))
        if self.identity_dir.exists():
            try:
                anchors = tuple(self.identity_dir.iterdir())
            except OSError as error:
                raise LogRegistryError(
                    f"log identity directory is inaccessible: {error}"
                ) from error
            for anchor_path in anchors:
                if anchor_path.absolute() not in known_anchors:
                    inspections.append(self._unowned_inspection(anchor_path.absolute()))
        return tuple(inspections)

    def clear(self, resource: OwnedLog) -> int:
        """Truncate one exact owned anchor and return the removed byte count."""
        secure_dir(self.log_dir)
        with self.lock_path.open("a+") as lock:
            flock(lock.fileno(), LOCK_EX)
            if resource not in self.load():
                raise LogRegistryError(f"log ownership changed for {resource.resource_id}")
            flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(resource.identity_path, flags)
            except OSError as error:
                raise LogRegistryError(
                    f"could not open identity anchor for {resource.resource_id}: {error}"
                ) from error
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode) or (status.st_dev, status.st_ino) != (
                    resource.device,
                    resource.inode,
                ):
                    raise LogRegistryError(
                        f"identity anchor changed for {resource.resource_id}; refusing to clear"
                    )
                removed = status.st_size
                os.ftruncate(descriptor, 0)
                os.fsync(descriptor)
                return removed
            finally:
                os.close(descriptor)

    @staticmethod
    def _inspection(
        resource: OwnedLog,
        confidence: OwnershipConfidence,
        presence: ResourcePresence,
        size_bytes: int | None,
        reason: str,
    ) -> LogResourceInspection:
        return LogResourceInspection(
            resource.resource_id,
            resource.expected_path,
            resource.identity_path,
            resource.generation_id,
            confidence,
            presence,
            size_bytes,
            reason,
            resource,
        )

    @staticmethod
    def _unowned_inspection(path: Path) -> LogResourceInspection:
        try:
            status = path.stat(follow_symlinks=False)
        except OSError:
            presence = ResourcePresence.UNKNOWN
            size_bytes = None
        else:
            presence = ResourcePresence.PRESENT
            size_bytes = status.st_size if stat.S_ISREG(status.st_mode) else None
        return LogResourceInspection(
            f"unregistered:{path.name}",
            str(path),
            None,
            None,
            OwnershipConfidence.AMBIGUOUS,
            presence,
            size_bytes,
            "no exact Spotter log ownership record",
            None,
        )

    @staticmethod
    def _create(path: Path) -> os.stat_result:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        return path.stat(follow_symlinks=False)

    @staticmethod
    def _anchor_status(resource: OwnedLog) -> tuple[os.stat_result | None, bool]:
        try:
            status = Path(resource.identity_path).stat(follow_symlinks=False)
        except FileNotFoundError:
            return None, False
        except OSError:
            return None, True
        if not stat.S_ISREG(status.st_mode) or (status.st_dev, status.st_ino) != (
            resource.device,
            resource.inode,
        ):
            return None, True
        return status, False
