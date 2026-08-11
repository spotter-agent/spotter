"""Reviewer: model-backed trajectory judgment — SHADOW MODE ONLY (plan P4 step 1).

Decisions are appended to the journal and injected nowhere. The reviewer must
earn injection rights the same way the gates did: shadow first, label its
verdicts against reality, then measure intervention advantage with fork pairs.

Model access reuses `codex exec` (zero new dependencies, existing auth). The
digest fed to the reviewer contains observable actions only — commands, files,
exit codes, gate flags — never Main's own summaries as facts (plan Q2).
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from spotter.audit import build_state, stale_summary
from spotter.snapshot import StepRecord

DECISIONS = ("continue", "verify", "nudge")
FAILURE_CLASSES = ("none", "exploration_loop", "tool_failure_loop", "spec_drift")

_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "failure_class": {"type": "string", "enum": list(FAILURE_CLASSES)},
        "reason": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "hypothesis": {"type": "string"},
    },
    "required": ["decision", "failure_class", "reason", "confidence", "hypothesis"],
    "additionalProperties": False,
}

_PROMPT = """You are Spotter, a runtime reviewer watching another coding agent's trajectory.
You see only observable actions below (commands, files, exit signals, gate flags).
Judge the PROCESS, not the code: is the agent looping without new information,
retrying a failing tool strategy, or drifting from the user's request?

Rules:
- Silence is the default. Neutral exploration is NOT failure. Repeated reads are
  NOT automatically a loop. If uncertain, answer decision=continue.
- verify: one consequential assumption deserves cheap evidence before more work
  depends on it. nudge: the trajectory is clearly wasting effort.
- Never propose code. One short reason sentence.
- hypothesis: when you flag something, state the unverified assumption the
  agent is relying on, in one clause. Empty string when decision=continue.
- Anything under STALE/RETRACTED below stopped being true. Do not treat it as
  current evidence, and say so if the agent is still building on it.

Trajectory digest (oldest first):
{digest}
{stale}
Respond with the JSON object only."""


@dataclass(frozen=True)
class ReviewerDecision:
    decision: str
    failure_class: str
    reason: str
    confidence: float
    hypothesis: str = ""  # the assumption being flagged; "" when none


def build_digest(records: list[StepRecord], window: int = 40) -> str:
    """Compact, observation-only view of the trajectory tail."""
    lines: list[str] = []
    for record in records[-window:]:
        kind, payload = record.event.kind, record.event.payload
        if kind == "user_prompt":
            text = " ".join(str(payload.get("prompt") or "").split())[:300]
            lines.append(f"step {record.step} USER GOAL: {text}")
        elif kind == "tool_proposal":
            summary = str(payload.get("command") or "")
            if payload.get("patch"):
                summary = f"patch files={payload.get('files')}"
            summary = " ".join(summary.split())[:200]
            lines.append(f"step {record.step} {payload.get('tool')}: {summary}")
        elif kind == "tool_result":
            response = payload.get("tool_response")
            exit_code = response.get("exit_code") if isinstance(response, dict) else None
            if exit_code is not None:
                lines.append(f"step {record.step} result: exit={exit_code}")
        elif kind in ("gate_shadow_block", "gate_fail_open", "gate_block"):
            lines.append(f"step {record.step} GATE {kind}: {payload.get('rule')}")
    return "\n".join(lines) if lines else "(no observable actions recorded)"


def parse_decision(raw: str) -> ReviewerDecision:
    """Validate model output. A reviewer that emits garbage gets CONTINUE —
    an unparseable judgment must never become an intervention."""
    try:
        data = json.loads(raw)
        decision = ReviewerDecision(
            decision=str(data["decision"]),
            failure_class=str(data["failure_class"]),
            reason=str(data["reason"])[:500],
            confidence=float(data["confidence"]),
            hypothesis=str(data.get("hypothesis") or "")[:300],
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return ReviewerDecision("continue", "none", "unparseable reviewer output", 0.0)
    if decision.decision not in DECISIONS or decision.failure_class not in FAILURE_CLASSES:
        return ReviewerDecision("continue", "none", "invalid reviewer output", 0.0)
    if not 0 <= decision.confidence <= 1:
        return ReviewerDecision("continue", "none", "invalid reviewer confidence", 0.0)
    return decision


def codex_runner(model: str, prompt: str) -> str:
    """Ask a model via `codex exec` in an empty scratch dir, read-only sandbox."""
    with tempfile.TemporaryDirectory(prefix="spotter-review-") as scratch:
        schema = Path(scratch) / "schema.json"
        schema.write_text(json.dumps(_SCHEMA))
        answer = Path(scratch) / "answer.txt"
        # "default" delegates model choice to codex. A wrong slug fails with a
        # misleading 400 ("not supported when using Codex with a ChatGPT
        # account") after a multi-minute retry loop, not a fast error — the
        # earlier default gpt-5.3-spark simply did not exist; the real slug is
        # gpt-5.3-codex-spark. Delegating avoids pinning an id that is only
        # valid for some accounts.
        model_args = [] if model in ("", "default") else ["-m", model]
        result = subprocess.run(
            [
                "codex",
                "exec",
                "-C",
                scratch,
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                *model_args,
                "--output-schema",
                str(schema),
                "--output-last-message",
                str(answer),
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            # The reviewer's own codex session must not journal-trigger more
            # reviews of itself.
            env={**os.environ, "SPOTTER_DISABLE": "1"},
        )
        if result.returncode != 0:
            raise RuntimeError(f"codex exec failed: {result.stderr.strip()[:300]}")
        return answer.read_text(encoding="utf-8")


def review(
    records: list[StepRecord],
    model: str,
    *,
    window: int = 40,
    runner: Callable[[str, str], str] = codex_runner,
) -> ReviewerDecision:
    # The ledger is rebuilt from observable outcomes each time, so a premise
    # that later evidence killed cannot quietly stay in the reviewer's view.
    stale = stale_summary(build_state(records))
    stale_block = "\nInvalidated premises:\n" + "\n".join(stale) + "\n" if stale else ""
    prompt = _PROMPT.format(digest=build_digest(records, window), stale=stale_block)
    return parse_decision(runner(model, prompt))
