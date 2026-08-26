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
        (
            "Codex Desktop/0.147.0 (Mac OS 26.5.1; arm64) dumb (spotter; 0.1.0)",
            CodexHostVersion(0, 147, 0),
        ),
        # Exactly what a live 0.149.1 App Server returned: the leading token is
        # the originator Spotter itself sent, so pinning it to a product name
        # rejected our own connection.
        (
            "spotter/0.149.1 (Mac OS 26.5.1; arm64) kitty/0.42.0 (spotter; 0.1.0)",
            CodexHostVersion(0, 149, 1),
        ),
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
        ("Codex Desktop/0.147.0 arbitrary metadata", "malformed"),
        ("spotter/0.149.1 arbitrary metadata", "malformed"),
        # Freeing the name must not free the shape.
        ("spotter/0.149.1 (Mac OS) kitty/0.42.0", "malformed"),
        ("0.147.0", "malformed"),
        (None, "missing"),
    ],
)
def test_unsupported_codex_host_versions(raw: object, message: str) -> None:
    with pytest.raises(CodexHostVersionError, match=message):
        validate_codex_host_version(raw)
