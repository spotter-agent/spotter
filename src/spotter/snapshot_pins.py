"""Durable manual roots for repository snapshot retention."""

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fcntl import LOCK_EX, flock
from pathlib import Path

from spotter.paths import RuntimeLayout, secure_dir

SNAPSHOT_PINS_SCHEMA = "spotter.snapshot_pins"
SNAPSHOT_PINS_SCHEMA_VERSION = 1


class SnapshotPinError(RuntimeError):
    """The manual snapshot-pin store is incompatible, corrupt, or unavailable."""


@dataclass(frozen=True)
class SnapshotPin:
    pin_id: str
    registry_entry_id: str
    snapshot_sha: str
    created_at: str


def _required_string(raw: dict[str, object], key: str, context: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise SnapshotPinError(f"{context}.{key} must be a non-empty string")
    return value


def _atomic_write(path: Path, pins: list[SnapshotPin]) -> None:
    secure_dir(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
            json.dump(
                {
                    "schema": SNAPSHOT_PINS_SCHEMA,
                    "schema_version": SNAPSHOT_PINS_SCHEMA_VERSION,
                    "pins": [asdict(pin) for pin in pins],
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


class SnapshotPinStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (RuntimeLayout.discover().user_data_dir / "snapshot-pins.json")

    def load(self) -> tuple[SnapshotPin, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SnapshotPinError(f"snapshot pin store is unreadable: {error}") from error
        if not isinstance(raw, dict):
            raise SnapshotPinError("snapshot pin store must be a JSON object")
        if raw.get("schema") != SNAPSHOT_PINS_SCHEMA:
            raise SnapshotPinError(f"unsupported snapshot pin schema {raw.get('schema')!r}")
        version = raw.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise SnapshotPinError("snapshot pin schema_version must be an integer")
        if version != SNAPSHOT_PINS_SCHEMA_VERSION:
            raise SnapshotPinError(
                f"unsupported snapshot pin schema v{version}; this build understands "
                f"v{SNAPSHOT_PINS_SCHEMA_VERSION}"
            )
        values = raw.get("pins")
        if not isinstance(values, list):
            raise SnapshotPinError("snapshot pin store pins must be a list")
        pins: list[SnapshotPin] = []
        for index, value in enumerate(values):
            context = f"snapshot pin {index}"
            if not isinstance(value, dict):
                raise SnapshotPinError(f"{context} must be an object")
            pins.append(
                SnapshotPin(
                    pin_id=_required_string(value, "pin_id", context),
                    registry_entry_id=_required_string(value, "registry_entry_id", context),
                    snapshot_sha=_required_string(value, "snapshot_sha", context),
                    created_at=_required_string(value, "created_at", context),
                )
            )
        identities = [pin.pin_id for pin in pins]
        roots = [(pin.registry_entry_id, pin.snapshot_sha) for pin in pins]
        if len(identities) != len(set(identities)):
            raise SnapshotPinError("snapshot pin store contains duplicate pin ids")
        if len(roots) != len(set(roots)):
            raise SnapshotPinError("snapshot pin store contains duplicate snapshot roots")
        return tuple(pins)

    def add(self, registry_entry_id: str, snapshot_sha: str) -> SnapshotPin:
        secure_dir(self.path.parent)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            flock(lock, LOCK_EX)
            pins = list(self.load())
            existing = next(
                (
                    pin
                    for pin in pins
                    if (pin.registry_entry_id, pin.snapshot_sha)
                    == (registry_entry_id, snapshot_sha)
                ),
                None,
            )
            if existing is not None:
                return existing
            pin = SnapshotPin(
                pin_id=str(uuid.uuid4()),
                registry_entry_id=registry_entry_id,
                snapshot_sha=snapshot_sha,
                created_at=datetime.now(UTC).isoformat(),
            )
            pins.append(pin)
            _atomic_write(self.path, pins)
            return pin

    def remove(self, pin_id: str) -> SnapshotPin:
        secure_dir(self.path.parent)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            flock(lock, LOCK_EX)
            pins = list(self.load())
            pin = next((item for item in pins if item.pin_id == pin_id), None)
            if pin is None:
                raise SnapshotPinError(f"snapshot pin {pin_id!r} does not exist")
            pins.remove(pin)
            _atomic_write(self.path, pins)
            return pin
