"""Dependency-free bootstrap compatibility for Codex host versions."""

import re
from dataclasses import dataclass

_CODEX_VERSION = re.compile(
    r"^(?:(?:codex|codex-cli|codex_cli_rs)[ /]|Codex Desktop/)"
    r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)"
    r"(?:\.(?P<patch>0|[1-9]\d*))?"
    r"(?P<suffix>[-+][0-9A-Za-z.-]+)?$"
)

# The App Server reports a user-agent whose leading token is the *caller's*
# originator, not a fixed product name: connecting as `spotter` yields
# `spotter/0.149.1 (…) kitty/0.42.0 (spotter; 0.1.0)`. Pinning the prefix to
# `Codex Desktop` made Spotter reject the identity its own handshake produced.
# The shape stays strict — version, platform, terminal, originator — because
# that is what distinguishes this from arbitrary text; only the name is free.
_CODEX_USER_AGENT_VERSION = re.compile(
    r"^[A-Za-z0-9._-]+(?: [A-Za-z0-9._-]+)*/"
    r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)"
    r"(?:\.(?P<patch>0|[1-9]\d*))?"
    r"(?P<suffix>[-+][0-9A-Za-z.-]+)?"
    r" \([^()\r\n]+\) [^()\s]+ \([^()\r\n]+\)$"
)


class CodexHostVersionError(ValueError):
    """The selected Codex host cannot satisfy Spotter's bootstrap contract."""


@dataclass(frozen=True, order=True)
class CodexHostVersion:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


MIN_CODEX_HOST_VERSION = CodexHostVersion(0, 147, 0)


def parse_codex_host_version(raw: object) -> CodexHostVersion:
    """Parse CLI and App Server identities without guessing arbitrary text."""
    if not isinstance(raw, str) or not raw.strip():
        raise CodexHostVersionError("Codex host version is missing")
    value = raw.strip()
    match = _CODEX_VERSION.fullmatch(value) or _CODEX_USER_AGENT_VERSION.fullmatch(value)
    if match is None:
        raise CodexHostVersionError(f"malformed Codex host version {raw!r}")
    suffix = match.group("suffix")
    if suffix is not None and suffix.startswith("-"):
        raise CodexHostVersionError(f"prerelease Codex host version is unsupported: {raw}")
    return CodexHostVersion(
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch") or 0),
    )


def validate_codex_host_version(raw: object) -> CodexHostVersion:
    """Enforce only the evidence-backed bootstrap floor; features use capabilities."""
    version = parse_codex_host_version(raw)
    if version < MIN_CODEX_HOST_VERSION:
        raise CodexHostVersionError(
            f"Codex {version} is too old; {MIN_CODEX_HOST_VERSION} or newer is required"
        )
    return version
