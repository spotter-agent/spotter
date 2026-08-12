"""Turn labels and experiments into the numbers the plan gates on.

Every rate is reported with its coverage (labeled / total). A rate computed
from a handful of labels is not a measurement, and this module refuses to
present one as if it were: rates below FULL coverage are marked provisional,
and rates with fewer than MIN_SAMPLES labels are withheld entirely.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from spotter.labels import Label, load_labels, matches
from spotter.snapshot import StepRecord

MIN_SAMPLES = 5
GATE_KINDS = ("gate_shadow_block", "gate_block")


@dataclass(frozen=True)
class HarmTally:
    harmed: int = 0
    complete: int = 0
    total: int = 0

    def line(self) -> str:
        coverage = f"{self.complete}/{self.total} complete pairs"
        if self.complete < MIN_SAMPLES:
            return f"harm: {coverage} — too few complete pairs ({self.complete}) to state a rate"
        qualifier = "" if self.complete == self.total else " (provisional: coverage incomplete)"
        return (
            f"harm: {self.harmed / self.complete:.0%} of {self.complete} pairs; "
            f"{coverage}{qualifier}"
        )


def tally_harm(paths: list[Path]) -> HarmTally:
    """Count control-pass/guidance-fail pairs from durable experiment rows."""
    harmed = complete = total = 0
    for path in paths:
        experiments: dict[str, dict[int, dict[str, tuple[int | None, int | None]]]] = {}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path.name} line {number} is unreadable") from error
            if "arm" not in row:
                continue
            pair = experiments.setdefault(str(row["experiment_id"]), {}).setdefault(
                int(row["pair"]), {}
            )
            pair[str(row["arm"])] = (row.get("agent_exit"), row.get("check_exit"))
        for pairs in experiments.values():
            total += len(pairs)
            for arms in pairs.values():
                if set(arms) != {"control", "guidance"} or any(
                    agent != 0 or check is None for agent, check in arms.values()
                ):
                    continue
                complete += 1
                harmed += arms["control"][1] == 0 and arms["guidance"][1] != 0
    return HarmTally(harmed, complete, total)


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
