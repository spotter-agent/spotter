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
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from spotter.hook import sanitize_session, spotter_home
from spotter.snapshot import StepRecord

STEP_VERDICTS = ("tp", "fp", "unclear")
SESSION_VERDICTS = ("visible", "invisible", "unclear")


class LabelError(ValueError):
    """Raised when a label cannot be applied to the thing it names."""


@dataclass(frozen=True)
class Label:
    session: str
    step: int | None  # None = session-level (observability ceiling)
    verdict: str
    fingerprint: str
    note: str
    labeled_at: str


def labels_path(session: str) -> Path:
    base = spotter_home() / "labels"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{sanitize_session(session)}.jsonl"


def fingerprint(record: StepRecord) -> str:
    """Identity of the labeled thing, insensitive to unrelated journal growth."""
    payload = {k: v for k, v in record.event.payload.items() if k != "proposal_number"}
    blob = json.dumps([record.event.kind, payload], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def add_label(
    session: str, step: int | None, verdict: str, note: str, records: list[StepRecord]
) -> Label:
    allowed = SESSION_VERDICTS if step is None else STEP_VERDICTS
    if verdict not in allowed:
        raise LabelError(f"verdict must be one of {allowed} for this target")
    if step is None:
        mark = "session"
    else:
        if not 0 <= step < len(records):
            raise LabelError(f"step {step} out of range (journal has {len(records)} steps)")
        mark = fingerprint(records[step])
    label = Label(session, step, verdict, mark, note, datetime.now(UTC).isoformat())
    with labels_path(session).open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(asdict(label), ensure_ascii=False) + "\n")
    return label


def load_labels(session: str) -> dict[int | None, Label]:
    """Latest label wins per target; corrupt lines are skipped loudly enough
    to be visible in coverage counts rather than silently dropped."""
    path = labels_path(session)
    labels: dict[int | None, Label] = {}
    if not path.exists():
        return labels
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            label = Label(
                session=str(raw["session"]),
                step=raw["step"],
                verdict=str(raw["verdict"]),
                fingerprint=str(raw["fingerprint"]),
                note=str(raw.get("note") or ""),
                labeled_at=str(raw.get("labeled_at") or ""),
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        labels[label.step] = label
    return labels


def sessions_with_labels() -> list[str]:
    base = spotter_home() / "labels"
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.jsonl"))


def matches(label: Label, records: list[StepRecord]) -> bool:
    """False when the label has drifted off the record it was applied to."""
    if label.step is None:
        return True
    if not 0 <= label.step < len(records):
        return False
    return fingerprint(records[label.step]) == label.fingerprint


_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def valid_session(session: str) -> bool:
    return bool(_SESSION_RE.match(session)) and os.sep not in session
