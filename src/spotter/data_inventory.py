"""Conservative schema-backed inventory for Spotter durable user data."""

import json
import os
import stat
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

from spotter.budget import LEDGER_SCHEMA, LEDGER_SCHEMA_VERSION
from spotter.experiment import EXPERIMENT_RESULT_SCHEMA, EXPERIMENT_RESULT_SCHEMA_VERSION
from spotter.feedback import FEEDBACK_SCHEMA, FEEDBACK_SCHEMA_VERSION
from spotter.labels import LABEL_SCHEMA, LABEL_SCHEMA_VERSION
from spotter.observability import SOURCE_AUDIT_SCHEMA, SOURCE_AUDIT_SCHEMA_VERSION
from spotter.opportunities import OPPORTUNITY_SCHEMA, OPPORTUNITY_SCHEMA_VERSION
from spotter.paths import RuntimeLayout
from spotter.replay import FORK_MANIFEST_SCHEMA, FORK_MANIFEST_SCHEMA_VERSION
from spotter.repository_registry import OwnershipConfidence, ResourcePresence
from spotter.sampling import SIGNAL_SAMPLING_SCHEMA, SIGNAL_SAMPLING_SCHEMA_VERSION
from spotter.snapshot import (
    JOURNAL_SCHEMA,
    JOURNAL_SCHEMA_VERSION,
    JOURNAL_STATE_SCHEMA,
    JOURNAL_STATE_SCHEMA_VERSION,
)
from spotter.task_corpus import TASK_BATCH_SCHEMA, TASK_BATCH_SCHEMA_VERSION


@dataclass(frozen=True)
class DataResourceInspection:
    relative_path: str
    expected_schema: str | None
    schema_version: int | None
    confidence: OwnershipConfidence
    presence: ResourcePresence
    size_bytes: int | None
    reason: str


@dataclass(frozen=True)
class DataRemovalResult:
    inspection: DataResourceInspection | None
    outcome: str
    failure: str | None = None


SchemaIdentity = tuple[str, int, str]

_ROOT_FILES: dict[Path, SchemaIdentity] = {
    Path("review-spend.json"): (LEDGER_SCHEMA, LEDGER_SCHEMA_VERSION, "json"),
}
_FAMILY_DEFAULTS: dict[str, SchemaIdentity] = {
    "sessions": (JOURNAL_SCHEMA, JOURNAL_SCHEMA_VERSION, "jsonl"),
    "labels": (LABEL_SCHEMA, LABEL_SCHEMA_VERSION, "jsonl"),
    "signal-samples": (SIGNAL_SAMPLING_SCHEMA, SIGNAL_SAMPLING_SCHEMA_VERSION, "jsonl"),
    "opportunities": (OPPORTUNITY_SCHEMA, OPPORTUNITY_SCHEMA_VERSION, "jsonl"),
    "feedback": (FEEDBACK_SCHEMA, FEEDBACK_SCHEMA_VERSION, "jsonl"),
    "source-audit": (SOURCE_AUDIT_SCHEMA, SOURCE_AUDIT_SCHEMA_VERSION, "jsonl"),
    "experiments": (EXPERIMENT_RESULT_SCHEMA, EXPERIMENT_RESULT_SCHEMA_VERSION, "jsonl"),
    "fork-manifests": (FORK_MANIFEST_SCHEMA, FORK_MANIFEST_SCHEMA_VERSION, "json"),
}
_OTHER_SCOPE_ROOTS = {
    "backups",
    "forks",
    # Rebuildable per-repository Git index cache: not durable data, and losing
    # it costs one slow snapshot, never a record.
    "index",
    "integrations",
    "logs",
    "lock",
    "runtime",
    "repos.json",
    "repos.json.lock",
    "snapshot-pins.json",
    "snapshot-pins.json.lock",
    "snapshot-pins.lock",
    "spotter.toml",
}


class DataInventoryError(RuntimeError):
    """The durable-data directory cannot be inspected safely."""


class DataInventory:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or RuntimeLayout.discover().user_data_dir).absolute()

    def inspect(self) -> tuple[DataResourceInspection, ...]:
        try:
            root_status = self.root.stat(follow_symlinks=False)
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise DataInventoryError(f"data directory is inaccessible: {error}") from error
        if not stat.S_ISDIR(root_status.st_mode):
            raise DataInventoryError("data directory is not a directory")
        try:
            entries = sorted(self.root.iterdir())
        except OSError as error:
            raise DataInventoryError(f"data directory is inaccessible: {error}") from error

        inspections: list[DataResourceInspection] = []
        for entry in entries:
            if entry.name in _OTHER_SCOPE_ROOTS:
                continue
            relative = entry.relative_to(self.root)
            identity = _ROOT_FILES.get(relative)
            if identity is not None:
                inspections.append(self._inspect_schema_file(entry, identity))
                continue
            if entry.name == "review-spend.lock":
                continue
            if entry.name in _FAMILY_DEFAULTS:
                inspections.extend(self._inspect_family(entry, entry.name))
                continue
            inspections.append(self._ambiguous(entry, "unrecognized data-root entry"))

        by_path = {item.relative_path: item for item in inspections}
        spend = by_path.get("review-spend.json")
        lock = self.root / "review-spend.lock"
        try:
            lock.stat(follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError:
            inspections.append(self._inaccessible(lock))
        else:
            inspected_lock = self._companion_lock(lock, spend)
            if inspected_lock is not None:
                inspections.append(inspected_lock)
        return tuple(sorted(inspections, key=lambda item: item.relative_path))

    def remove(self, item: DataResourceInspection) -> DataRemovalResult:
        """Remove one schema-proven file after serialised ownership revalidation.

        The lock is deliberately retained. Removing a lock path can split waiting and new writers
        across different inodes, so an empty regular orphan lock is an expected synchronization
        remnant rather than durable user data.
        """
        if item.confidence != OwnershipConfidence.SAFE_OWNED or item.relative_path.endswith(
            ".lock"
        ):
            return DataRemovalResult(item, "skipped_ambiguous", "resource is not removable data")
        path = self.root / item.relative_path
        lock_path = self._lock_path(path)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            self._validate_directory_chain(lock_path.parent)
            descriptor = os.open(lock_path, flags, 0o600)
            lock_status = os.fstat(descriptor)
            if not stat.S_ISREG(lock_status.st_mode):
                raise DataInventoryError("data lock is not a regular file")
            with os.fdopen(descriptor, "r+") as lock:
                descriptor = -1
                flock(lock, LOCK_EX)
                try:
                    self._validate_directory_chain(path.parent)
                    current = self._reinspect(path)
                    if current is None:
                        return DataRemovalResult(None, "already_absent")
                    if current.confidence != OwnershipConfidence.SAFE_OWNED:
                        return DataRemovalResult(current, "skipped_ambiguous", current.reason)
                    self._validate_directory_chain(path.parent)
                    path.unlink()
                    self._sync_directory(path.parent)
                    return DataRemovalResult(current, "removed")
                finally:
                    flock(lock, LOCK_UN)
        except (OSError, DataInventoryError) as error:
            if descriptor >= 0:
                os.close(descriptor)
            return DataRemovalResult(item, "failed_retryable", str(error))

    def _inspect_family(self, directory: Path, family: str) -> list[DataResourceInspection]:
        try:
            status = directory.stat(follow_symlinks=False)
        except OSError:
            return [self._inaccessible(directory)]
        if not stat.S_ISDIR(status.st_mode):
            return [self._ambiguous(directory, "expected data family is not a directory")]

        files: list[Path] = []
        inspections: list[DataResourceInspection] = []
        directories = [directory]
        while directories:
            current = directories.pop()
            try:
                children = sorted(current.iterdir())
            except OSError:
                inspections.append(self._inaccessible(current))
                continue
            for child in children:
                try:
                    child_status = child.stat(follow_symlinks=False)
                except OSError:
                    inspections.append(self._inaccessible(child))
                else:
                    if stat.S_ISDIR(child_status.st_mode):
                        directories.append(child)
                    else:
                        files.append(child)

        locks: list[Path] = []
        for path in sorted(files):
            if path.name.endswith(".lock"):
                locks.append(path)
                continue
            identity = self._identity_for(path, family)
            if identity is None:
                inspections.append(self._ambiguous(path, "unrecognized file in data family"))
            else:
                inspections.append(self._inspect_schema_file(path, identity))

        by_path = {item.relative_path: item for item in inspections}
        for lock in locks:
            companion = str(lock.relative_to(self.root))[: -len(".lock")]
            inspected_lock = self._companion_lock(lock, by_path.get(companion))
            if inspected_lock is not None:
                inspections.append(inspected_lock)
        return inspections

    def _reinspect(self, path: Path) -> DataResourceInspection | None:
        try:
            path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError:
            return self._inaccessible(path)
        relative = path.relative_to(self.root)
        identity = _ROOT_FILES.get(relative)
        if identity is None and relative.parts:
            family = relative.parts[0]
            if family in _FAMILY_DEFAULTS:
                identity = self._identity_for(path, family)
        if identity is None:
            return self._ambiguous(path, "data path no longer has a recognized schema identity")
        return self._inspect_schema_file(path, identity)

    def _lock_path(self, path: Path) -> Path:
        relative = path.relative_to(self.root)
        if relative == Path("review-spend.json"):
            return self.root / "review-spend.lock"
        if relative.parts[0] == "sessions" and path.name.endswith(".jsonl.state"):
            return Path(str(path)[: -len(".state")] + ".lock")
        return path.with_suffix(path.suffix + ".lock")

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _validate_directory_chain(self, directory: Path) -> None:
        current = self.root
        try:
            relative = directory.relative_to(self.root)
        except ValueError as error:
            raise DataInventoryError("data path escaped the durable-data root") from error
        status = current.stat(follow_symlinks=False)
        if not stat.S_ISDIR(status.st_mode):
            raise DataInventoryError(f"data parent is not a directory: {current}")
        for part in relative.parts:
            current /= part
            status = current.stat(follow_symlinks=False)
            if not stat.S_ISDIR(status.st_mode):
                raise DataInventoryError(f"data parent is not a directory: {current}")

    @staticmethod
    def _identity_for(path: Path, family: str) -> SchemaIdentity | None:
        if family == "sessions" and path.name.endswith(".jsonl.state"):
            return JOURNAL_STATE_SCHEMA, JOURNAL_STATE_SCHEMA_VERSION, "json"
        if family == "experiments" and "task-batches" in path.parts:
            return TASK_BATCH_SCHEMA, TASK_BATCH_SCHEMA_VERSION, "jsonl"
        identity = _FAMILY_DEFAULTS[family]
        expected_suffix = ".json" if identity[2] == "json" else ".jsonl"
        return identity if path.name.endswith(expected_suffix) else None

    def _inspect_schema_file(self, path: Path, identity: SchemaIdentity) -> DataResourceInspection:
        schema, version, encoding = identity
        try:
            status = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(status.st_mode):
                return self._ambiguous(path, "schema-bearing data path is not a regular file")
            rows = self._rows(path, encoding)
        except (OSError, UnicodeError, json.JSONDecodeError, DataInventoryError) as error:
            return self._ambiguous(path, f"schema validation failed: {error}")
        if not rows:
            return self._ambiguous(path, "empty file has no ownership schema")
        for number, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                return self._ambiguous(path, f"record {number} is not an object")
            if row.get("schema") != schema or row.get("schema_version") != version:
                return self._ambiguous(
                    path,
                    f"record {number} does not match current {schema} v{version}",
                )
        return DataResourceInspection(
            str(path.relative_to(self.root)),
            schema,
            version,
            OwnershipConfidence.SAFE_OWNED,
            ResourcePresence.PRESENT,
            status.st_size,
            f"all records match current {schema} v{version}",
        )

    @staticmethod
    def _rows(path: Path, encoding: str) -> list[object]:
        if encoding == "json":
            return [json.loads(path.read_text(encoding="utf-8"))]
        rows: list[object] = []
        with path.open(encoding="utf-8") as source:
            for number, line in enumerate(source, start=1):
                if not line.strip():
                    raise DataInventoryError(f"blank record at line {number}")
                rows.append(json.loads(line))
        return rows

    def _companion_lock(
        self, path: Path, companion: DataResourceInspection | None
    ) -> DataResourceInspection | None:
        try:
            status = path.stat(follow_symlinks=False)
        except OSError:
            return self._inaccessible(path)
        if not stat.S_ISREG(status.st_mode):
            return self._ambiguous(path, "lock companion is not a regular file")
        if companion is None:
            return None
        if companion.confidence != OwnershipConfidence.SAFE_OWNED:
            return self._ambiguous(path, "lock has no schema-proven companion data file")
        return DataResourceInspection(
            str(path.relative_to(self.root)),
            companion.expected_schema,
            companion.schema_version,
            OwnershipConfidence.SAFE_OWNED,
            ResourcePresence.PRESENT,
            status.st_size,
            f"exact lock companion for {companion.relative_path}",
        )

    def _ambiguous(self, path: Path, reason: str) -> DataResourceInspection:
        try:
            status = path.stat(follow_symlinks=False)
        except OSError:
            return self._inaccessible(path)
        return DataResourceInspection(
            str(path.relative_to(self.root)),
            None,
            None,
            OwnershipConfidence.AMBIGUOUS,
            ResourcePresence.PRESENT,
            status.st_size if stat.S_ISREG(status.st_mode) else None,
            reason,
        )

    def _inaccessible(self, path: Path) -> DataResourceInspection:
        return DataResourceInspection(
            str(path.relative_to(self.root)),
            None,
            None,
            OwnershipConfidence.INACCESSIBLE,
            ResourcePresence.UNKNOWN,
            None,
            "data path is inaccessible",
        )
