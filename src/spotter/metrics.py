"""Turn human labels into coverage-aware detector and reviewer measurements.

Every rate is reported with its coverage (labeled / total). A rate computed
from a handful of labels is not a measurement, and this module refuses to
present one as if it were: rates below FULL coverage are marked provisional,
and rates with fewer than MIN_SAMPLES labels are withheld entirely.
"""

from dataclasses import dataclass

from spotter.labels import (
    Label,
    load_label_history,
    load_labels,
    matches,
    unflagged_proposal_eligibility,
)
from spotter.sampling import SignalSamplingBatch, load_signal_sampling, sample_matches
from spotter.snapshot import StepRecord

MIN_SAMPLES = 5
GATE_KINDS = ("gate_shadow_block", "gate_block")


@dataclass(frozen=True)
class Tally:
    labeled: int = 0
    total: int = 0
    positive: int = 0  # tp for gates/reviewer, "visible" for ceiling
    negative: int = 0
    unclear: int = 0
    not_applicable: int = 0  # session had no failure to see
    stale: int = 0

    def plus(self, verdict: str | None, *, stale: bool = False) -> "Tally":
        counted = verdict is not None and not stale
        return Tally(
            labeled=self.labeled + counted,
            total=self.total + 1,
            positive=self.positive + (counted and verdict in ("tp", "visible", "miss")),
            negative=self.negative + (counted and verdict in ("fp", "invisible", "tn")),
            unclear=self.unclear + (counted and verdict == "unclear"),
            not_applicable=self.not_applicable + (counted and verdict == "na"),
            stale=self.stale + stale,
        )

    def rate_line(self, name: str, measure: str, *, count_negative: bool = False) -> str:
        """One reported rate. `measure` must name what is counted — a header
        that says false positives while printing the true-positive share
        points the reader's decision the wrong way."""
        decided = self.positive + self.negative
        coverage = f"{self.labeled}/{self.total} labeled"
        if self.unclear:
            coverage += f", {self.unclear} unclear"
        if self.not_applicable:
            coverage += f", {self.not_applicable} n/a"
        if self.stale:
            coverage += f", {self.stale} stale"
        if decided < MIN_SAMPLES:
            return f"{name}: {coverage} — too few decided labels ({decided}) to state a rate"
        rate = (self.negative if count_negative else self.positive) / decided
        qualifier = "" if self.labeled == self.total else " (provisional: coverage incomplete)"
        return f"{name}: {measure} {rate:.0%} of {decided} decided; {coverage}{qualifier}"


@dataclass(frozen=True)
class AgreementTally:
    labeled_targets: int = 0
    double_labeled_targets: int = 0
    agreed_targets: int = 0
    disagreed_targets: int = 0
    unattributed_labels: int = 0
    stale_labels: int = 0

    def rate_line(self) -> str:
        coverage = (
            f"{self.double_labeled_targets}/{self.labeled_targets} labeled targets double-labeled"
        )
        if self.unattributed_labels:
            coverage += f", {self.unattributed_labels} unattributed legacy labels"
        if self.stale_labels:
            coverage += f", {self.stale_labels} stale labels"
        if self.double_labeled_targets < MIN_SAMPLES:
            return (
                f"raters: {coverage} — too few double-labeled targets "
                f"({self.double_labeled_targets}) to state agreement"
            )
        rate = self.agreed_targets / self.double_labeled_targets
        return f"raters: exact agreement {rate:.0%} of {self.double_labeled_targets}; {coverage}"


def _verdict(
    labels: dict[int | None, Label], records: list[StepRecord], step: int | None
) -> tuple[str | None, bool]:
    label = labels.get(step)
    if label is None:
        return None, False
    if not matches(label, records):
        return label.verdict, True
    return label.verdict, False


def tally_session(session: str, records: list[StepRecord]) -> tuple[dict[str, Tally], Tally, Tally]:
    """Return (gate tallies by rule, reviewer tally, ceiling tally)."""
    labels = load_labels(session)
    gates: dict[str, Tally] = {}
    reviewer = Tally()
    ceiling = Tally()
    for record in records:
        kind = record.event.kind
        if kind in GATE_KINDS:
            rule = str(record.event.payload.get("rule") or "unknown")
            verdict, stale = _verdict(labels, records, record.step)
            gates[rule] = gates.get(rule, Tally()).plus(verdict, stale=stale)
        elif kind == "reviewer_decision":
            # Only interventions are judgeable: a CONTINUE is silence, and
            # scoring silence as correct would inflate precision for free.
            if record.event.payload.get("decision") == "continue":
                continue
            verdict, stale = _verdict(labels, records, record.step)
            reviewer = reviewer.plus(verdict, stale=stale)
    # Every examined session is in the ceiling denominator, labeled or not.
    # Counting only labeled sessions would report 1/1 coverage after judging
    # one session out of a hundred.
    verdict, stale = _verdict(labels, records, None)
    ceiling = ceiling.plus(verdict, stale=stale)
    return gates, reviewer, ceiling


@dataclass(frozen=True)
class PendingLabel:
    """One judgeable record that still has no verdict from this rater."""

    session: str
    step: int
    kind: str
    subject: str


def pending_labels(
    session: str, records: list[StepRecord], *, rater: str | None = None
) -> tuple[PendingLabel, ...]:
    """List the judgeable records a rater has not decided yet.

    `tally_session` already knows which records are judgeable and which carry a
    verdict, but it only counts them. A rate of `0/122` tells a rater how much
    work is left and nothing about where it is, which leaves reading every
    journal by hand as the only way to start (#38).
    """

    if rater is None:
        decided = set(load_labels(session))
    else:
        decided = {
            label.step for label in load_label_history(session) if label.rater == rater.strip()
        }
    pending: list[PendingLabel] = []
    for record in records:
        kind = record.event.kind
        payload = record.event.payload
        if kind in GATE_KINDS:
            subject = str(payload.get("rule") or "unknown")
        elif kind == "reviewer_decision":
            # CONTINUE is silence; scoring it would inflate precision for free.
            if payload.get("decision") == "continue":
                continue
            subject = str(payload.get("decision") or "unknown")
        else:
            continue
        if record.step in decided:
            continue
        pending.append(PendingLabel(session, record.step, kind, subject))
    return tuple(pending)


def agreement_session(session: str, records: list[StepRecord]) -> AgreementTally:
    """Measure independent latest-per-rater judgments over current targets."""

    latest: dict[tuple[int | None, str, str], Label] = {}
    for label in load_label_history(session):
        latest[(label.step, label.scope, label.rater)] = label

    by_target: dict[tuple[int | None, str], list[Label]] = {}
    stale = 0
    unattributed = 0
    for label in latest.values():
        if not matches(label, records):
            stale += 1
            continue
        by_target.setdefault((label.step, label.scope), []).append(label)
        unattributed += not label.rater

    doubled = agreed = 0
    for labels in by_target.values():
        verdicts = [label.verdict for label in labels if label.rater]
        if len(verdicts) < 2:
            continue
        doubled += 1
        agreed += len(set(verdicts)) == 1
    return AgreementTally(
        labeled_targets=len(by_target),
        double_labeled_targets=doubled,
        agreed_targets=agreed,
        disagreed_targets=doubled - agreed,
        unattributed_labels=unattributed,
        stale_labels=stale,
    )


def tally_unflagged_proposals(session: str, records: list[StepRecord]) -> tuple[Tally, int]:
    """Return labeled gate-silence coverage and uncorrelatable proposal count."""

    labels = load_labels(session)
    tally = Tally()
    uncorrelatable = 0
    for record in records:
        if record.event.kind != "tool_proposal":
            continue
        eligibility = unflagged_proposal_eligibility(record, records)
        if eligibility is None:
            uncorrelatable += 1
        elif eligibility:
            verdict, stale = _verdict(labels, records, record.step)
            tally = tally.plus(verdict, stale=stale)
    return tally, uncorrelatable


def tally_signal_candidates(
    session: str, records: list[StepRecord]
) -> tuple[dict[str, Tally], int]:
    """Return active-candidate precision coverage by signal type and blind spots."""

    labels = load_labels(session)
    tallies: dict[str, Tally] = {}
    seen: set[str] = set()
    unattributed = 0
    for record in records:
        if (
            record.event.kind != "signal_candidate"
            or record.event.payload.get("status") != "active"
        ):
            continue
        signal_id = record.event.payload.get("signal_id")
        signal_type = record.event.payload.get("signal_type")
        if (
            not isinstance(signal_id, str)
            or not signal_id
            or not isinstance(signal_type, str)
            or not signal_type
        ):
            unattributed += 1
            continue
        if signal_id in seen:
            continue
        seen.add(signal_id)
        verdict, stale = _verdict(labels, records, record.step)
        tallies[signal_type] = tallies.get(signal_type, Tally()).plus(verdict, stale=stale)
    return tallies, unattributed


def tally_reviewer_continues(session: str, records: list[StepRecord]) -> Tally:
    """Return coverage and miss labels for explicit reviewer abstentions."""

    labels = load_labels(session)
    tally = Tally()
    for record in records:
        if (
            record.event.kind == "reviewer_decision"
            and record.event.payload.get("decision") == "continue"
        ):
            verdict, stale = _verdict(labels, records, record.step)
            tally = tally.plus(verdict, stale=stale)
    return tally


def tally_reviewer_triggers(
    session: str, records: list[StepRecord]
) -> tuple[dict[str, Tally], dict[str, Tally]]:
    """Stratify reviewer precision and misses by the durable launch trigger."""

    labels = load_labels(session)
    job_triggers = _review_job_triggers(records)
    interventions: dict[str, Tally] = {}
    continues: dict[str, Tally] = {}
    for record in records:
        if record.event.kind != "reviewer_decision":
            continue
        trigger = _review_trigger(record, job_triggers)
        verdict, stale = _verdict(labels, records, record.step)
        target = continues if record.event.payload.get("decision") == "continue" else interventions
        target[trigger] = target.get(trigger, Tally()).plus(verdict, stale=stale)
    return interventions, continues


def _review_job_triggers(records: list[StepRecord]) -> dict[str, str]:
    triggers: dict[str, str] = {}
    for record in records:
        if record.event.kind != "review_job_queued":
            continue
        job_id = record.event.payload.get("review_job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        trigger = record.event.payload.get("review_trigger")
        if isinstance(trigger, str) and trigger:
            triggers[job_id] = trigger
        elif isinstance(record.event.payload.get("signal_id"), str):
            triggers[job_id] = "signal"
        elif job_id.startswith("proposal:"):
            triggers[job_id] = "periodic"
    return triggers


def _review_trigger(record: StepRecord, job_triggers: dict[str, str]) -> str:
    payload = record.event.payload
    trigger = payload.get("review_trigger")
    if isinstance(trigger, str) and trigger:
        return trigger
    job_id = payload.get("review_job_id")
    if not isinstance(job_id, str) or not job_id:
        return "manual"
    if job_id in job_triggers:
        return job_triggers[job_id]
    return "periodic" if job_id.startswith("proposal:") else "unknown"


def tally_signal_silence(
    session: str, records: list[StepRecord]
) -> tuple[dict[str, Tally], tuple[SignalSamplingBatch, ...]]:
    """Return miss coverage for persisted non-emitted signal samples."""

    batches, stored_samples = load_signal_sampling(session)
    batches_by_id = {batch.batch_id: batch for batch in batches}
    samples = {(sample.step, sample.signal_type): sample for sample in stored_samples}
    labels = {
        signal_type: load_labels(session, scope=f"signal:{signal_type}")
        for _, signal_type in samples
    }
    tallies: dict[str, Tally] = {}
    for sample in samples.values():
        batch = batches_by_id[sample.batch_id]
        event_kinds = ",".join(batch.event_kinds)
        key = f"{sample.signal_type}/{event_kinds}@p={batch.inclusion_probability:g}"
        label = labels[sample.signal_type].get(sample.step)
        stale = not sample_matches(sample, records) or bool(
            label is not None and not matches(label, records)
        )
        verdict = label.verdict if label is not None else None
        tallies[key] = tallies.get(key, Tally()).plus(verdict, stale=stale)
    return tallies, batches


def merge(left: Tally, right: Tally) -> Tally:
    return Tally(
        labeled=left.labeled + right.labeled,
        total=left.total + right.total,
        positive=left.positive + right.positive,
        negative=left.negative + right.negative,
        unclear=left.unclear + right.unclear,
        not_applicable=left.not_applicable + right.not_applicable,
        stale=left.stale + right.stale,
    )


def merge_agreement(left: AgreementTally, right: AgreementTally) -> AgreementTally:
    return AgreementTally(
        labeled_targets=left.labeled_targets + right.labeled_targets,
        double_labeled_targets=left.double_labeled_targets + right.double_labeled_targets,
        agreed_targets=left.agreed_targets + right.agreed_targets,
        disagreed_targets=left.disagreed_targets + right.disagreed_targets,
        unattributed_labels=left.unattributed_labels + right.unattributed_labels,
        stale_labels=left.stale_labels + right.stale_labels,
    )
