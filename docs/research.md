# Research

> **Purpose:** track the research ideas Spotter borrows, map them to concrete mechanisms, and keep **design inspiration**, **implemented code**, and **measured evidence** separate.

Spotter is not based on one paper. It combines ideas from runtime supervision, process-level evaluation, online auditing, restart/recovery, verifier-first reasoning, claim/evidence tracking, and harness diagnosis.

The central Spotter claim remains unproven:

> **Runtime intervention improves coding-agent outcomes more than it harms them, at an acceptable cost.**

The standalone-runtime direction in [#66](https://github.com/Bogyie/spotter/issues/66) changes the observation/control substrate used to test that claim. It does not count as evidence for the claim by itself.

---

## 30-second research map

| Work | Main idea Spotter borrows | Spotter mechanism | Current status |
| --- | --- | --- | --- |
| **Wink** | async course correction during coding trajectories | shadow reviewer → future live steer | reviewer implemented; live delivery missing |
| **SWE-PRM** | process-level failure taxonomy | structured candidates/verdicts | partially reflected in reviewer taxonomy |
| **AgentForesight** | early prefix-only auditing | detection timing / first-deviation metrics | measurement design, not fully executed |
| **FailFast-RestartSmart** | stop/restart bad trajectories | `INTERRUPT` / `RESTART` | target only |
| **Calibration Is Not Control** | intervention value matters more than risk score | same-prefix counterfactual experiment | harness implemented; evidence missing |
| **interwhen** | verify deterministic facts outside LLM | deterministic gate / verifier-first design | implemented for selected policies |
| **Grounded Continuation** | claim/evidence invalidation | audit ledger + stale propagation | implemented, observation-limited |
| **AgentProcessBench** | neutral exploration ≠ error | candidates are not verdicts; conservative reviewer | design principle |
| **HarnessFix** | normalize traces; diagnose harness | Trace IR / adapter boundary | partial implementation / target expansion |
| **Refute-or-Promote** | falsification beats consensus | evidence-first VERIFY / empirical probes | design principle |

---

## Current evidence posture

Legend: ✅ implemented · 🟡 partial/shadow · 🧪 needs experiment · ❌ missing

| Claim / mechanism | Status | What the evidence actually says |
| --- | --- | --- |
| real trajectory collection | ✅ | Spotter records real Codex Hook trajectories |
| deterministic pre-action policy | ✅ | implemented and hardened with real false-positive cases |
| snapshot/fork/replay | ✅ | machinery exists and can branch from recorded prefixes |
| model-backed semantic judgment | ✅ shadow | reviewer produces plausible structured verdicts but does not affect Main |
| claim/evidence ledger | 🟡 | works only where the observation surface exposes usable outcomes |
| counterfactual experiment framework | ✅ | machinery exists; not enough executed ground-truth runs |
| ground-truth task set | ❌ | major current evaluation blocker |
| positive intervention advantage | ❌ | not established |
| event-driven reviewer dispatch | ❌ | current reviewer trigger is cadence-based |
| live `VERIFY / NUDGE` delivery | ❌ | target: `turn/steer` |
| `INTERRUPT / RESTART` | ❌ | target only |
| App Server observation/control | 🧪 | P0 architecture PoC required |

The hook-collected corpus exposed a concrete observation limit for the audit ledger: directly usable outcomes were present in only a small fraction of real tool results. The App Server migration must **re-measure** that ceiling instead of assuming it is permanent.

---

## What evidence would change a design decision?

This project should use research questions to make engineering decisions, not just accumulate papers.

| Decision | Evidence required |
| --- | --- |
| Replace periodic review with signal-triggered review | similar/better detection with fewer reviewer calls and acceptable miss rate |
| Enable live NUDGE by default | positive intervention advantage, bounded harm, wrong-nudge resistance |
| Keep deterministic BLOCK active | near-deterministic precision with measured blind spots and low latency |
| Add INTERRUPT | high precision in shadow mode + evidence that steer is insufficient for targeted cases |
| Add automatic RESTART | restart advantage over continue/interrupt + side-effect-safe recovery model |
| Use a different reviewer model/family | measured reduction in correlated failures after controlling for model strength |
| Keep App Server architecture | richer observation/control and lower hot-path cost justify lifecycle complexity |

---

# 1. Wink — asynchronous course correction for coding agents

**Wink: Recovering from Misbehaviors in Coding Agents**  
Rahul Nanda et al., 2026  
https://arxiv.org/abs/2602.17037

## Why it matters

Wink is the closest direct precedent to Spotter's product idea: observe a coding agent while it works and inject targeted guidance when the execution trajectory begins to misbehave.

The useful framing is not “another reviewer model.” It is **asynchronous runtime course correction**.

## What Spotter borrows

- persistent online observation instead of post-hoc-only review;
- semantic review running mostly outside the main execution path;
- compact coding-agent failure categories;
- targeted guidance rather than a full competing implementation;
- the principle that the observer should usually stay silent.

## Where Spotter differs or extends

| Extension | Status |
| --- | --- |
| deterministic pre-action blocking | implemented in prototype |
| independent claim/evidence state | implemented, observation-limited |
| same-prefix causal evaluation | experiment harness implemented |
| event-driven cheap signal layer | not implemented |
| live steer delivery | not implemented |
| interrupt/restart | not implemented |
| explicit external side-effect handling | not implemented |

## Architecture consequence

Semantic supervision should stay asynchronous. In the target architecture:

```text
App Server event
  ↓
spotterd signal
  ↓
reviewer in parallel
  ↓
verdict still relevant?
  ├─ yes → live steer
  └─ no  → stale/discard
```

This is one reason to move semantic supervision out of per-hook processes.

---

# 2. SWE-PRM — process reward models for SWE trajectory correction

**When Agents go Astray: Course-Correcting SWE Agents with PRMs**  
Shubham Gandhi et al., 2025  
https://arxiv.org/abs/2509.02360

## Why it matters

SWE-PRM treats software-engineering agent failures as **trajectory-level process problems**, including redundant exploration, loops, premature stopping, and other execution inefficiencies.

## What Spotter borrows

- failure categories attached to execution process rather than final artifact only;
- inference-time correction without retraining Main;
- compact interpretable feedback;
- evaluation of efficiency as well as final success.

## Design consequence

Reviewer output should remain structured and testable.

Prefer:

```json
{
  "candidate": "POSSIBLE_SPEC_DRIFT",
  "decision": "VERIFY",
  "target": "constraint:C2",
  "reason": "The current edit changes files outside the requested scope.",
  "probe": "Re-read the explicit scope constraint before continuing."
}
```

rather than unrestricted reviewer prose.

Structured categories make it possible to measure precision, misses, harm, latency, and intervention advantage by failure/action class.

---

# 3. AgentForesight — online auditing and early failure prediction

**AgentForesight: Online Auditing for Early Failure Prediction in Multi-Agent Systems**  
Boxuan Zhang et al., 2026  
https://arxiv.org/abs/2605.08715

## Why it matters

AgentForesight emphasizes **prefix-only** judgment. A runtime auditor cannot rely on knowing what happens later; it must decide from the trajectory that is visible now.

## What Spotter borrows

- evaluate from the current prefix, not hindsight;
- separate “eventually detected” from “detected early enough to matter”;
- measure the first point where intervention becomes warranted/possible.

## Metrics implied by this work

Spotter should measure:

```text
first warranted intervention point
first observable intervention point
actual detection point
reviewer decision point
actual delivery point
```

From those timestamps/steps we can derive:

- detection delay;
- reviewer latency;
- delivery latency;
- wasted tool calls/actions;
- stale intervention rate.

A reviewer that is correct twenty actions late may be useless as a controller.

---

# 4. FailFast-RestartSmart — stop bad trajectories and restart cleanly

**Fail-Fast, Restart-Smart: Early Failure Prediction and Restart for SWE Agentic Tasks**  
Chenyu Wang et al., 2026  
https://arxiv.org/abs/2608.03222

## Why it matters

This line of work motivates a control primitive stronger than guidance: sometimes continuing inside the same reasoning context is worse than abandoning it and starting again from selected state.

## What Spotter borrows

- `RESTART` is distinct from `NUDGE`;
- reasoning context can become contaminated by stale assumptions;
- useful repository state can be preserved without preserving the failed reasoning history;
- strong actions require stricter false-positive budgets.

## Preconditions before Spotter can implement RESTART responsibly

```text
verified-state representation
last-sound-state / branch-point model
snapshot/checkpoint lineage
explicit retained artifacts
external side-effect ledger
fresh continuation mechanism
```

The daemon architecture makes these boundaries easier to represent, but does not solve them automatically.

## Evaluation requirement

Compare from the same prefix:

```text
continue
vs
NUDGE
vs
INTERRUPT + replan
vs
RESTART from verified state
```

RESTART should not exist simply because it is technically possible.

---

# 5. Calibration Is Not Control — intervention advantage

**Calibration Is Not Control: Why LLM-Agent Oversight Needs Intervention**  
Chubin Zhang et al., 2026  
https://arxiv.org/abs/2606.21399

## Why it matters

Predicting “this trajectory is likely to fail” is not the same as knowing that an intervention will help.

Two prefixes with similar failure probability can have very different control value:

```text
Prefix A
  high risk, but one focused VERIFY may recover it

Prefix B
  high risk, but an intervention may only distract Main
```

## Spotter consequence

The decision target should be closer to:

> **Expected outcome if we take action X now minus expected outcome if we continue.**

That leads to action-conditioned evaluation:

```text
Advantage(VERIFY | prefix)
Advantage(NUDGE  | prefix)
Advantage(BLOCK  | proposal)
Advantage(INTERRUPT | prefix)
```

## Current status

Spotter already has same-prefix counterfactual experiment machinery. The missing pieces are:

- mechanically scored tasks;
- enough executed pairs;
- replay/fork fidelity measurement;
- cost/timing accounting;
- harm classification.

This is one of the largest gaps between “implemented system” and “proven product value.”

---

# 6. interwhen — verifier-first runtime supervision

**interwhen: A Generalizable Framework for Verifiable Reasoning with Test-time Monitors**  
Vishak K. Bhat et al., 2026  
https://arxiv.org/abs/2602.11202  
Code: https://github.com/microsoft/interwhen

## Why it matters

Some properties can be verified directly; they do not need broad model judgment.

## What Spotter borrows

- extract deterministic properties into executable checks;
- prefer external/verifiable evidence over debate;
- keep semantic model calls selective;
- do not force all reasoning into a rigid formal schema.

## Architecture mapping

```text
PreToolUse synchronous gate
  deterministic
  bounded
  no model call

Async semantic reviewer
  ambiguity
  context-sensitive
  only when cheap signals justify it
```

## Product implication

One useful metric is **semantic-review avoidance**: what fraction of useful supervision decisions can be resolved without an LLM call?

That directly affects latency and cost.

---

# 7. Grounded Continuation — claim/evidence dependency tracking

**Grounded Continuation: A Linear-Time Runtime Verifier for LLM Conversations**  
Qisong He, Yi Dong, Xiaowei Huang, 2026  
https://arxiv.org/abs/2605.14175

## Why it matters

The core mechanism maps naturally to coding trajectories: hypotheses depend on evidence, and when evidence is retracted, downstream conclusions should become stale.

## Coding example

```text
E1: timeout correlates with high concurrency
        ↓ supports
H1: Redis pool exhaustion causes timeout
        ↓ motivates
P1: change Redis pool configuration

new evidence
E2: stack trace points to upstream HTTP client

→ H1 stale
→ P1 requires revalidation
```

## Current Spotter status

The prototype implements a typed claim/evidence audit ledger with transitive stale propagation for observable outcomes.

The practical limitation is observation coverage. If the runtime does not expose reliable outcomes, the ledger correctly remains silent rather than inventing pass/fail.

## App Server hypothesis

The App Server migration creates a measurable question:

> Does richer event/result visibility materially increase the fraction of useful claims that can be grounded and invalidated mechanically?

That must be measured after P4.

---

# 8. AgentProcessBench — neutral exploration is not failure

**AgentProcessBench: Diagnosing Step-Level Process Quality in Tool-Using Agents**  
Shengda Fan et al., 2026  
https://arxiv.org/abs/2603.14465  
Code/data: https://github.com/RUCBM/AgentProcessBench

## Why it matters

Real tool-use contains steps that are neither clearly useful nor clearly wrong. Exploration may look inefficient from a short window but still be justified.

## What Spotter borrows

- neutral/uncertain behavior must exist in the reviewer worldview;
- repetition is a signal, not an automatic verdict;
- early intervention should be conservative around ambiguous exploration.

## Signal-engine consequence

Bad:

```text
same file read three times
→ EXPLORATION_LOOP
→ NUDGE
```

Better:

```text
same semantic read repeated
+ no frontier expansion
+ no new evidence recorded
→ POSSIBLE_EXPLORATION_LOOP
→ reviewer decides whether intervention is warranted
```

This is why P5 separates cheap signal generation from semantic verdicts.

---

# 9. HarnessFix — normalize traces and diagnose the harness too

**From Failed Trajectories to Reliable LLM Agents: Diagnosing and Repairing Harness Flaws**  
Mengzhuo Chen et al., 2026  
https://arxiv.org/abs/2606.06324

## Why it matters

Runtime failures can come from Main, but also from the harness/observer itself. A normalized representation helps reason across both.

## What Spotter borrows

- normalize backend-specific runtime streams into Trace IR;
- preserve provenance from normalized records back to raw events;
- distinguish Main failure from Spotter/harness failure;
- eventually diagnose recurring harness problems rather than endlessly nudging Main.

## Architecture consequence

The App Server migration must not replace one coupling with another.

Bad:

```text
Codex App Server JSON fields
  scattered through GateEngine / Reviewer / Metrics / Replay
```

Target:

```text
Codex raw events
  ↓
CodexAdapter
  ↓
Trace IR
  ↓
Spotter core
```

This keeps another coding-agent adapter possible later.

---

# 10. Refute-or-Promote — falsification and empirical gates

**Refute-or-Promote: An Adversarial Stage-Gated Multi-Agent Review Methodology for High-Precision LLM-Assisted Defect Discovery**  
Abhinav Agarwal, 2026  
https://arxiv.org/abs/2604.19049

## Why it matters

Multiple models can agree on a wrong claim. Empirical evidence can eliminate the claim cheaply.

## What Spotter borrows

- try to falsify consequential hypotheses;
- use a different reviewer/model context as a source of independent challenge, not truth;
- promote intervention only after sufficient evidence;
- prefer one decisive test over model consensus.

## Product consequence

A useful `VERIFY` message should propose the smallest discriminating probe.

```text
Weak:
"I think the Redis hypothesis may be wrong."

Better:
"Before editing Redis settings, inspect the timeout stack trace; no observed result currently ties the timeout to Redis."
```

---

# Synthesis: how the pieces fit together

```text
Wink
  async coding-agent supervision
        +
SWE-PRM
  process failure taxonomy
        +
AgentForesight
  early prefix-only detection
        +
FailFast-RestartSmart
  interrupt / restart
        +
Calibration Is Not Control
  intervention advantage
        +
interwhen
  verifier-first deterministic checks
        +
Grounded Continuation
  claim/evidence invalidation
        +
AgentProcessBench
  neutral exploration awareness
        +
HarnessFix
  Trace IR and harness attribution
        +
Refute-or-Promote
  falsification before promotion
```

Spotter adds an engineering hypothesis underneath this research synthesis:

```text
rich observation/control stream
        +
long-lived independent state
        +
selective async semantic review
        +
bounded synchronous enforcement
```

may be a better substrate than reconstructing state inside independent Hook processes.

That engineering hypothesis must be measured too. A richer runtime is a regression if it adds more operational cost/fragility than the supervision benefit it enables.

---

# Research questions

## RQ1 — Observation surface

How much does moving from Hook-collected events to App Server events change:

- directly visible tool outcomes;
- goal/constraint availability;
- failure observability ceiling;
- event provenance;
- timing precision;
- missing/duplicated events?

**Decision affected:** whether App Server should remain the primary observation plane.

## RQ2 — Intervention timing

How early can/will Spotter identify a warranted intervention?

Compare:

- periodic review;
- event-triggered review;
- deterministic pre-action gate.

Measure wasted actions, not only final detection correctness.

## RQ3 — Event-driven triggering

Can cheap signals reduce reviewer calls without materially increasing misses?

Sweep:

```text
signal threshold
fallback cadence
reviewer calls/session
detection delay
precision
sampled miss rate
reviewer tokens
```

**Decision affected:** P5 signal policy.

## RQ4 — Intervention policy

When is `VERIFY` better than `NUDGE`? When does `INTERRUPT` outperform soft steering? When does `RESTART` outperform continuation?

**Decision affected:** intervention ladder thresholds.

## RQ5 — Reviewer harm and wrong-nudge susceptibility

Two directions matter:

1. Spotter interrupts a trajectory that would have succeeded.
2. Main obeys plausible but wrong Spotter guidance instead of refuting it with evidence.

Both should gate live injection.

## RQ6 — Model diversity

Does a different reviewer family reduce correlated blind spots after controlling for model capability?

Compare:

```text
same model + isolated context
different model + isolated context
smaller specialized reviewer
```

Do not confuse “different” with “weaker” or “stronger”.

## RQ7 — Deterministic vs semantic supervision

What fraction of useful supervision can be resolved by executable checks before an LLM is required?

**Decision affected:** cost/latency budget and which policies belong in GateEngine.

## RQ8 — Runtime overhead

Does the standalone runtime reduce hot-path cost enough to justify lifecycle complexity?

Measure:

```text
Hook p50/p95/p99 before/after
reviewer dispatch latency
App Server processing overhead
idle/active daemon CPU/memory
reconnect/recovery latency
additional failure modes
```

## RQ9 — Replay/fork fidelity

How noisy is the counterfactual instrument itself?

Measure:

- identical-arm noise floor;
- repeated continuation similarity;
- environmental failure rate;
- detached-worktree effects.

Intervention deltas smaller than the instrument noise floor should not be overinterpreted.

## RQ10 — User experience

Correct supervision can still be a bad product if it is confusing or annoying.

Collect/measure:

- dismiss/disagree signals;
- overridden blocks;
- perceived usefulness vs disruption;
- whether users can tell healthy silence from disconnected silence;
- whether setup/daemon lifecycle becomes operationally burdensome.

---

# Evaluation matrix

Eventually compare at least:

```text
A. Vanilla coding agent

B. Periodic async reviewer
   fixed cadence, soft guidance

C. Event-driven Spotter
   cheap candidates + semantic reviewer

D. Spotter + deterministic gate
   event-driven + PreToolUse enforcement

E. Full Spotter
   gate + steer + stronger interrupt/recovery primitives
```

Hold the runtime substrate constant where possible when comparing supervision policy, otherwise architecture changes become a confound.

---

# Primary metrics

| Dimension | Metrics |
| --- | --- |
| Outcome | task resolution, regression rate |
| Main efficiency | main tokens, tool calls, repeated actions, elapsed/tool time |
| Spotter cost | reviewer tokens/calls, CPU/memory, storage growth |
| Observation | event coverage, observable outcome rate, missing goal/constraint rate, disconnect rate |
| Detection | precision, FP rate, sampled miss rate/recall, abstention/blind spots |
| Intervention | advantage, harm rate, recovery rate, ignored rate |
| Timing | detection delay, reviewer latency, delivery latency, stale rate, wasted actions |
| Recovery | success after nudge/interrupt/restart, useful work retained, external effects unreverted |
| Operations | Hook latency, reconnect/recovery time, upgrade/migration failure rate |

---

# Evidence gates

Strong mechanisms should not become default behavior merely because they exist.

```text
VERIFY
  lowest semantic intervention cost
  can tolerate uncertainty if it requests concrete evidence

NUDGE
  require conservative precision + positive harm/advantage evidence

BLOCK
  deterministic / near-deterministic only
  measure blind spots separately from FP

INTERRUPT
  shadow first
  very high precision/harm threshold

RESTART
  strongest control
  requires restart advantage + side-effect-safe state model
```

---

# Current top evidence gaps

1. **No ground-truth task set** large enough to run meaningful counterfactual experiments.
2. **No measured positive intervention advantage** for Spotter guidance.
3. **No sampled miss-rate/recall estimate** for semantic detector behavior.
4. **No App Server observability measurement** yet.
5. **No replay/fork noise-floor measurement** supporting causal interpretation.
6. **No live wrong-nudge/sycophancy evaluation** before injection.
7. **No full runtime overhead measurement** for the target architecture.

These gaps should be treated as first-class roadmap items, not post-launch research cleanup.

For current implementation state, see [Status](status.md). For implementation sequence, see [Roadmap](roadmap.md). For runtime details, see [Architecture](architecture.md).
