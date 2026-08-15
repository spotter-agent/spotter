"""Conservative schema-backed inventory for Spotter durable user data."""

import json
import stat
from dataclasses import dataclass
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
    "integrations",
    "logs",
    "lock",
    "runtime",
    "repos.json",
    "repos.json.lock",
    "snapshot-pins.json",
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
            inspections.append(self._companion_lock(lock, spend))
        return tuple(sorted(inspections, key=lambda item: item.relative_path))

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
            inspections.append(self._companion_lock(lock, by_path.get(companion)))
        return inspections

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
    ) -> DataResourceInspection:
        try:
            status = path.stat(follow_symlinks=False)
        except OSError:
            return self._inaccessible(path)
        if not stat.S_ISREG(status.st_mode):
            return self._ambiguous(path, "lock companion is not a regular file")
        if companion is None or companion.confidence != OwnershipConfidence.SAFE_OWNED:
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
