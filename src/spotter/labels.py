"""Human labels over recorded trajectories — the input to every plan decision.

The plan's evidence gates wait on the same thing: was a judgment right?
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
from spotter.sampling import (
    load_signal_sampling,
    sample_fingerprint,
    sample_matches,
    signal_source_is_silent,
)
from spotter.snapshot import StepRecord

STEP_VERDICTS = ("tp", "fp", "unclear")
UNFLAGGED_VERDICTS = ("miss", "tn", "unclear")
# "na" disposes of a session that had no failure to see — without it the
# ceiling denominator can never reach full coverage, and a metric whose
# coverage cannot be completed is a metric nobody will ever trust.
SESSION_VERDICTS = ("visible", "invisible", "unclear", "na")

# Only records metrics actually scores may be labeled. Accepting a label on
# anything else prints success and then silently discards the judgment.
LABELABLE_KINDS = (
    "gate_shadow_block",
    "gate_block",
    "reviewer_decision",
    "signal_candidate",
    "tool_proposal",
)


class LabelError(ValueError):
    """Raised when a label cannot be applied to the thing it names."""


SCHEMA_VERSION = 6
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
    scope: str = ""
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
    signal_type: str | None = None,
) -> Label:
    allowed: tuple[str, ...]
    scope = ""
    if step is None:
        if signal_type is not None:
            raise LabelError("a signal-silence label requires a sampled journal step")
        allowed = SESSION_VERDICTS
        mark = session_fingerprint(records)
    else:
        if not 0 <= step < len(records):
            raise LabelError(f"step {step} out of range (journal has {len(records)} steps)")
        target = records[step]
        if signal_type is not None:
            scope = f"signal:{signal_type}"
            _, samples = load_signal_sampling(session)
            sample = next(
                (
                    sample
                    for sample in samples
                    if sample.step == step and sample.signal_type == signal_type
                ),
                None,
            )
            if sample is None:
                raise LabelError(
                    f"step {step} is not in a persisted silence sample for {signal_type}"
                )
            if not sample_matches(sample, records):
                raise LabelError(f"step {step} signal sample is stale")
            allowed = UNFLAGGED_VERDICTS
            mark = sample.fingerprint
        elif target.event.kind not in LABELABLE_KINDS:
            raise LabelError(
                f"step {step} is {target.event.kind}; only {LABELABLE_KINDS} are scored"
            )
        elif target.event.kind == "tool_proposal":
            eligibility = unflagged_proposal_eligibility(target, records)
            if eligibility is None:
                raise LabelError(
                    f"step {step} proposal has no correlation id; unflagged status is unknown"
                )
            if not eligibility:
                raise LabelError(f"step {step} proposal has a correlated gate flag")
            allowed = UNFLAGGED_VERDICTS
        elif target.event.kind == "signal_candidate":
            payload = target.event.payload
            if payload.get("status") != "active":
                raise LabelError(f"step {step} signal candidate is not active")
            if not isinstance(payload.get("signal_id"), str) or not payload["signal_id"]:
                raise LabelError(f"step {step} signal candidate has no stable identity")
            if not isinstance(payload.get("signal_type"), str) or not payload["signal_type"]:
                raise LabelError(f"step {step} signal candidate has no signal type")
            if any(
                record.event.kind == "signal_candidate"
                and record.event.payload.get("status") == "active"
                and record.event.payload.get("signal_id") == payload["signal_id"]
                for record in records[:step]
            ):
                raise LabelError(f"step {step} repeats an earlier active signal candidate")
            allowed = STEP_VERDICTS
        elif (
            target.event.kind == "reviewer_decision"
            and target.event.payload.get("decision") == "continue"
        ):
            allowed = UNFLAGGED_VERDICTS
        else:
            allowed = STEP_VERDICTS
        if signal_type is None:
            mark = fingerprint(target)
    if verdict not in allowed:
        raise LabelError(f"verdict must be one of {allowed} for this target")
    if scope and not note.strip():
        raise LabelError("a sampled signal verdict requires written criteria in --note")
    rater_id = getuser() if rater is None else rater.strip()
    if not rater_id or len(rater_id) > 200:
        raise LabelError("rater must be a non-empty identity of at most 200 characters")
    label = Label(
        session=session,
        step=step,
        verdict=verdict,
        fingerprint=mark,
        note=note,
        labeled_at=datetime.now(UTC).isoformat(),
        rater=rater_id,
        scope=scope,
        version=SCHEMA_VERSION,
    )
    with labels_path(session).open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(asdict(label), ensure_ascii=False) + "\n")
    return label


def unflagged_proposal_eligibility(record: StepRecord, records: list[StepRecord]) -> bool | None:
    """True only when correlation proves a proposal has no gate flag."""

    if record.event.kind != "tool_proposal":
        return None
    tool_use_id = record.event.payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return None
    return not any(
        candidate.event.kind in {"gate_shadow_block", "gate_block"}
        and candidate.event.payload.get("tool_use_id") == tool_use_id
        for candidate in records
    )


def load_labels(session: str, *, scope: str = "") -> dict[int | None, Label]:
    """Latest label wins per target.

    A corrupt line raises. Skipping it would resurrect the verdict it was
    meant to overwrite: a torn write on a correction silently reinstates the
    judgment the labeler had just rejected, which is worse than no data.
    """
    labels: dict[int | None, Label] = {}
    for label in load_label_history(session):
        if label.scope == scope:
            labels[label.step] = label
    return labels


def load_all_labels(session: str) -> dict[tuple[int | None, str], Label]:
    """Latest label wins independently for each target and measurement scope."""

    labels: dict[tuple[int | None, str], Label] = {}
    for label in load_label_history(session):
        labels[(label.step, label.scope)] = label
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
                scope=str(raw.get("scope") or "") if version >= 6 else "",
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
    if label.scope.startswith("signal:"):
        record = records[label.step]
        signal_type = label.scope.removeprefix("signal:")
        return sample_fingerprint(record) == label.fingerprint and signal_source_is_silent(
            record.event.event_id, signal_type, records
        )
    return fingerprint(records[label.step]) == label.fingerprint


_SESSION_RE = re.compile(r"[A-Za-z0-9_-]+")


def valid_session(session: str) -> bool:
    """fullmatch, not match: Python's ``$`` also matches before a trailing
    newline, so "a\n" passed as valid and then sanitized to "a_" — sharing a
    journal and label file with the distinct session "a_"."""
    return bool(_SESSION_RE.fullmatch(session))
