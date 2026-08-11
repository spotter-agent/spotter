"""Turn labels into the three numbers the plan gates on.

Every rate is reported with its coverage (labeled / total). A rate computed
from a handful of labels is not a measurement, and this module refuses to
present one as if it were: rates below FULL coverage are marked provisional,
and rates with fewer than MIN_SAMPLES labels are withheld entirely.
"""

from dataclasses import dataclass

from spotter.labels import Label, load_labels, matches
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
            positive=self.positive + (counted and verdict in ("tp", "visible")),
            negative=self.negative + (counted and verdict in ("fp", "invisible")),
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
        if self.not_applicable:
            coverage += f", {self.not_applicable} n/a"
        if self.stale:
            coverage += f", {self.stale} stale"
        if decided < MIN_SAMPLES:
            return f"{name}: {coverage} — too few decided labels ({decided}) to state a rate"
        rate = (self.negative if count_negative else self.positive) / decided
        qualifier = "" if self.labeled == self.total else " (provisional: coverage incomplete)"
        return f"{name}: {measure} {rate:.0%} of {decided} decided; {coverage}{qualifier}"


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
