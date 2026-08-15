"""Repository ownership registry foundation for lifecycle cleanup (#89)."""

import json
import subprocess
from pathlib import Path

import pytest

from spotter.repository_registry import (
    REPOSITORY_REGISTRY_SCHEMA,
    RepositoryRegistry,
    RepositoryRegistryError,
)
from spotter.snapshot import SnapshotError, restore_snapshot, snapshot_worktree


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(command, cwd=repository, check=True)
    (repository / "tracked.txt").write_text("one")
    return repository


@pytest.fixture(autouse=True)
def spotter_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "spotter"
    monkeypatch.setenv("SPOTTER_HOME", str(home))
    return home


def test_snapshot_and_worktree_record_exact_owned_resources(
    repo: Path, spotter_home: Path, tmp_path: Path
) -> None:
    sha = snapshot_worktree(repo)
    worktree = restore_snapshot(repo, sha, tmp_path / "restored")

    registry = RepositoryRegistry(spotter_home / "repos.json")
    [entry] = registry.load()
    resources = {
        (resource.resource_type, resource.resource_id): resource for resource in entry.resources
    }

    ref = resources[("git_ref", f"refs/spotter/steps/{sha}")]
    restored = resources[("worktree", str(worktree.resolve()))]
    assert ref.owner == restored.owner == "spotter"
    assert ref.expected_target == restored.expected_target == sha
    assert restored.expected_git_dir is not None
    assert entry.last_known_path == str(repo.resolve())
    assert entry.repository_identity.startswith("git-common-dir:")
    raw = json.loads((spotter_home / "repos.json").read_text())
    assert raw["schema"] == REPOSITORY_REGISTRY_SCHEMA
    assert raw["schema_version"] == 1


def test_moved_repository_keeps_registry_identity(
    repo: Path, spotter_home: Path, tmp_path: Path
) -> None:
    first = snapshot_worktree(repo)
    moved = tmp_path / "moved"
    repo.rename(moved)
    (moved / "tracked.txt").write_text("two")
    second = snapshot_worktree(moved, first)

    [entry] = RepositoryRegistry(spotter_home / "repos.json").load()
    assert entry.last_known_path == str(moved.resolve())
    assert {resource.expected_target for resource in entry.resources} == {first, second}


def test_recreated_path_does_not_adopt_prior_repository(
    repo: Path, spotter_home: Path, tmp_path: Path
) -> None:
    snapshot_worktree(repo)
    moved = tmp_path / "original"
    repo.rename(moved)
    repo.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    (repo / "replacement.txt").write_text("replacement")

    snapshot_worktree(repo)

    entries = RepositoryRegistry(spotter_home / "repos.json").load()
    assert len(entries) == 2
    assert len({entry.repository_identity for entry in entries}) == 2


def test_future_registry_refuses_snapshot_and_rolls_back_ref(
    repo: Path, spotter_home: Path
) -> None:
    registry = spotter_home / "repos.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "schema": REPOSITORY_REGISTRY_SCHEMA,
                "schema_version": 2,
                "repositories": [],
            }
        )
    )

    with pytest.raises(SnapshotError, match="newer repository registry schema"):
        snapshot_worktree(repo)

    refs = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/spotter/"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert refs.stdout == ""


def test_future_registry_refuses_mutation_without_rewrite(repo: Path, spotter_home: Path) -> None:
    registry_path = spotter_home / "repos.json"
    registry_path.parent.mkdir(parents=True)
    content = json.dumps(
        {"schema": REPOSITORY_REGISTRY_SCHEMA, "schema_version": 9, "repositories": []}
    )
    registry_path.write_text(content)

    with pytest.raises(RepositoryRegistryError, match="newer repository registry schema"):
        RepositoryRegistry(registry_path).load()

    assert registry_path.read_text() == content


def test_future_registry_rolls_back_new_worktree(
    repo: Path, spotter_home: Path, tmp_path: Path
) -> None:
    sha = snapshot_worktree(repo)
    registry_path = spotter_home / "repos.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema": REPOSITORY_REGISTRY_SCHEMA,
                "schema_version": 2,
                "repositories": [],
            }
        )
    )
    destination = tmp_path / "refused-worktree"

    with pytest.raises(SnapshotError, match="newer repository registry schema"):
        restore_snapshot(repo, sha, destination)

    assert not destination.exists()
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert str(destination) not in worktrees.stdout
