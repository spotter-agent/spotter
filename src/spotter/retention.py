"""Reachability roots that keep durable repository artifacts live."""

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from spotter.replay import ReplayError, load_fork_manifest
from spotter.repository_registry import (
    OwnershipConfidence,
    RepositoryEntry,
    RepositoryResourceInspection,
    ResourcePresence,
)
from spotter.snapshot import SnapshotError, snapshot_references
from spotter.snapshot_pins import SnapshotPinError, SnapshotPinStore


class RetentionState(StrEnum):
    REFERENCED = "REFERENCED"
    UNREFERENCED = "UNREFERENCED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class ArtifactRetention:
    state: RetentionState
    references: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


ArtifactKey = tuple[str, str]


def inspect_snapshot_retention(
    entries: tuple[RepositoryEntry, ...],
    inspections: tuple[RepositoryResourceInspection, ...],
    data_dir: Path,
) -> dict[ArtifactKey, ArtifactRetention]:
    """Build conservative roots for every registered snapshot ref.

    A malformed durable source makes otherwise-unreferenced snapshots unknown;
    a known reference still wins because it is already sufficient to retain.
    """
    keys = {
        (entry.registry_entry_id, resource.expected_target)
        for entry in entries
        for resource in entry.resources
        if resource.resource_type == "git_ref"
    }
    references: dict[ArtifactKey, set[str]] = {key: set() for key in keys}
    diagnostics: dict[ArtifactKey, set[str]] = {key: set() for key in keys}
    sessions_dir = data_dir / "sessions"

    by_target: dict[str, set[ArtifactKey]] = {}
    for key in keys:
        by_target.setdefault(key[1], set()).add(key)

    try:
        pins = SnapshotPinStore(data_dir / "snapshot-pins.json").load()
    except SnapshotPinError as error:
        if not keys:
            raise
        _record_unknown(diagnostics, f"manual pin reachability unavailable: {error}")
    else:
        for pin in pins:
            key = (pin.registry_entry_id, pin.snapshot_sha)
            if key not in references:
                if not keys:
                    raise SnapshotPinError(
                        f"manual pin {pin.pin_id} has no repository ownership record"
                    )
                _record_unknown(
                    diagnostics,
                    f"manual pin {pin.pin_id} does not match a registered snapshot",
                )
                continue
            references[key].add(f"manual_pin:{pin.pin_id}")

    try:
        # Journals retain the path observed at the time, while the registry
        # follows a moved repository by Git identity. Matching without a path
        # filter may over-retain the same commit in another repository, but it
        # cannot silently lose lineage after a move.
        journal_roots = snapshot_references(sessions_dir)
    except (OSError, UnicodeError, SnapshotError) as error:
        message = f"journal reachability unavailable: {error}"
        for key in keys:
            diagnostics[key].add(message)
    else:
        for sha, holders in journal_roots.items():
            for key in by_target.get(sha, ()):
                references[key].update(
                    f"journal:{session}:step:{step}" for session, step in holders
                )

    manifest_dir = data_dir / "fork-manifests"
    for path in sorted(manifest_dir.glob("*.json")) if manifest_dir.exists() else ():
        try:
            manifest = load_fork_manifest(path)
        except ReplayError as error:
            message = f"fork manifest reachability unavailable: {error}"
            for key in keys:
                diagnostics[key].add(message)
            continue
        for key in by_target.get(manifest.prefix.snapshot_sha, ()):
            references[key].add(f"fork_manifest:{manifest.fork_id}:{manifest.status.value}")

    experiment_dir = data_dir / "experiments"
    for path in sorted(experiment_dir.rglob("*.jsonl")) if experiment_dir.exists() else ():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            _record_unknown(
                diagnostics,
                f"experiment result reachability unavailable: {path}: {error}",
            )
            continue
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise TypeError("record is not an object")
            except (json.JSONDecodeError, TypeError, UnicodeError) as error:
                _record_unknown(
                    diagnostics,
                    f"experiment result reachability unavailable: {path} line {number}: {error}",
                )
                continue
            manifest_value = row.get("fork_manifest")
            if manifest_value is None:
                continue
            if not isinstance(manifest_value, str) or not manifest_value:
                _record_unknown(
                    diagnostics,
                    f"experiment result reachability unavailable: {path} line {number} "
                    "has an invalid fork manifest path",
                )
                continue
            referenced_path = Path(manifest_value)
            if not referenced_path.is_absolute():
                referenced_path = path.parent / referenced_path
            try:
                manifest = load_fork_manifest(referenced_path)
            except ReplayError as error:
                _record_unknown(
                    diagnostics,
                    f"experiment result reachability unavailable: {path} line {number}: {error}",
                )
                continue
            for key in by_target.get(manifest.prefix.snapshot_sha, ()):
                references[key].add(
                    f"experiment_result:{path.relative_to(data_dir)}:line:{number}:"
                    f"fork:{manifest.fork_id}"
                )

    for inspection in inspections:
        if (
            inspection.resource_type != "worktree"
            or inspection.confidence != OwnershipConfidence.SAFE_OWNED
            or inspection.presence != ResourcePresence.PRESENT
        ):
            continue
        key = (inspection.registry_entry_id, inspection.expected_target)
        if key in references:
            references[key].add(f"worktree:{inspection.resource_id}")

    result: dict[ArtifactKey, ArtifactRetention] = {}
    for key in keys:
        roots = tuple(sorted(references[key]))
        errors = tuple(sorted(diagnostics[key]))
        if roots:
            state = RetentionState.REFERENCED
        elif errors:
            state = RetentionState.UNKNOWN
        else:
            state = RetentionState.UNREFERENCED
        result[key] = ArtifactRetention(state, roots, errors)
    return result


def _record_unknown(diagnostics: dict[ArtifactKey, set[str]], message: str) -> None:
    for errors in diagnostics.values():
        errors.add(message)


def retention_for(
    inspection: RepositoryResourceInspection,
    reachability: dict[ArtifactKey, ArtifactRetention],
) -> ArtifactRetention:
    if inspection.resource_type != "git_ref":
        return ArtifactRetention(RetentionState.NOT_APPLICABLE)
    return reachability.get(
        (inspection.registry_entry_id, inspection.expected_target),
        ArtifactRetention(
            RetentionState.UNKNOWN,
            diagnostics=("snapshot is missing from the reachability index",),
        ),
    )
