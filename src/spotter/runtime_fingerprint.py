"""Non-secret identity for inputs that construct a long-lived runtime."""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from spotter.config import SpotterConfig, resolve_config
from spotter.paths import RuntimeLayout
from spotter.protocol import (
    CONTROL_PROTOCOL_VERSION,
    MAX_CONTROL_PROTOCOL_VERSION,
    MIN_CONTROL_PROTOCOL_VERSION,
)

_FINGERPRINT_SCHEMA = 1


def load_runtime_manifest(layout: RuntimeLayout) -> dict[str, Any]:
    """Load only the manifest shape needed by runtime construction."""
    try:
        raw = json.loads(layout.integration_manifest.read_bytes())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def configured_runtime_config(layout: RuntimeLayout) -> SpotterConfig:
    """Resolve the daemon's effective construction config, including fallback."""
    raw = load_runtime_manifest(layout)
    configured = raw.get("config_path")
    config_path = Path(configured) if isinstance(configured, str) and configured else None
    try:
        return resolve_config(layout=layout, explicit_path=config_path).config
    except (OSError, ValueError):
        return SpotterConfig.from_mapping({"main_agent": {"adapter": "codex"}})


def runtime_construction_fingerprint(
    layout: RuntimeLayout,
    config: SpotterConfig,
    manifest: Mapping[str, Any] | None = None,
    *,
    control_socket: Path | None = None,
) -> str:
    """Hash stable runtime-construction inputs without returning their values."""
    source = manifest if manifest is not None else load_runtime_manifest(layout)
    recorded_layout = source.get("runtime_layout")
    daemon_executable = (
        _string(recorded_layout.get("daemon_executable"))
        if isinstance(recorded_layout, Mapping)
        else None
    )
    payload = {
        "schema": _FINGERPRINT_SCHEMA,
        "control": {
            "socket": str(control_socket or layout.control_socket),
            "protocol": CONTROL_PROTOCOL_VERSION,
            "min_protocol": MIN_CONTROL_PROTOCOL_VERSION,
            "max_protocol": MAX_CONTROL_PROTOCOL_VERSION,
        },
        # Use setup's stable executable identity. RuntimeLayout discovery can
        # legitimately spell the same source invocation as either `spotterd`
        # or `python -m spotter.daemon` on opposite sides of process startup.
        "daemon_executable": daemon_executable,
        "adapter": config.main_agent.adapter,
        "integration": {
            "generation": _string(source.get("integration_generation")),
            "runtime_mode": _string(source.get("runtime_mode")),
            "config_path": _string(source.get("config_path")),
            "app_server_strategy": _string(source.get("app_server_strategy")),
            "app_server_endpoint": _string(source.get("app_server_endpoint")),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "runtime-" + hashlib.sha256(encoded).hexdigest()[:24]


def expected_runtime_construction_fingerprint(
    layout: RuntimeLayout,
    *,
    control_socket: Path | None = None,
) -> str:
    """Return the fingerprint a daemon started from the current installation will use."""
    manifest = load_runtime_manifest(layout)
    return runtime_construction_fingerprint(
        layout,
        configured_runtime_config(layout),
        manifest,
        control_socket=control_socket,
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
