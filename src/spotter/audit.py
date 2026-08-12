"""Independent audit state: claims are not evidence (plan Q2).

The type system is the enforcement mechanism: ``EvidenceSource`` has no
"summary" member, so a reasoning summary cannot become ``Evidence`` without
mypy rejecting the call site. Summaries enter only as unverified hypotheses.
"""

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from spotter.snapshot import StepRecord

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


def build_state(records: list["StepRecord"]) -> AuditState:
    """Reconstruct the ledger from a journal (plan P2, wired).

    Evidence comes only from observable outcomes — a tool result, never a
    summary the agent wrote about itself. Hypotheses come from the reviewer's
    own VERIFY/NUDGE claims, which is why they enter as ``unverified``.

    Retraction is mechanical, not a judgment call: when the same command later
    produces a different exit code, the earlier outcome is no longer true, so
    it is retracted and anything resting solely on it goes stale.
    """
    state = AuditState()
    outcomes: dict[str, str] = {}  # command -> evidence id of its last result
    for record in records:
        payload = record.event.payload
        if record.event.kind == "tool_result":
            command = _command_of(payload)
            exit_code = _exit_code_of(payload)
            if command is None or exit_code is None:
                continue
            evidence_id = f"e{record.step}"
            state.add_evidence(
                Evidence(evidence_id, "tool_result", f"{command} -> exit {exit_code}")
            )
            previous = outcomes.get(command)
            if previous is not None and state.evidence[previous].description.rsplit(" ", 1)[
                -1
            ] != str(exit_code):
                state.retract(previous)  # same command, different outcome
            outcomes[command] = evidence_id
        elif record.event.kind == "reviewer_decision":
            claim = payload.get("hypothesis")
            if isinstance(claim, str) and claim.strip():
                state.add_hypothesis(
                    Hypothesis(
                        f"h{record.step}",
                        claim.strip(),
                        supported_by=set(state.evidence) - state.retracted,
                    )
                )
    return state


def _command_of(payload: dict[str, object]) -> str | None:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return " ".join(command.split())
    return None


_EXIT_CODE_TEXT = re.compile(r"(?im)^Exit code: (-?\d+)\s*$")


def _exit_code_of(payload: dict[str, object]) -> int | None:
    """Outcome of a tool call, where one is observable at all.

    Two shapes exist in the wild: a structured ``{"exit_code": n}`` response,
    and Codex's raw text with an ``Exit code: n`` line. Measured on real
    journals, only 33 of 301 Codex tool results carry an outcome at all (all
    of them apply_patch) — shell results carry none. So the ledger records
    outcomes where they exist and stays silent where they do not, rather than
    inventing pass/fail from output text that legitimately differs run to run.
    That gap is an observability-ceiling fact (plan P1), not a parser bug.
    """
    response = payload.get("tool_response")
    if isinstance(response, dict):
        exit_code = response.get("exit_code")
        if isinstance(exit_code, int):
            return exit_code
    if isinstance(response, str):
        match = _EXIT_CODE_TEXT.search(response)
        if match:
            return int(match.group(1))
    return None


def stale_summary(state: AuditState) -> list[str]:
    """Lines for the reviewer digest: what stopped being true, and what that
    invalidated. An empty list means nothing was retracted."""
    lines: list[str] = []
    for evidence_id in sorted(state.retracted):
        lines.append(f"RETRACTED {state.evidence[evidence_id].description}")
    for hypothesis in state.hypotheses.values():
        if hypothesis.status == "stale":
            lines.append(f"STALE hypothesis: {hypothesis.claim}")
    return lines
