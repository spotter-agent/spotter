"""Reviewer prompt construction — goal pinning, budget, and untrusted framing.

Three defects in one place, because they are one place (issues #48, #57, #40):

- The goal used to live inside the rolling window, so in a long session driven
  by one instruction it scrolled away and the reviewer judged specification
  drift against nothing.
- The prompt had no size ceiling, so a long window produced an unbounded
  request whose failure would surface as an opaque runtime error.
- Trajectory text is attacker-influenced — it comes from files, dependencies
  and web pages the agent read — and it was interpolated into the prompt as
  plain prose, i.e. as instructions.

The reviewer's own prompt is the last place that should trust its inputs.
"""

import re
from dataclasses import dataclass, field

from spotter.snapshot import StepRecord

# The fence must be improbable in real command output and is stripped from
# untrusted values, so trajectory content cannot close it and start speaking
# as the prompt author.
FENCE = "<<<SPOTTER-TRAJECTORY-DATA>>>"
FENCE_END = "<<<END-SPOTTER-TRAJECTORY-DATA>>>"

DEFAULT_BUDGET = 12000  # characters; ~3k tokens, measured against real journals
_GOAL_CHARS = 600
_COMMAND_CHARS = 200
_MAX_PREMISES = 12
_MAX_CONSTRAINTS = 20

# Phrases that only make sense as an attempt to address the reviewer. They are
# annotated rather than deleted: a suppressed injection is evidence, and the
# reviewer should see that someone tried.
_CONTROL_PHRASES = re.compile(
    r"(?i)\b("
    r"ignore (all |any |the )?(previous|prior|above) instructions?"
    r"|disregard (all |any |the )?(previous|prior|above)"
    r"|you are (now )?(a|an|the)\b"
    r"|new instructions?:"
    r"|system prompt"
    r"|answer (with )?decision\s*=?\s*(continue|nudge|verify)"
    r"|(respond|reply|output) (only )?with"
    r"|spotter[,:]? (ignore|stop|disable)"
    r")"
)


@dataclass(frozen=True)
class Digest:
    """A prompt body plus what the reviewer was actually able to see.

    ``goal_present`` is not cosmetic: a verdict of spec_drift is meaningless
    without it, and the caller enforces that.
    """

    body: str
    goal_present: bool
    truncated: bool
    injection_suspected: bool
    steps_shown: int
    total_steps: int

    def provenance(self) -> dict[str, object]:
        return {
            "goal_present": self.goal_present,
            "truncated": self.truncated,
            "injection_suspected": self.injection_suspected,
            "steps_shown": self.steps_shown,
            "total_steps": self.total_steps,
        }


@dataclass
class _Untrusted:
    """Accumulates whether anything looked like an attempt to steer us."""

    suspected: bool = False
    seen: set[str] = field(default_factory=set)

    def clean(self, text: object, limit: int) -> str:
        flat = " ".join(str(text).split())[:limit]
        # Strip fence markers first: without this, content could close the
        # data region and continue as if it were prompt text.
        flat = flat.replace(FENCE, "").replace(FENCE_END, "")
        if _CONTROL_PHRASES.search(flat):
            self.suspected = True
            flat = _CONTROL_PHRASES.sub("[REDACTED-INSTRUCTION]", flat)
        return flat


def _goal_lines(records: list[StepRecord], untrusted: _Untrusted) -> list[str]:
    """The goal, pinned outside the window.

    The first prompt is the goal; later prompts are amendments and may
    legitimately redefine scope, so both are kept and labelled differently.
    """
    prompts = [r for r in records if r.event.kind == "user_prompt"]
    if not prompts:
        return []
    lines = [
        f"GOAL (step {prompts[0].step}): "
        f"{untrusted.clean(prompts[0].event.payload.get('prompt'), _GOAL_CHARS)}"
    ]
    for record in prompts[1:][-3:]:  # last few amendments only
        lines.append(
            f"AMENDMENT (step {record.step}): "
            f"{untrusted.clean(record.event.payload.get('prompt'), _GOAL_CHARS)}"
        )
    return lines


def _action_line(record: StepRecord, untrusted: _Untrusted) -> str | None:
    kind, payload = record.event.kind, record.event.payload
    if kind == "tool_proposal":
        if payload.get("patch"):
            summary = f"patch files={payload.get('files')}"
        else:
            summary = str(payload.get("command") or "")
        return (
            f"step {record.step} {payload.get('tool')}: {untrusted.clean(summary, _COMMAND_CHARS)}"
        )
    if kind == "tool_result":
        response = payload.get("tool_response")
        exit_code = response.get("exit_code") if isinstance(response, dict) else None
        return f"step {record.step} result: exit={exit_code}" if exit_code is not None else None
    if kind == "external_effect":
        resource = untrusted.clean(payload.get("resource"), _COMMAND_CHARS)
        return (
            f"step {record.step} EXTERNAL EFFECT remains after restart: "
            f"{payload.get('kind')} resource={resource} "
            f"result={payload.get('result')} reversible={payload.get('reversible')}"
        )
    if kind in ("gate_shadow_block", "gate_fail_open", "gate_block"):
        return f"step {record.step} GATE {kind}: {payload.get('rule')}"
    return None


def build(
    records: list[StepRecord],
    *,
    window: int = 40,
    budget: int = DEFAULT_BUDGET,
    stale: list[str] | None = None,
    constraints: list[str] | None = None,
) -> Digest:
    """Assemble the prompt body within ``budget`` characters.

    Priority when truncating, most important first: goal and constraints,
    invalidated premises, then recent actions, then older ones. Spending the
    budget on old actions while dropping the goal is the worst point on the
    curve, and it is what the previous implementation did by accident.
    """
    untrusted = _Untrusted()
    goal = _goal_lines(records, untrusted)
    header: list[str] = []
    if goal:
        header.extend(goal)
    else:
        header.append("GOAL: (none recorded — spec_drift cannot be judged)")
    for constraint in (constraints or [])[:_MAX_CONSTRAINTS]:
        header.append(f"CONSTRAINT: {untrusted.clean(constraint, 200)}")
    # The header is protected from truncation, so it must be bounded at the
    # source: a session that retracts a hundred premises would otherwise blow
    # the budget through the one region nothing trims.
    premises = list(stale or [])
    dropped_premises = max(0, len(premises) - _MAX_PREMISES)
    for premise in premises[-_MAX_PREMISES:]:
        header.append(untrusted.clean(premise, 300))
    if dropped_premises:
        header.append(f"({dropped_premises} older invalidated premises omitted)")

    actions: list[str] = []
    for record in records[-window:]:
        action = _action_line(record, untrusted)
        if action:
            actions.append(action)

    # Actions are trimmed from the oldest end; the header is bounded above so
    # it cannot be the thing that overruns.
    fixed = "\n".join(header)
    truncated = bool(dropped_premises)
    while actions and len(fixed) + len("\n".join(actions)) + len(FENCE) * 2 > budget:
        actions.pop(0)
        truncated = True

    body = (
        fixed
        + "\n"
        + FENCE
        + "\n"
        + ("\n".join(actions) or "(no observable actions)")
        + "\n"
        + FENCE_END
    )
    return Digest(
        body=body,
        goal_present=bool(goal),
        truncated=truncated,
        injection_suspected=untrusted.suspected,
        steps_shown=len(actions),
        total_steps=len(records),
    )
