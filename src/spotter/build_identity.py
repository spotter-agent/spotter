"""Packaged release identity shared by the CLI, daemon, and Hook bridge."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Literal, cast

from spotter._version import __version__ as source_version
from spotter.protocol import (
    CONTROL_CAPABILITIES,
    CONTROL_PROTOCOL_VERSION,
    MAX_CONTROL_PROTOCOL_VERSION,
    MIN_CONTROL_PROTOCOL_VERSION,
)

PACKAGE_NAME = "spotter-agent"
RuntimeComponent = Literal["cli", "daemon", "hook_bridge"]


@dataclass(frozen=True)
class BuildIdentity:
    """Immutable package identity, distinct from protocol compatibility."""

    version: str
    build_id: str
    release_tag: str | None
    commit: str | None

    @property
    def is_release(self) -> bool:
        return self.release_tag is not None and self.commit is not None

    def peer_metadata(self, component: RuntimeComponent) -> dict[str, object]:
        metadata: dict[str, object] = {
            "component": component,
            "spotter_version": self.version,
            "build_id": self.build_id,
            "ipc_protocol_version": CONTROL_PROTOCOL_VERSION,
            "min_peer_protocol": MIN_CONTROL_PROTOCOL_VERSION,
            "max_peer_protocol": MAX_CONTROL_PROTOCOL_VERSION,
            "capabilities": list(CONTROL_CAPABILITIES),
        }
        if self.release_tag is not None:
            metadata["release_tag"] = self.release_tag
        if self.commit is not None:
            metadata["commit"] = self.commit
        return metadata


def _distribution_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return source_version


@lru_cache(maxsize=1)
def current_build_identity() -> BuildIdentity:
    """Return embedded release identity, or an explicit source-build identity."""
    package_version = _distribution_version()
    try:
        generated = import_module("spotter._generated_build")
    except ModuleNotFoundError as error:
        if error.name != "spotter._generated_build":
            raise
        return BuildIdentity(package_version, "source", None, None)

    embedded_version = cast(str, generated.VERSION)
    if embedded_version != package_version:
        # This should be impossible for artifacts produced by build_release.py.
        # Keep the mismatch visible instead of silently choosing one identity.
        return BuildIdentity(
            package_version,
            f"invalid:{generated.BUILD_ID}",
            cast(str, generated.RELEASE_TAG),
            cast(str, generated.COMMIT),
        )
    return BuildIdentity(
        package_version,
        cast(str, generated.BUILD_ID),
        cast(str, generated.RELEASE_TAG),
        cast(str, generated.COMMIT),
    )


def version_line(executable: str) -> str:
    identity = current_build_identity()
    return (
        f"{executable} {identity.version} "
        f"(build {identity.build_id}; ipc {CONTROL_PROTOCOL_VERSION})"
    )
