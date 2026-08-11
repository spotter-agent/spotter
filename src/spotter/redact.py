"""Secret redaction at the write boundary (issue #39).

Journals are durable copies of material that previously lived only in a
shell's memory, and the reviewer digest forwards them to a model. Measured on
this machine's ordinary usage, 15 lines across 5 journals matched common
credential shapes before this existed.

Two rules shape the design:

- **Redact the value, keep the structure.** Gate rules judge a parsed token
  stream, so ``curl -H "Authorization: Bearer X"`` must still parse as a curl
  invocation after redaction, or supervision silently degrades while looking
  fine.
- **Redaction is a losing arms race and is documented as one.** It reduces
  exposure; it does not eliminate it. The durable fix is journaling less, and
  that trade is recorded in docs rather than assumed away.
"""

import re

PLACEHOLDER = "[REDACTED]"

# Ordered: more specific shapes first, so a generic assignment rule cannot
# swallow a token that a precise rule would have matched better.
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # Provider-issued tokens carry recognisable prefixes.
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), PLACEHOLDER),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), PLACEHOLDER),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), PLACEHOLDER),
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), PLACEHOLDER),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        PLACEHOLDER,
    ),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        PLACEHOLDER,
    ),
    # Header and assignment shapes: keep the key, drop the value.
    (
        "auth_header",
        re.compile(
            r"(?i)(authorization\s*:\s*)(bearer|basic|token)?\s*"
            r"(?!\[REDACTED\])[A-Za-z0-9._~+/=-]{8,}"
        ),
        r"\1\2 " + PLACEHOLDER,
    ),
    (
        "assignment",
        re.compile(
            r"(?i)\b((?:api[_-]?key|secret[_-]?\w*|access[_-]?token|auth[_-]?token"
            r"|password|passwd|token|client[_-]?secret)\s*[=:]\s*)"
            r"(?!\[REDACTED\])(\"[^\"]{4,}\"|'[^']{4,}'|[^\s;&|'\"]{4,})"
        ),
        r"\1" + PLACEHOLDER,
    ),
    (
        "env_export",
        re.compile(
            r"(?i)\b(export\s+[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*\s*=\s*)"
            r"(?!\[REDACTED\])(\"[^\"]+\"|'[^']+'|\S+)"
        ),
        r"\1" + PLACEHOLDER,
    ),
)


def redact_text(text: str) -> tuple[str, list[str]]:
    """Return the redacted text and the names of the rules that fired."""
    fired: list[str] = []
    for name, pattern, replacement in _PATTERNS:
        text, count = pattern.subn(replacement, text)
        if count:
            fired.append(name)
    return text, fired


def redact(value: object) -> tuple[object, list[str]]:
    """Redact recursively, preserving the shape of the payload.

    Structure is preserved because gates, fork correlation and analysis all
    read specific keys; a redactor that flattened payloads would break
    supervision in order to protect it.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out_map: dict[str, object] = {}
        fired: list[str] = []
        for key, item in value.items():
            cleaned, hits = redact(item)
            out_map[str(key)] = cleaned
            fired.extend(hits)
        return out_map, fired
    if isinstance(value, list):
        out_list: list[object] = []
        fired = []
        for item in value:
            cleaned, hits = redact(item)
            out_list.append(cleaned)
            fired.extend(hits)
        return out_list, fired
    return value, []


def scan_text(text: str) -> list[str]:
    """Rules that would fire, without producing the redacted text.

    Used to report that an existing journal contains credentials without
    printing them back to the terminal.
    """
    return [name for name, pattern, _ in _PATTERNS if pattern.search(text)]
