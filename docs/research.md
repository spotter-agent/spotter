# Research

> **Purpose:** track Spotter's hypotheses, evidence state, research questions, and experiments. Detailed paper/system summaries live in [Reference](reference.md).

Spotter's central claim remains unproven:

> **Runtime intervention improves coding-agent outcomes more than it harms them, at an acceptable cost.**

The standalone-runtime direction described in [Architecture](architecture.md) and tracked through the [Roadmap](roadmap.md) changes the observation/control substrate used to test that claim. It does not count as evidence for the claim by itself.

A second, more specific hypothesis now matters just as much:

> **Semantic supervision can be moved largely off Main's critical path by keeping an independent Navigator active and evaluating likely near-future decisions before Main reaches them.**

The literature and implementation precedents behind these hypotheses are catalogued in [Reference](reference.md), including Wink, SWE-PRM, AgentForesight, ECLoop, Shepherd, NVIDIA NeMo Relay, and OpenClaw Codex supervision.

---

## 30-second research map

| Research idea | Reference precedent | Spotter mechanism | Current status |
| --- | --- | --- | --- |
| asynchronous course correction | Wink / Shepherd | shadow reviewer → future live steer | reviewer implemented; live delivery missing |
| process-level failure taxonomy | SWE-PRM | structured candidates/verdicts | partially reflected in reviewer taxonomy |
| prefix-only early auditing | AgentForesight | detection timing / first-deviation metrics | measurement design, not fully executed |
| evidence-conditioned action gating | ECLoop / interwhen | bounded deterministic/evidence gates | selected deterministic gates implemented; broader evidence gates unproven |
| stop/restart bad trajectories | FailFast-RestartSmart | `INTERRUPT` / `RESTART` | target only |
| intervention advantage | Calibration Is Not Control | same-prefix counterfactual experiment | dev-v2 qualification was 3/3 tie-success; causal evidence missing |
| claim/evidence invalidation | Grounded Continuation | audit ledger + stale propagation | implemented, observation-limited |
| neutral exploration awareness | AgentProcessBench | candidates are not verdicts | design principle |
| normalized runtime traces | HarnessFix / NeMo Relay | Trace IR / adapter boundary | partial implementation / target expansion |
| falsification before guidance | Refute-or-Promote | evidence-first `VERIFY` | design principle |
| native Codex observation/control | OpenClaw Codex supervision | App Server adapter / steer / interrupt | control PoC passed; production event routing partial |
| speculative supervision | no complete precedent identified | parallel lookahead + precomputed intervention | research/target architecture |

See [Reference](reference.md) for what each precedent actually implements and which patterns Spotter intends to study without taking runtime dependencies.

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
| counterfactual experiment framework | ✅ | frozen/resumable machinery completed its first 6-arm development qualification run |
| ground-truth task set | 🟡 | six synthetic frozen tasks across five failure families; external/ecosystem breadth remains missing |
| positive intervention advantage | ❌ | not established |
| event-driven reviewer dispatch | ❌ | current reviewer trigger is cadence-based |
| live `VERIFY / NUDGE` delivery | ❌ | target: `turn/steer` |
| `INTERRUPT / RESTART` | ❌ | target only |
| App Server observation/control viability | ✅ PoC | #78 proved a Spotter-managed external App Server can be shared by the real TUI and Spotter and can steer the active turn |
| production App Server observation path | 🟡 | async client/control surface exists; normalized event routing, identity reconciliation, and reconnect remain |
| speculative supervision | ❌ | no measured lookahead/prediction/intervention pipeline yet |

The first [dev-v2 multi-task run](experiments/dev-v2-first-run.md) completed all six arms and produced
three tie-success pairs. This qualifies the task instrument; it is a small development-set null result,
not positive intervention evidence. The held-out validation set remains unexecuted.

The Hook-collected corpus exposed a concrete observation limit for the audit ledger: directly usable outcomes were present in only a small fraction of real tool results. The App Server migration must **re-measure** that ceiling rather than assume it is permanent.

---

## What evidence would change a design decision?

This project should use research questions to make engineering decisions, not just accumulate papers.

| Decision | Evidence required |
| --- | --- |
| Replace periodic review with signal-triggered review | similar/better detection with fewer reviewer calls and acceptable miss rate |
| Add broader evidence-conditioned gates | measurable prevention of premature commitment with low false blocking and low latency |
| Enable live NUDGE by default | positive intervention advantage, bounded harm, wrong-nudge resistance |
| Keep deterministic BLOCK active | near-deterministic precision with measured blind spots and low latency |
| Add INTERRUPT | high precision in shadow mode + evidence that steer is insufficient for targeted cases |
| Add automatic RESTART | restart advantage over continue/interrupt + side-effect-safe recovery model |
| Use a different reviewer model/family | measured reduction in correlated failures after controlling for model strength |
| Keep App Server architecture | richer observation/control and lower hot-path cost justify lifecycle complexity |
| Build speculative lookahead | useful prediction/decision hit rate and positive supervision lead time justify additional inference cost |

---

# Synthesis

The references suggest several pieces independently work:

```text
runtime observation
        +
selective semantic supervision
        +
evidence-conditioned gating
        +
native steer / interrupt control
        +
reversible / counterfactual evaluation
```

Spotter's engineering hypothesis is that they should be composed around a long-lived independent runtime:

```text
rich App Server observation/control stream
        +
long-lived independent state
        +
selective semantic review
        +
speculative lookahead off Main's critical path
        +
bounded synchronous enforcement
```

rather than reconstructing state inside isolated per-Hook processes or synchronously invoking a model at every tool boundary.

A richer runtime is still a regression if it adds more operational cost, latency, or fragility than the supervision benefit it enables.

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

---

## RQ2 — Intervention timing

How early can/will Spotter identify a warranted intervention?

Compare:

- periodic review;
- event-triggered review;
- evidence/deterministic pre-action gates;
- speculative lookahead.

Measure wasted actions, not only final detection correctness.

Useful timestamps include:

```text
first warranted intervention point
first observable intervention point
prediction / review start
reviewer decision point
actual delivery point
```

From them derive detection delay, reviewer latency, supervision lead/lag, wasted actions, and stale-intervention rate.

---

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

**Decision affected:** Detect-stage signal policy.

---

## RQ4 — Intervention policy

When is `VERIFY` better than `NUDGE`? When does `INTERRUPT` outperform soft steering? When does `RESTART` outperform continuation?

Also distinguish semantic guidance from evidence-conditioned delay/blocking: an unsupported action may need more evidence rather than a competing implementation suggestion.

**Decision affected:** intervention ladder thresholds.

---

## RQ5 — Reviewer harm and wrong-nudge susceptibility

Two directions matter:

1. Spotter interrupts a trajectory that would have succeeded.
2. Main obeys plausible but wrong Spotter guidance instead of refuting it with evidence.

Both should gate live injection.

---

## RQ6 — Model diversity

Does a different reviewer family reduce correlated blind spots after controlling for model capability?

Compare:

```text
same model + isolated context
different model + isolated context
smaller specialized reviewer
```

Do not confuse “different” with “weaker” or “stronger”.

---

## RQ7 — Deterministic/evidence gates vs semantic supervision

What fraction of useful supervision can be resolved by executable checks or explicit evidence conditions before an LLM is required?

This question is strengthened by ECLoop's result that evidence-conditioned execution can improve SWE outcomes without model retraining. Spotter still needs to determine whether similar conditions generalize beyond premature commitment and whether they coexist cleanly with an independent Navigator.

**Decision affected:** cost/latency budget and which policies belong in the synchronous fast path.

---

## RQ8 — Runtime overhead

Does the standalone runtime reduce hot-path cost enough to justify lifecycle complexity?

Measure:

```text
Hook p50/p95/p99 before/after
App Server event processing overhead
reviewer dispatch latency
steer delivery latency
idle/active daemon CPU/memory
reconnect/recovery latency
additional failure modes
```

---

## RQ9 — Replay/fork fidelity

How noisy is the counterfactual instrument itself?

Measure:

- identical-arm noise floor;
- repeated continuation similarity;
- environmental failure rate;
- detached-worktree effects.

The harness now has an explicit neutral-noise mode: each repeated pair receives the same control
prompt after shared-prefix/environment preflight, and the summary separates mechanical outcome
disagreement from environment mismatch and infrastructure failure. No representative real-prefix
run has established the rate yet. New Hook-observed sessions pin a baseline snapshot at
`SessionStart`, removing the previous pre-mutation read-only blind region without snapshotting every
observation; historical sessions whose snapshot objects were pruned remain non-recoverable.
The read-only `fork-coverage` report now quantifies that distinction per proposal, including earliest
exact and pre-mutation coverage, without mistaking missing historical Git objects for valid anchors.
The first [historical coverage baseline](experiments/fork-coverage-baseline.md) found 0/1,246 clean
exact proposals (including 0/120 pre-mutation), so neutral execution on that legacy sample is a
documented no-go rather than a fabricated zero-noise result.
The first [fresh identical-arm run](experiments/fork-neutral-first-run.md) then observed 0/3
mechanical disagreements, 0/3 environment mismatches, and 0/6 infrastructure failures at one exact
prefix. This validates the path but is too narrow to establish the representative noise bound.

Intervention deltas smaller than the instrument noise floor should not be overinterpreted.

---

## RQ10 — User experience

Correct supervision can still be a bad product if it is confusing or annoying.

Collect/measure:

- dismiss/disagree signals;
- overridden blocks;
- perceived usefulness vs disruption;
- whether users can tell healthy silence from disconnected silence;
- whether setup/daemon lifecycle becomes operationally burdensome;
- whether Spotter installation changes the perceived native Codex interaction or latency profile.

---

## RQ11 — Speculative supervision and critical-path removal

Can Spotter predict likely near-future decisions/actions early enough to finish semantic evaluation before Main reaches the decision boundary?

Measure:

```text
prediction horizon
next-action / next-decision hit rate
supervision lead time
supervision lag
precomputed-decision hit rate
stale prediction rate
intervention precision
added wall-clock latency
```

Compare at least:

```text
reactive reviewer
  inference starts after signal/action

async reviewer
  inference runs beside Main from current state

speculative supervisor
  likely future branches are predicted and evaluated ahead of Main
```

**Decision affected:** whether speculative lookahead is worth its additional token/compute cost and whether it can keep semantic supervision effectively off Main's critical path.

---

# Evaluation matrix

Eventually compare at least:

```text
A. Vanilla coding agent

B. Periodic async reviewer
   fixed cadence, soft guidance

C. Event-driven Spotter
   cheap candidates + semantic reviewer

D. Evidence-gated Spotter
   deterministic/evidence conditions + event-driven semantic review

E. Full intervention Spotter
   gate + steer + stronger interrupt/recovery primitives

F. Speculative Spotter
   continuous lookahead + precomputed interventions
```

Hold the runtime substrate constant where possible when comparing supervision policy, otherwise architecture changes become a confound.

A particularly important comparison is **same additional inference budget, different timing**:

```text
post-hoc / reactive review
vs
parallel speculative supervision
```

If Spotter only beats Vanilla because it spends more model compute, the pair-supervision claim remains weak.

---

# Primary metrics

| Dimension | Metrics |
| --- | --- |
| Outcome | task resolution, regression rate |
| Main efficiency | main tokens, tool calls, repeated actions, elapsed/tool time |
| Spotter cost | reviewer tokens/calls, speculative tokens, CPU/memory, storage growth |
| Observation | event coverage, observable outcome rate, missing goal/constraint rate, disconnect rate |
| Detection | precision, FP rate, sampled miss rate/recall, abstention/blind spots |
| Intervention | advantage, harm rate, recovery rate, ignored rate, evidence-gate false blocking |
| Timing | detection delay, reviewer latency, delivery latency, supervision lead/lag, precomputed-decision hit rate, stale rate, wasted actions |
| Recovery | success after nudge/interrupt/restart, useful work retained, external effects unreverted |
| Operations | Hook latency, App Server overhead, reconnect/recovery time, upgrade/migration failure rate |
| Product UX | native-workflow deviation, perceived latency, setup/daemon friction |

---

# Evidence gates

Strong mechanisms should not become default behavior merely because they exist.

```text
VERIFY
  lowest semantic intervention cost
  can tolerate uncertainty if it requests concrete evidence

NUDGE
  require conservative precision + positive harm/advantage evidence

EVIDENCE GATE / BLOCK
  deterministic or strongly grounded condition
  bounded synchronous work only
  measure false blocking and blind spots separately

INTERRUPT
  shadow first
  very high precision/harm threshold

RESTART
  strongest control
  requires restart advantage + side-effect-safe state model
```

---

# Current top evidence gaps

1. **No broad ground-truth task set** beyond the six-task synthetic v2 foundation; ecosystem and
   sample-size coverage remain too small for a meaningful intervention claim.
2. **No measured positive intervention advantage** for Spotter guidance.
3. **No sampled miss-rate/recall estimate** for semantic detector behavior.
4. **No post-migration App Server observability-ceiling measurement** yet. The coverage taxonomy,
   value-free source audit, and conformance corpus exist, but the current snapshot has no App Server
   sessions or labeled failures; see the [observability baseline](observability-baseline.md).
5. **No replay/fork noise-floor measurement** supporting causal interpretation.
6. **No live wrong-nudge/sycophancy evaluation** before injection.
7. **No full runtime overhead measurement** for the target architecture.
8. **No speculative-supervision measurement** showing that useful Navigator inference can finish ahead of Main often enough to remove semantic review from the critical path.
9. **No Spotter-specific evidence-gating experiment** establishing whether ECLoop-like premature-commitment controls generalize to Spotter's broader supervision model.

These gaps should be treated as first-class roadmap items, not post-launch research cleanup.

For prior work and implementation precedents, see [Reference](reference.md). For current implementation state, see [Status](status.md). For implementation sequence, see [Roadmap](roadmap.md). For runtime details, see [Architecture](architecture.md).
