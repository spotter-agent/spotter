"""Small operator-owned activation presets; other configuration remains untouched."""

import os
import tomllib
from fcntl import LOCK_EX, flock
from pathlib import Path

from spotter.config import (
    CONFIG_SCHEMA,
    CONFIG_SCHEMA_VERSION,
    ConfigurationError,
    supervision_mode_settings,
)
from spotter.paths import RuntimeLayout, atomic_write_bytes, secure_dir

MODES = ("observe", "protect", "advisory", "custom")


def selected_mode(path: Path) -> str:
    if path.is_symlink():
        raise ConfigurationError(f"refusing symlink mode file: {path}")
    if not path.exists():
        return "custom"
    raw = tomllib.loads(path.read_text())
    for mode in MODES[:-1]:
        if raw == supervision_mode_settings(mode):
            return mode
    raise ConfigurationError(f"unrecognized or edited mode file preserved: {path}")


def save_mode(mode: str, *, layout: RuntimeLayout | None = None) -> Path:
    if mode not in MODES:
        raise ConfigurationError(f"unknown mode: {mode}")
    root = (layout or RuntimeLayout.discover()).user_config_dir
    secure_dir(root)
    path = root / "mode.toml"
    descriptor = os.open(root / "mode.toml.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        flock(lock, LOCK_EX)
        if selected_mode(path) == mode:
            return path
        if mode == "custom":
            path.unlink()
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        else:
            raw = supervision_mode_settings(mode)
            reviewer = raw["reviewer"]
            content = (
                "# Managed by `spotter mode`; use `spotter mode custom` to remove.\n"
                f'config_schema = "{CONFIG_SCHEMA}"\n'
                f"config_schema_version = {CONFIG_SCHEMA_VERSION}\n"
                f"observation_only = {str(raw['observation_only']).lower()}\n\n[reviewer]\n"
                + "".join(f"{key} = {str(value).lower()}\n" for key, value in reviewer.items())
            )
            atomic_write_bytes(path, content.encode())
    return path
