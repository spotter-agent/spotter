"""Durable deterministic sampling frames for detector-negative evidence."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from spotter.paths import sanitize_session, spotter_home
from spotter.signals import SignalType
from spotter.snapshot import StepRecord

SCHEMA_VERSION = 1
_SIGNAL_TYPES = {signal_type.value for signal_type in SignalType}


class SignalSampleError(ValueError):
    """Raised when a signal sampling frame is invalid or unreadable."""


@dataclass(frozen=True)
class SignalSamplingBatch:
    batch_id: str
    session: str
    signal_type: str
    event_kinds: tuple[str, ...]
    inclusion_probability: float
    start_step: int
    end_step: int
    eligible: int
    selected: int
    excluded_emitted: int
    excluded_suppressed: int
    excluded_unobservable: int
    sampled_at: str
    version: int = SCHEMA_VERSION


@dataclass(frozen=True)
class SignalSample:
    batch_id: str
    session: str
    step: int
    signal_type: str
    event_kind: str
    event_id: str
    fingerprint: str
    sampled_at: str
    version: int = SCHEMA_VERSION


def signal_samples_path(session: str) -> Path:
    base = spotter_home() / "signal-samples"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{sanitize_session(session)}.jsonl"


def sample_signal_silence(
    session: str,
    records: list[StepRecord],
    signal_type: str,
    event_kinds: tuple[str, ...],
    inclusion_probability: float,
) -> SignalSamplingBatch:
    """Persist a deterministic event-kind-stratified sample of non-emitted sources."""

    try:
        normalized_type = SignalType(signal_type).value
    except ValueError as error:
        raise SignalSampleError(f"unknown signal type: {signal_type}") from error
    kinds = tuple(sorted(set(event_kinds)))
    if not kinds or any(not kind for kind in kinds):
        raise SignalSampleError("at least one non-empty event kind is required")
    probability = float(inclusion_probability)
    if not 0 < probability <= 1:
        raise SignalSampleError("sample rate must be greater than 0 and at most 1")

    existing_batches, _ = load_signal_sampling(session)
    conflicting = next(
        (
            batch
            for batch in existing_batches
            if batch.signal_type == normalized_type
            and set(batch.event_kinds).intersection(kinds)
            and (batch.event_kinds != kinds or batch.inclusion_probability != probability)
        ),
        None,
    )
    if conflicting is not None:
        raise SignalSampleError(
            "overlapping signal strata must keep the same event kinds and sample rate"
        )
    matching_batches = [
        batch
        for batch in existing_batches
        if batch.signal_type == normalized_type
        and batch.event_kinds == kinds
        and batch.inclusion_probability == probability
    ]
    previous_end = max((batch.end_step for batch in matching_batches), default=-1)
    end_step = len(records) - 1
    if matching_batches and previous_end >= end_step:
        return max(matching_batches, key=lambda batch: batch.end_step)

    emitted_sources = {
        str(record.event.payload["source_event_id"])
        for record in records
        if record.event.kind == "signal_candidate"
        and record.event.payload.get("status") == "active"
        and record.event.payload.get("signal_type") == normalized_type
        and isinstance(record.event.payload.get("source_event_id"), str)
        and record.event.payload["source_event_id"]
    }
    suppressed_sources = {
        str(record.event.payload["source_event_id"])
        for record in records
        if record.event.kind == "signal_candidate_suppressed"
        and record.event.payload.get("signal_type") == normalized_type
        and isinstance(record.event.payload.get("source_event_id"), str)
        and record.event.payload["source_event_id"]
    }
    suppressed_sources.difference_update(emitted_sources)
    frame = [
        record for record in records if record.step > previous_end and record.event.kind in kinds
    ]
    excluded_unobservable = sum(
        not isinstance(record.event.event_id, str)
        or not record.event.event_id
        or record.event.identity is None
        or record.event.identity.thread_id is None
        for record in frame
    )
    observable = [
        record
        for record in frame
        if isinstance(record.event.event_id, str)
        and record.event.event_id
        and record.event.identity is not None
        and record.event.identity.thread_id is not None
    ]
    excluded_emitted = sum(record.event.event_id in emitted_sources for record in observable)
    excluded_suppressed = sum(record.event.event_id in suppressed_sources for record in observable)
    detected_sources = emitted_sources | suppressed_sources
    eligible = [record for record in observable if record.event.event_id not in detected_sources]

    frame_identity = _digest(
        session,
        normalized_type,
        *kinds,
        str(probability),
        str(previous_end + 1),
        str(end_step),
        sample_fingerprint(records[-1]) if records else "empty",
    )
    batch_id = f"signal-sample:{frame_identity}"

    seed = _digest(session, normalized_type, *kinds, str(probability))
    selected = [
        record for record in eligible if _fraction(seed, str(record.event.event_id)) < probability
    ]
    sampled_at = datetime.now(UTC).isoformat()
    batch = SignalSamplingBatch(
        batch_id,
        session,
        normalized_type,
        kinds,
        probability,
        previous_end + 1,
        end_step,
        len(eligible),
        len(selected),
        excluded_emitted,
        excluded_suppressed,
        excluded_unobservable,
        sampled_at,
    )
    path = signal_samples_path(session)
    with path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps({"record_type": "batch", **asdict(batch)}) + "\n")
        for record in selected:
            assert record.event.event_id is not None
            sample = SignalSample(
                batch_id,
                session,
                record.step,
                normalized_type,
                record.event.kind,
                record.event.event_id,
                sample_fingerprint(record),
                sampled_at,
            )
            sink.write(json.dumps({"record_type": "sample", **asdict(sample)}) + "\n")
    return batch


def load_signal_sampling(
    session: str,
) -> tuple[tuple[SignalSamplingBatch, ...], tuple[SignalSample, ...]]:
    path = signal_samples_path(session)
    if not path.exists():
        return (), ()
    batches: list[SignalSamplingBatch] = []
    samples: list[SignalSample] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            version = raw.get("version")
            if not isinstance(version, int) or isinstance(version, bool):
                raise SignalSampleError(f"{path.name} line {number} has a non-integer version")
            if version > SCHEMA_VERSION:
                raise SignalSampleError(
                    f"{path.name} line {number} was written by schema v{version}; "
                    f"this build understands up to v{SCHEMA_VERSION}"
                )
            if raw.get("record_type") == "batch":
                batches.append(
                    SignalSamplingBatch(
                        batch_id=str(raw["batch_id"]),
                        session=str(raw["session"]),
                        signal_type=str(raw["signal_type"]),
                        event_kinds=tuple(str(kind) for kind in raw["event_kinds"]),
                        inclusion_probability=float(raw["inclusion_probability"]),
                        start_step=int(raw["start_step"]),
                        end_step=int(raw["end_step"]),
                        eligible=int(raw["eligible"]),
                        selected=int(raw["selected"]),
                        excluded_emitted=int(raw["excluded_emitted"]),
                        excluded_suppressed=int(raw["excluded_suppressed"]),
                        excluded_unobservable=int(raw["excluded_unobservable"]),
                        sampled_at=str(raw["sampled_at"]),
                        version=version,
                    )
                )
            elif raw.get("record_type") == "sample":
                samples.append(
                    SignalSample(
                        batch_id=str(raw["batch_id"]),
                        session=str(raw["session"]),
                        step=int(raw["step"]),
                        signal_type=str(raw["signal_type"]),
                        event_kind=str(raw["event_kind"]),
                        event_id=str(raw["event_id"]),
                        fingerprint=str(raw["fingerprint"]),
                        sampled_at=str(raw["sampled_at"]),
                        version=version,
                    )
                )
            else:
                raise SignalSampleError(f"{path.name} line {number} has an unknown record type")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            if isinstance(error, SignalSampleError):
                raise
            raise SignalSampleError(f"{path.name} line {number} is unreadable ({error})") from error
    batches_by_id = {batch.batch_id: batch for batch in batches}
    if len(batches_by_id) != len(batches):
        raise SignalSampleError(f"{path.name} contains a duplicate batch id")
    for batch in batches:
        if (
            batch.session != session
            or batch.signal_type not in _SIGNAL_TYPES
            or not batch.event_kinds
            or any(not kind for kind in batch.event_kinds)
            or not 0 < batch.inclusion_probability <= 1
            or batch.start_step < 0
            or batch.end_step < batch.start_step - 1
            or min(
                batch.eligible,
                batch.selected,
                batch.excluded_emitted,
                batch.excluded_suppressed,
                batch.excluded_unobservable,
            )
            < 0
            or batch.selected > batch.eligible
        ):
            raise SignalSampleError(f"{path.name} batch {batch.batch_id} is invalid")
    orphan = next((sample for sample in samples if sample.batch_id not in batches_by_id), None)
    if orphan is not None:
        raise SignalSampleError(
            f"{path.name} sample at step {orphan.step} references an unknown batch"
        )
    for sample in samples:
        batch = batches_by_id[sample.batch_id]
        if (
            sample.session != session
            or sample.signal_type != batch.signal_type
            or sample.event_kind not in batch.event_kinds
            or not batch.start_step <= sample.step <= batch.end_step
            or not sample.event_id
            or not sample.fingerprint
        ):
            raise SignalSampleError(
                f"{path.name} sample at step {sample.step} disagrees with its batch"
            )
    counts = {batch.batch_id: 0 for batch in batches}
    for sample in samples:
        counts[sample.batch_id] += 1
    mismatch = next(
        (batch for batch in batches if counts[batch.batch_id] != batch.selected),
        None,
    )
    if mismatch is not None:
        raise SignalSampleError(
            f"{path.name} batch {mismatch.batch_id} selected count does not match its samples"
        )
    return tuple(batches), tuple(samples)


def sample_matches(sample: SignalSample, records: list[StepRecord]) -> bool:
    if not 0 <= sample.step < len(records):
        return False
    record = records[sample.step]
    return (
        record.event.event_id == sample.event_id
        and sample_fingerprint(record) == sample.fingerprint
        and signal_source_is_silent(sample.event_id, sample.signal_type, records)
    )


def signal_source_is_silent(
    event_id: str | None, signal_type: str, records: list[StepRecord]
) -> bool:
    return not any(
        candidate.event.kind in {"signal_candidate", "signal_candidate_suppressed"}
        and (
            candidate.event.kind == "signal_candidate_suppressed"
            or candidate.event.payload.get("status") == "active"
        )
        and candidate.event.payload.get("signal_type") == signal_type
        and candidate.event.payload.get("source_event_id") == event_id
        for candidate in records
    )


def sample_fingerprint(record: StepRecord) -> str:
    payload = {
        key: value for key, value in record.event.payload.items() if key != "proposal_number"
    }
    identity = record.event.identity
    address = (
        [
            identity.thread_id.value if identity.thread_id is not None else None,
            identity.turn_id.value if identity.turn_id is not None else None,
            identity.attachment_id.value if identity.attachment_id is not None else None,
            record.event.connection_epoch,
        ]
        if identity is not None
        else None
    )
    blob = json.dumps(
        [record.event.kind, record.event.event_id, address, payload],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def _fraction(seed: str, event_id: str) -> float:
    value = int(hashlib.sha256(f"{seed}\0{event_id}".encode()).hexdigest(), 16)
    return value / (1 << 256)
