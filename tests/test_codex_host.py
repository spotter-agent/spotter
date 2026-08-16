import pytest

from spotter.codex_host import (
    CodexHostVersion,
    CodexHostVersionError,
    validate_codex_host_version,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("codex-cli 0.147.0", CodexHostVersion(0, 147, 0)),
        ("codex_cli_rs/1.2.3", CodexHostVersion(1, 2, 3)),
        ("codex 1.0", CodexHostVersion(1, 0, 0)),
        ("codex-cli 0.147.0+homebrew", CodexHostVersion(0, 147, 0)),
    ],
)
def test_supported_codex_host_versions(raw: str, expected: CodexHostVersion) -> None:
    assert validate_codex_host_version(raw) == expected


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("codex-cli 0.146.9", "too old"),
        ("codex-cli 0.148.0-beta.1", "prerelease"),
        ("codex development", "malformed"),
        ("0.147.0", "malformed"),
        (None, "missing"),
    ],
)
def test_unsupported_codex_host_versions(raw: object, message: str) -> None:
    with pytest.raises(CodexHostVersionError, match=message):
        validate_codex_host_version(raw)
