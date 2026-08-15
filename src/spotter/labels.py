"""Human labels over recorded trajectories — the input to every plan decision.

Three gates in the plan wait on the same thing: was a judgment right?
- P1 observability ceiling: was a failure visible in the observable stream?
- P3 active gates: what fraction of shadow blocks were false positives?
- P4 injection: what fraction of reviewer nudges were correct?

Labels live in their own store, NOT in the session journal. Two reasons:
the journal is what the reviewer reads, so labels inside it would feed the
judge its own report card; and appending to the journal would shift step
numbering that forks and prior labels point at.

Each label pins a fingerprint of what was labeled. If the journal record
later differs, the label is reported as stale rather than silently counted
— a label that has drifted off its target is not evidence.
"""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from getpass import getuser
from pathlib import Path

from spotter.paths import sanitize_session, spotter_home
from spotter.snapshot import StepRecord

STEP_VERDICTS = ("tp", "fp", "unclear")
# "na" disposes of a session that had no failure to see — without it the
# ceiling denominator can never reach full coverage, and a metric whose
# coverage cannot be completed is a metric nobody will ever trust.
SESSION_VERDICTS = ("visible", "invisible", "unclear", "na")

# Only records metrics actually scores may be labeled. Accepting a label on
# anything else prints success and then silently discards the judgment.
LABELABLE_KINDS = ("gate_shadow_block", "gate_block", "reviewer_decision")


class LabelError(ValueError):
    """Raised when a label cannot be applied to the thing it names."""


SCHEMA_VERSION = 2
LEGACY_VERSION = 0


@dataclass(frozen=True)
class Label:
    session: str
    step: int | None  # None = session-level (observability ceiling)
    verdict: str
    fingerprint: str
    note: str
    labeled_at: str
    rater: str = ""
    # The verdict vocabularies have already changed once. Without a version a
    # future change reinterprets old labels silently, and every published rate
    # rests on them (issue #47).
    version: int = LEGACY_VERSION


def labels_path(session: str) -> Path:
    base = spotter_home() / "labels"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{sanitize_session(session)}.jsonl"


def fingerprint(record: StepRecord) -> str:
    """Identity of the labeled thing, insensitive to unrelated journal growth."""
    payload = {k: v for k, v in record.event.payload.items() if k != "proposal_number"}
    blob = json.dumps([record.event.kind, payload], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def session_fingerprint(records: list[StepRecord]) -> str:
    """Identity of a whole trajectory.

    A ceiling verdict is a judgment about the trajectory as a whole, so it
    must go stale when the trajectory grows — label sessions once they are
    finished, not while they are still running.
    """
    tail = fingerprint(records[-1]) if records else "empty"
    return f"session:{len(records)}:{tail}"


def add_label(
    session: str,
    step: int | None,
    verdict: str,
    note: str,
    records: list[StepRecord],
    *,
    rater: str | None = None,
) -> Label:
    allowed = SESSION_VERDICTS if step is None else STEP_VERDICTS
    if verdict not in allowed:
        raise LabelError(f"verdict must be one of {allowed} for this target")
    if step is None:
        mark = session_fingerprint(records)
    else:
        if not 0 <= step < len(records):
            raise LabelError(f"step {step} out of range (journal has {len(records)} steps)")
        target = records[step]
        if target.event.kind not in LABELABLE_KINDS:
            raise LabelError(
                f"step {step} is {target.event.kind}; only {LABELABLE_KINDS} are scored"
            )
        if target.event.payload.get("decision") == "continue":
            raise LabelError(f"step {step} is a CONTINUE verdict; silence is not scored")
        mark = fingerprint(target)
    rater_id = getuser() if rater is None else rater.strip()
    if not rater_id or len(rater_id) > 200:
        raise LabelError("rater must be a non-empty identity of at most 200 characters")
    label = Label(
        session,
        step,
        verdict,
        mark,
        note,
        datetime.now(UTC).isoformat(),
        rater_id,
        SCHEMA_VERSION,
    )
    with labels_path(session).open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(asdict(label), ensure_ascii=False) + "\n")
    return label


def load_labels(session: str) -> dict[int | None, Label]:
    """Latest label wins per target.

    A corrupt line raises. Skipping it would resurrect the verdict it was
    meant to overwrite: a torn write on a correction silently reinstates the
    judgment the labeler had just rejected, which is worse than no data.
    """
    labels: dict[int | None, Label] = {}
    for label in load_label_history(session):
        labels[label.step] = label
    return labels


def load_label_history(session: str) -> tuple[Label, ...]:
    """Read append-only label history so independent raters remain measurable."""

    path = labels_path(session)
    if not path.exists():
        return ()
    labels: list[Label] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            version = raw.get("version", LEGACY_VERSION)
            if not isinstance(version, int) or isinstance(version, bool):
                raise LabelError(f"{path.name} line {number} has a non-integer version")
            if version > SCHEMA_VERSION:
                raise LabelError(
                    f"{path.name} line {number} was written by schema v{version}; "
                    f"this build understands up to v{SCHEMA_VERSION}"
                )
            label = Label(
                session=str(raw["session"]),
                step=raw["step"],
                verdict=str(raw["verdict"]),
                fingerprint=str(raw["fingerprint"]),
                note=str(raw.get("note") or ""),
                labeled_at=str(raw.get("labeled_at") or ""),
                rater=str(raw.get("rater") or ""),
                version=version,
            )
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise LabelError(
                f"{path.name} line {number} is unreadable ({error}); "
                "a dropped correction would revive the verdict it replaced"
            ) from error
        labels.append(label)
    return tuple(labels)


def sessions_with_labels() -> list[str]:
    base = spotter_home() / "labels"
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.jsonl"))


def matches(label: Label, records: list[StepRecord]) -> bool:
    """False when the label has drifted off what it was applied to."""
    if label.step is None:
        return session_fingerprint(records) == label.fingerprint
    if not 0 <= label.step < len(records):
        return False
    return fingerprint(records[label.step]) == label.fingerprint


_SESSION_RE = re.compile(r"[A-Za-z0-9_-]+")


def valid_session(session: str) -> bool:
    """fullmatch, not match: Python's ``$`` also matches before a trailing
    newline, so "a\n" passed as valid and then sanitized to "a_" — sharing a
    journal and label file with the distinct session "a_"."""
    return bool(_SESSION_RE.fullmatch(session))
