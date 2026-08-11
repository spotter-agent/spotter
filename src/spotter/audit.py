"""Independent audit state: claims are not evidence (plan Q2).

The type system is the enforcement mechanism: ``EvidenceSource`` has no
"summary" member, so a reasoning summary cannot become ``Evidence`` without
mypy rejecting the call site. Summaries enter only as unverified hypotheses.
"""

from dataclasses import dataclass, field
from typing import Literal, get_args

EvidenceSource = Literal["tool_result", "diff", "test_output", "repo_state"]
HypothesisStatus = Literal["unverified", "supported", "stale"]

_VALID_SOURCES = frozenset(get_args(EvidenceSource))


@dataclass(frozen=True)
class Evidence:
    id: str
    source: EvidenceSource
    description: str

    def __post_init__(self) -> None:
        # Runtime guard for untyped callers (JSON ingestion bypasses mypy).
        if self.source not in _VALID_SOURCES:
            raise ValueError(f"not an evidence source: {self.source!r}")


@dataclass
class Hypothesis:
    id: str
    claim: str
    supported_by: set[str] = field(default_factory=set)  # evidence ids
    depends_on: set[str] = field(default_factory=set)  # hypothesis ids
    status: HypothesisStatus = "unverified"


class AuditState:
    """Claim/evidence ledger with transitive stale propagation."""

    def __init__(self) -> None:
        self.evidence: dict[str, Evidence] = {}
        self.hypotheses: dict[str, Hypothesis] = {}
        self.retracted: set[str] = set()

    def add_evidence(self, evidence: Evidence) -> None:
        if evidence.id in self.evidence:
            raise ValueError(f"duplicate evidence id: {evidence.id}")
        self.evidence[evidence.id] = evidence

    def add_hypothesis(self, hypothesis: Hypothesis) -> None:
        if hypothesis.id in self.hypotheses:
            raise ValueError(f"duplicate hypothesis id: {hypothesis.id}")
        self.hypotheses[hypothesis.id] = hypothesis

    def support(self, hypothesis_id: str, evidence_id: str) -> None:
        if evidence_id in self.retracted:
            raise ValueError(f"evidence is retracted: {evidence_id}")
        hypothesis = self.hypotheses[hypothesis_id]
        _ = self.evidence[evidence_id]  # KeyError on unknown evidence
        hypothesis.supported_by.add(evidence_id)
        if hypothesis.status == "unverified":
            hypothesis.status = "supported"

    def retract(self, evidence_id: str) -> set[str]:
        """Retract evidence; mark every transitively dependent hypothesis stale.

        Returns the ids of hypotheses that became stale.
        """
        _ = self.evidence[evidence_id]
        self.retracted.add(evidence_id)
        stale = {
            h.id
            for h in self.hypotheses.values()
            if evidence_id in h.supported_by and not h.supported_by - self.retracted
        }
        # Propagate along depends_on edges; visited set guards against cycles.
        frontier = set(stale)
        while frontier:
            frontier = {
                h.id
                for h in self.hypotheses.values()
                if h.id not in stale and h.depends_on & frontier
            }
            stale |= frontier
        for hypothesis_id in stale:
            self.hypotheses[hypothesis_id].status = "stale"
        return stale
