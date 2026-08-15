"""Append-only human feedback for Spotter supervision events."""

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from fcntl import LOCK_EX, flock
from getpass import getuser
from pathlib import Path
from uuid import uuid4

from spotter.paths import secure_dir, spotter_home
from spotter.redact import redact_text

FEEDBACK_SCHEMA = "spotter.intervention_feedback"
FEEDBACK_SCHEMA_VERSION = 1
SCHEMA_VERSION = FEEDBACK_SCHEMA_VERSION


class FeedbackCategory(StrEnum):
    USEFUL = "USEFUL"
    WRONG = "WRONG"
    UNNECESSARY = "UNNECESSARY"
    TOO_LATE = "TOO_LATE"
    TOO_EARLY = "TOO_EARLY"
    TOO_DISRUPTIVE = "TOO_DISRUPTIVE"
    CORRECT_BUT_POORLY_WORDED = "CORRECT_BUT_POORLY_WORDED"
    BLOCK_SHOULD_HAVE_ALLOWED = "BLOCK_SHOULD_HAVE_ALLOWED"
    BLOCK_CORRECT = "BLOCK_CORRECT"
    STALE_OR_DUPLICATE = "STALE_OR_DUPLICATE"
    OTHER = "OTHER"


class FeedbackError(ValueError):
    """Feedback input or durable history is invalid."""


@dataclass(frozen=True)
class HumanFeedback:
    feedback_id: str
    supervision_event_id: str
    category: str
    created_at: str
    note: str = ""
    rater: str = ""
    version: int = SCHEMA_VERSION


def feedback_path(*, create: bool = False) -> Path:
    base = spotter_home() / "feedback"
    return (secure_dir(base) if create else base) / "interventions.jsonl"


def add_feedback(
    supervision_event_id: str,
    category: str,
    *,
    note: str = "",
    rater: str | None = None,
) -> HumanFeedback:
    event_id = supervision_event_id.strip()
    if not event_id or len(event_id) > 200:
        raise FeedbackError("intervention id must be non-empty and at most 200 characters")
    try:
        normalized_category = FeedbackCategory(category.upper()).value
    except ValueError as error:
        allowed = ", ".join(item.value for item in FeedbackCategory)
        raise FeedbackError(f"category must be one of: {allowed}") from error
    rater_id = getuser() if rater is None else rater.strip()
    if not rater_id or len(rater_id) > 200:
        raise FeedbackError("rater must be a non-empty identity of at most 200 characters")
    redacted_note, _ = redact_text(note.strip())
    feedback = HumanFeedback(
        feedback_id=f"feedback-{uuid4().hex}",
        supervision_event_id=event_id,
        category=normalized_category,
        created_at=datetime.now(UTC).isoformat(),
        note=redacted_note[:2000],
        rater=rater_id,
    )
    path = feedback_path(create=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w") as lock:
        flock(lock, LOCK_EX)
        if path.exists():
            _load_feedback_path(path)
        if not path.exists():
            path.touch(mode=0o600)
        with path.open("a", encoding="utf-8") as sink:
            sink.write(
                json.dumps(
                    {
                        "schema": FEEDBACK_SCHEMA,
                        "schema_version": FEEDBACK_SCHEMA_VERSION,
                        **asdict(feedback),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            sink.flush()
            os.fsync(sink.fileno())
    return feedback


def load_feedback(supervision_event_id: str | None = None) -> tuple[HumanFeedback, ...]:
    path = feedback_path()
    feedback = _load_feedback_path(path)
    if supervision_event_id is None:
        return feedback
    return tuple(item for item in feedback if item.supervision_event_id == supervision_event_id)


def _load_feedback_path(path: Path) -> tuple[HumanFeedback, ...]:
    if not path.exists():
        return ()
    feedback: list[HumanFeedback] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise TypeError("record is not an object")
            version = raw["version"]
            if not isinstance(version, int) or isinstance(version, bool):
                raise FeedbackError(f"{path.name} line {number} has a non-integer version")
            schema = raw.get("schema")
            schema_version = raw.get("schema_version")
            if schema is None and schema_version is None:
                pass
            elif schema != FEEDBACK_SCHEMA:
                raise FeedbackError(f"{path.name} line {number} uses unsupported schema {schema!r}")
            elif not isinstance(schema_version, int) or isinstance(schema_version, bool):
                raise FeedbackError(f"{path.name} line {number} has a non-integer schema version")
            elif schema_version != version:
                raise FeedbackError(f"{path.name} line {number} has mismatched schema versions")
            if version > SCHEMA_VERSION:
                raise FeedbackError(
                    f"{path.name} line {number} uses schema v{version}; "
                    f"this build understands up to v{SCHEMA_VERSION}"
                )
            if version < 1:
                raise FeedbackError(f"{path.name} line {number} uses unsupported schema v{version}")
            item = HumanFeedback(
                feedback_id=str(raw["feedback_id"]),
                supervision_event_id=str(raw["supervision_event_id"]),
                category=FeedbackCategory(str(raw["category"])).value,
                created_at=str(raw["created_at"]),
                note=str(raw.get("note") or ""),
                rater=str(raw.get("rater") or ""),
                version=version,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, FeedbackError):
                raise
            raise FeedbackError(f"{path.name} line {number} is unreadable ({error})") from error
        feedback.append(item)
    return tuple(feedback)
