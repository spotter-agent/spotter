"""Exact ownership records for logs created by Spotter (#89)."""

import json
from pathlib import Path

import pytest

from spotter.log_registry import LogRegistry, LogRegistryError


@pytest.fixture()
def registry(tmp_path: Path) -> LogRegistry:
    logs = tmp_path / "logs"
    return LogRegistry(path=logs / "ownership.json", log_dir=logs)


def test_claim_creates_private_log_and_exact_ownership(registry: LogRegistry) -> None:
    path = registry.log_dir / "spotterd.log"

    assert registry.claim(path, "spotterd") is True

    assert path.read_bytes() == b""
    assert path.stat().st_mode & 0o777 == 0o600
    [owned] = registry.load()
    assert owned.resource_type == "log"
    assert owned.resource_id == "spotterd"
    assert owned.owner == "spotter"
    assert owned.expected_path == str(path)
    assert (owned.device, owned.inode) == (path.stat().st_dev, path.stat().st_ino)


def test_claim_is_idempotent_for_the_same_file(registry: LogRegistry) -> None:
    path = registry.log_dir / "spotterd.log"
    assert registry.claim(path, "spotterd") is True
    path.write_text("kept")
    before = registry.load()

    assert registry.claim(path, "spotterd") is True

    assert path.read_text() == "kept"
    assert registry.load() == before


def test_claim_does_not_adopt_preexisting_log(registry: LogRegistry) -> None:
    registry.log_dir.mkdir(parents=True)
    path = registry.log_dir / "spotterd.log"
    path.write_text("foreign")

    assert registry.claim(path, "spotterd") is False

    assert path.read_text() == "foreign"
    assert registry.load() == ()


def test_claim_does_not_adopt_replaced_log(registry: LogRegistry) -> None:
    path = registry.log_dir / "spotterd.log"
    assert registry.claim(path, "spotterd") is True
    [before] = registry.load()
    path.unlink()
    path.write_text("replacement")

    assert registry.claim(path, "spotterd") is False

    [after] = registry.load()
    assert after == before
    assert after.inode != path.stat().st_ino


def test_claim_recreates_a_missing_owned_log_with_new_identity(registry: LogRegistry) -> None:
    path = registry.log_dir / "spotterd.log"
    assert registry.claim(path, "spotterd") is True
    [before] = registry.load()
    path.unlink()

    assert registry.claim(path, "spotterd") is True

    [after] = registry.load()
    assert after.expected_path == before.expected_path
    assert (after.device, after.inode) == (path.stat().st_dev, path.stat().st_ino)


def test_claim_refuses_path_outside_log_directory(registry: LogRegistry, tmp_path: Path) -> None:
    with pytest.raises(LogRegistryError, match="outside the owned log directory"):
        registry.claim(tmp_path / "elsewhere.log", "elsewhere")

    with pytest.raises(LogRegistryError, match="outside the owned log directory"):
        registry.claim(registry.path, "registry")


def test_future_registry_is_read_only(registry: LogRegistry) -> None:
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text(
        json.dumps(
            {
                "schema": "spotter.log_registry",
                "schema_version": 99,
                "resources": [],
            }
        )
    )

    with pytest.raises(LogRegistryError, match="newer log registry schema"):
        registry.load()
    with pytest.raises(LogRegistryError, match="newer log registry schema"):
        registry.claim(registry.log_dir / "spotterd.log", "spotterd")


def test_registry_refuses_owned_path_outside_boundary(
    registry: LogRegistry, tmp_path: Path
) -> None:
    assert registry.claim(registry.log_dir / "spotterd.log", "spotterd") is True
    payload = json.loads(registry.path.read_text())
    payload["resources"][0]["expected_path"] = str(tmp_path / "foreign.log")
    registry.path.write_text(json.dumps(payload))

    with pytest.raises(LogRegistryError, match="outside its ownership boundary"):
        registry.load()
