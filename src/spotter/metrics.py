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
    stale: int = 0

    def plus(self, verdict: str | None, *, stale: bool = False) -> "Tally":
        return Tally(
            labeled=self.labeled + (verdict is not None and not stale),
            total=self.total + 1,
            positive=self.positive + (verdict in ("tp", "visible") and not stale),
            negative=self.negative + (verdict in ("fp", "invisible") and not stale),
            unclear=self.unclear + (verdict == "unclear" and not stale),
            stale=self.stale + stale,
        )

    def rate_line(self, name: str, positive_name: str) -> str:
        decided = self.positive + self.negative
        coverage = f"{self.labeled}/{self.total} labeled"
        if self.stale:
            coverage += f", {self.stale} stale"
        if decided < MIN_SAMPLES:
            return f"{name}: {coverage} — too few decided labels ({decided}) to state a rate"
        rate = self.positive / decided
        qualifier = "" if self.labeled == self.total else " (provisional: coverage incomplete)"
        return f"{name}: {positive_name} {rate:.0%} of {decided} decided; {coverage}{qualifier}"


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
    if None in labels:
        verdict, _ = _verdict(labels, records, None)
        ceiling = ceiling.plus(verdict)
    return gates, reviewer, ceiling


def merge(left: Tally, right: Tally) -> Tally:
    return Tally(
        labeled=left.labeled + right.labeled,
        total=left.total + right.total,
        positive=left.positive + right.positive,
        negative=left.negative + right.negative,
        unclear=left.unclear + right.unclear,
        stale=left.stale + right.stale,
    )
