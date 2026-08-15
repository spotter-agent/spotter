"""Durable ownership records for Git resources created by Spotter."""

import json
import os
import subprocess
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from fcntl import LOCK_EX, flock
from pathlib import Path

from spotter.build_identity import current_build_identity
from spotter.paths import RuntimeLayout, secure_dir

REPOSITORY_REGISTRY_SCHEMA = "spotter.repository_registry"
REPOSITORY_REGISTRY_SCHEMA_VERSION = 1


class RepositoryRegistryError(RuntimeError):
    """The repository registry is incompatible, corrupt, or cannot be updated."""


class OwnershipConfidence(StrEnum):
    SAFE_OWNED = "SAFE_OWNED"
    INACCESSIBLE = "INACCESSIBLE"
    AMBIGUOUS = "AMBIGUOUS"


class ResourcePresence(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OwnedRepositoryResource:
    resource_type: str
    resource_id: str
    owner: str
    generation_id: str
    created_by_spotter_version: str
    created_at: str
    expected_target: str
    expected_git_dir: str | None = None


@dataclass(frozen=True)
class RepositoryEntry:
    registry_entry_id: str
    last_known_path: str
    git_common_dir: str
    repository_device: int
    repository_inode: int
    last_seen_at: str
    resources: tuple[OwnedRepositoryResource, ...]

    @property
    def repository_identity(self) -> str:
        return f"git-common-dir:{self.repository_device}:{self.repository_inode}"


@dataclass(frozen=True)
class RepositoryResourceInspection:
    registry_entry_id: str
    repository_path: str
    resource_type: str
    resource_id: str
    expected_target: str
    confidence: OwnershipConfidence
    presence: ResourcePresence
    reason: str


@dataclass(frozen=True)
class _RepositoryIdentity:
    path: Path
    git_common_dir: Path
    device: int
    inode: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _git(repo: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise RepositoryRegistryError(
            f"git {' '.join(arguments)} failed to start: {error}"
        ) from error
    if result.returncode != 0:
        raise RepositoryRegistryError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _repository_identity(repo: Path) -> _RepositoryIdentity:
    root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    common_output = Path(_git(repo, "rev-parse", "--git-common-dir"))
    common_dir = (
        common_output.resolve() if common_output.is_absolute() else (repo / common_output).resolve()
    )
    try:
        status = common_dir.stat()
    except OSError as error:
        raise RepositoryRegistryError(
            f"could not inspect Git common directory {common_dir}: {error}"
        ) from error
    return _RepositoryIdentity(root, common_dir, status.st_dev, status.st_ino)


def _string(raw: dict[str, object], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise RepositoryRegistryError(f"{context}.{key} must be a non-empty string")
    return value


def _integer(raw: dict[str, object], key: str, context: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RepositoryRegistryError(f"{context}.{key} must be a non-negative integer")
    return value


def _resource(raw: object, context: str) -> OwnedRepositoryResource:
    if not isinstance(raw, dict):
        raise RepositoryRegistryError(f"{context} must be an object")
    expected_git_dir = raw.get("expected_git_dir")
    if expected_git_dir is not None and not isinstance(expected_git_dir, str):
        raise RepositoryRegistryError(f"{context}.expected_git_dir must be a string or null")
    resource_type = _string(raw, "resource_type", context)
    if resource_type not in {"git_ref", "worktree"}:
        raise RepositoryRegistryError(f"{context}.resource_type is unsupported: {resource_type!r}")
    owner = _string(raw, "owner", context)
    if owner != "spotter":
        raise RepositoryRegistryError(f"{context}.owner is unsupported: {owner!r}")
    return OwnedRepositoryResource(
        resource_type=resource_type,
        resource_id=_string(raw, "resource_id", context),
        owner=owner,
        generation_id=_string(raw, "generation_id", context),
        created_by_spotter_version=_string(raw, "created_by_spotter_version", context),
        created_at=_string(raw, "created_at", context),
        expected_target=_string(raw, "expected_target", context),
        expected_git_dir=expected_git_dir,
    )


def _entry(raw: object, index: int) -> RepositoryEntry:
    context = f"repository registry entry {index}"
    if not isinstance(raw, dict):
        raise RepositoryRegistryError(f"{context} must be an object")
    resources = raw.get("resources")
    if not isinstance(resources, list):
        raise RepositoryRegistryError(f"{context}.resources must be a list")
    parsed = tuple(
        _resource(resource, f"{context}.resources[{resource_index}]")
        for resource_index, resource in enumerate(resources)
    )
    identities = [(resource.resource_type, resource.resource_id) for resource in parsed]
    if len(identities) != len(set(identities)):
        raise RepositoryRegistryError(f"{context} contains duplicate resource identities")
    return RepositoryEntry(
        registry_entry_id=_string(raw, "registry_entry_id", context),
        last_known_path=_string(raw, "last_known_path", context),
        git_common_dir=_string(raw, "git_common_dir", context),
        repository_device=_integer(raw, "repository_device", context),
        repository_inode=_integer(raw, "repository_inode", context),
        last_seen_at=_string(raw, "last_seen_at", context),
        resources=parsed,
    )


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    secure_dir(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
            json.dump(payload, sink, indent=2, sort_keys=True)
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


class RepositoryRegistry:
    """Record exact repository resources without using their names as ownership proof."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or RuntimeLayout.discover().repository_registry

    def load(self) -> tuple[RepositoryEntry, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RepositoryRegistryError(f"repository registry is unreadable: {error}") from error
        if not isinstance(raw, dict):
            raise RepositoryRegistryError("repository registry must be a JSON object")
        if raw.get("schema") != REPOSITORY_REGISTRY_SCHEMA:
            raise RepositoryRegistryError(
                f"unsupported repository registry schema {raw.get('schema')!r}"
            )
        version = raw.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise RepositoryRegistryError("repository registry schema_version must be an integer")
        if version != REPOSITORY_REGISTRY_SCHEMA_VERSION:
            direction = "newer" if version > REPOSITORY_REGISTRY_SCHEMA_VERSION else "unsupported"
            raise RepositoryRegistryError(
                f"{direction} repository registry schema v{version}; this build understands "
                f"v{REPOSITORY_REGISTRY_SCHEMA_VERSION}"
            )
        repositories = raw.get("repositories")
        if not isinstance(repositories, list):
            raise RepositoryRegistryError("repository registry repositories must be a list")
        entries = tuple(_entry(entry, index) for index, entry in enumerate(repositories))
        entry_ids = [entry.registry_entry_id for entry in entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise RepositoryRegistryError("repository registry contains duplicate entry ids")
        return entries

    def find_repository(self, repo: Path) -> RepositoryEntry | None:
        """Return the entry matching the repository's stable Git identity."""
        identity = _repository_identity(repo)
        matches = tuple(
            entry
            for entry in self.load()
            if (entry.repository_device, entry.repository_inode)
            == (identity.device, identity.inode)
        )
        if len(matches) > 1:
            raise RepositoryRegistryError("repository registry contains duplicate Git identities")
        return matches[0] if matches else None

    def inspect(
        self, entries: tuple[RepositoryEntry, ...] | None = None
    ) -> tuple[RepositoryResourceInspection, ...]:
        """Re-check recorded resources without mutating Git or registry state."""
        inspections: list[RepositoryResourceInspection] = []
        for entry in entries if entries is not None else self.load():
            repository = Path(entry.last_known_path)
            if not repository.exists():
                inspections.extend(
                    self._entry_inspections(
                        entry,
                        OwnershipConfidence.INACCESSIBLE,
                        ResourcePresence.UNKNOWN,
                        "last known repository path is unavailable",
                    )
                )
                continue
            try:
                identity = _repository_identity(repository)
            except RepositoryRegistryError as error:
                inspections.extend(
                    self._entry_inspections(
                        entry,
                        OwnershipConfidence.AMBIGUOUS,
                        ResourcePresence.UNKNOWN,
                        f"last known path is not the recorded Git repository: {error}",
                    )
                )
                continue
            if (identity.device, identity.inode) != (
                entry.repository_device,
                entry.repository_inode,
            ):
                inspections.extend(
                    self._entry_inspections(
                        entry,
                        OwnershipConfidence.AMBIGUOUS,
                        ResourcePresence.UNKNOWN,
                        "last known path now identifies a different Git repository",
                    )
                )
                continue
            inspections.extend(self._inspect_resources(repository, entry))
        return tuple(inspections)

    @staticmethod
    def _entry_inspections(
        entry: RepositoryEntry,
        confidence: OwnershipConfidence,
        presence: ResourcePresence,
        reason: str,
    ) -> tuple[RepositoryResourceInspection, ...]:
        return tuple(
            RepositoryResourceInspection(
                registry_entry_id=entry.registry_entry_id,
                repository_path=entry.last_known_path,
                resource_type=resource.resource_type,
                resource_id=resource.resource_id,
                expected_target=resource.expected_target,
                confidence=confidence,
                presence=presence,
                reason=reason,
            )
            for resource in entry.resources
        )

    def _inspect_resources(
        self, repository: Path, entry: RepositoryEntry
    ) -> tuple[RepositoryResourceInspection, ...]:
        inspections: list[RepositoryResourceInspection] = []
        for resource in entry.resources:
            if resource.resource_type == "git_ref":
                confidence, presence, reason = self._inspect_ref(repository, resource)
            else:
                confidence, presence, reason = self._inspect_worktree(repository, resource)
            inspections.append(
                RepositoryResourceInspection(
                    registry_entry_id=entry.registry_entry_id,
                    repository_path=entry.last_known_path,
                    resource_type=resource.resource_type,
                    resource_id=resource.resource_id,
                    expected_target=resource.expected_target,
                    confidence=confidence,
                    presence=presence,
                    reason=reason,
                )
            )
        return tuple(inspections)

    @staticmethod
    def _inspect_ref(
        repository: Path, resource: OwnedRepositoryResource
    ) -> tuple[OwnershipConfidence, ResourcePresence, str]:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", resource.resource_id],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 1:
            return (
                OwnershipConfidence.SAFE_OWNED,
                ResourcePresence.ABSENT,
                "recorded ref is already absent",
            )
        if result.returncode != 0:
            return (
                OwnershipConfidence.AMBIGUOUS,
                ResourcePresence.UNKNOWN,
                f"could not inspect ref: {result.stderr.strip()}",
            )
        actual = result.stdout.strip()
        if actual != resource.expected_target:
            return (
                OwnershipConfidence.AMBIGUOUS,
                ResourcePresence.PRESENT,
                f"ref target changed to {actual}",
            )
        return OwnershipConfidence.SAFE_OWNED, ResourcePresence.PRESENT, "exact ref target matches"

    @staticmethod
    def _inspect_worktree(
        repository: Path, resource: OwnedRepositoryResource
    ) -> tuple[OwnershipConfidence, ResourcePresence, str]:
        worktree = Path(resource.resource_id)
        expected_git_dir = (
            Path(resource.expected_git_dir) if resource.expected_git_dir is not None else None
        )
        if not worktree.exists():
            listed = subprocess.run(
                ["git", "worktree", "list", "--porcelain", "-z"],
                cwd=repository,
                capture_output=True,
                text=True,
                check=False,
            )
            if listed.returncode != 0:
                return (
                    OwnershipConfidence.AMBIGUOUS,
                    ResourcePresence.UNKNOWN,
                    f"could not inspect Git worktree metadata: {listed.stderr.strip()}",
                )
            registered = f"worktree {resource.resource_id}" in listed.stdout.split("\0")
            metadata_exists = expected_git_dir is not None and expected_git_dir.exists()
            if registered and metadata_exists:
                return (
                    OwnershipConfidence.SAFE_OWNED,
                    ResourcePresence.PRESENT,
                    "Git worktree metadata remains but the worktree path is absent",
                )
            if registered or metadata_exists:
                return (
                    OwnershipConfidence.AMBIGUOUS,
                    ResourcePresence.UNKNOWN,
                    "worktree path and Git administrative metadata disagree",
                )
            return (
                OwnershipConfidence.SAFE_OWNED,
                ResourcePresence.ABSENT,
                "recorded worktree and Git metadata are already absent",
            )
        try:
            repository_identity = _repository_identity(repository)
            worktree_identity = _repository_identity(worktree)
            actual_git_dir = Path(_git(worktree, "rev-parse", "--absolute-git-dir")).resolve()
        except RepositoryRegistryError as error:
            return (
                OwnershipConfidence.AMBIGUOUS,
                ResourcePresence.PRESENT,
                f"worktree path no longer has the recorded Git identity: {error}",
            )
        if (repository_identity.device, repository_identity.inode) != (
            worktree_identity.device,
            worktree_identity.inode,
        ) or expected_git_dir != actual_git_dir:
            return (
                OwnershipConfidence.AMBIGUOUS,
                ResourcePresence.PRESENT,
                "worktree repository or Git administrative path changed",
            )
        return (
            OwnershipConfidence.SAFE_OWNED,
            ResourcePresence.PRESENT,
            "exact worktree Git identity matches",
        )

    def record_ref(self, repo: Path, ref: str, target: str) -> RepositoryEntry:
        if not ref.startswith("refs/spotter/"):
            raise RepositoryRegistryError(f"ref is outside the Spotter namespace: {ref}")
        actual_target = _git(repo, "rev-parse", "--verify", ref)
        if actual_target != target:
            raise RepositoryRegistryError(
                f"ref {ref} resolves to {actual_target}, not the expected target {target}"
            )
        return self._record_safely(repo, "git_ref", ref, target)

    def record_worktree(self, repo: Path, worktree: Path, target: str) -> RepositoryEntry:
        repository_identity = _repository_identity(repo)
        worktree_identity = _repository_identity(worktree)
        if (repository_identity.device, repository_identity.inode) != (
            worktree_identity.device,
            worktree_identity.inode,
        ):
            raise RepositoryRegistryError(
                f"worktree {worktree} does not belong to repository {repo}"
            )
        worktree_path = Path(_git(worktree, "rev-parse", "--show-toplevel")).resolve()
        git_dir = Path(_git(worktree, "rev-parse", "--absolute-git-dir")).resolve()
        return self._record_safely(
            repo,
            "worktree",
            str(worktree_path),
            target,
            expected_git_dir=str(git_dir),
        )

    def _record_safely(
        self,
        repo: Path,
        resource_type: str,
        resource_id: str,
        target: str,
        *,
        expected_git_dir: str | None = None,
    ) -> RepositoryEntry:
        try:
            return self._record(
                repo,
                resource_type,
                resource_id,
                target,
                expected_git_dir=expected_git_dir,
            )
        except RepositoryRegistryError:
            raise
        except OSError as error:
            raise RepositoryRegistryError(
                f"could not update repository registry: {error}"
            ) from error

    def _record(
        self,
        repo: Path,
        resource_type: str,
        resource_id: str,
        target: str,
        *,
        expected_git_dir: str | None = None,
    ) -> RepositoryEntry:
        identity = _repository_identity(repo)
        secure_dir(self.path.parent)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a") as lock:
            flock(lock, LOCK_EX)
            entries = list(self.load())
            match = next(
                (
                    entry
                    for entry in entries
                    if entry.repository_device == identity.device
                    and entry.repository_inode == identity.inode
                ),
                None,
            )
            now = _now()
            if match is None:
                match = RepositoryEntry(
                    registry_entry_id=str(uuid.uuid4()),
                    last_known_path=str(identity.path),
                    git_common_dir=str(identity.git_common_dir),
                    repository_device=identity.device,
                    repository_inode=identity.inode,
                    last_seen_at=now,
                    resources=(),
                )
                entries.append(match)

            existing = next(
                (
                    resource
                    for resource in match.resources
                    if resource.resource_type == resource_type
                    and resource.resource_id == resource_id
                ),
                None,
            )
            if existing is not None and (
                existing.expected_target != target or existing.expected_git_dir != expected_git_dir
            ):
                raise RepositoryRegistryError(
                    f"owned {resource_type} {resource_id} no longer matches its recorded target"
                )
            resource = existing or OwnedRepositoryResource(
                resource_type=resource_type,
                resource_id=resource_id,
                owner="spotter",
                generation_id=str(uuid.uuid4()),
                created_by_spotter_version=current_build_identity().version,
                created_at=now,
                expected_target=target,
                expected_git_dir=expected_git_dir,
            )
            updated = replace(
                match,
                last_known_path=str(identity.path),
                git_common_dir=str(identity.git_common_dir),
                last_seen_at=now,
                resources=(
                    match.resources if existing is not None else (*match.resources, resource)
                ),
            )
            entries[entries.index(match)] = updated
            _atomic_write(
                self.path,
                {
                    "schema": REPOSITORY_REGISTRY_SCHEMA,
                    "schema_version": REPOSITORY_REGISTRY_SCHEMA_VERSION,
                    "repositories": [
                        {**asdict(entry), "resources": [asdict(item) for item in entry.resources]}
                        for entry in entries
                    ],
                },
            )
            return updated
