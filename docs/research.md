# Research

Spotter is not based on a single paper. It combines ideas from recent work on runtime supervision, process-level evaluation, online auditing, restart/recovery, verifiable reasoning, claim/evidence tracking, and harness diagnosis.

This document has two purposes:

1. track the ideas Spotter borrows from prior work;
2. keep a clear boundary between **design inspiration**, **implemented mechanisms**, and **measured evidence**.

That distinction matters. Spotter already implements several mechanisms, but its central causal claim—**that runtime intervention improves coding-agent outcomes more than it harms them**—is not yet established.

The standalone-runtime direction in [#66](https://github.com/Bogyie/spotter/issues/66) does not change the research hypothesis. It changes the observation/control substrate so the hypothesis can be tested with better state, timing, and intervention primitives.

## Current evidence posture

Before the literature survey, an honest status summary:

| Claim / mechanism | Status |
| --- | --- |
| hook-level trajectory collection | implemented |
| deterministic pre-action gates | implemented, default-safe/shadow posture |
| Git snapshot/fork/replay machinery | implemented |
| model-backed reviewer judgment | implemented in shadow mode |
| claim/evidence ledger | implemented for observable outcomes |
| counterfactual experiment machinery | implemented |
| ground-truth task set and enough executed experiments | **missing** |
| positive intervention advantage | **not established** |
| live NUDGE/VERIFY delivery | **not implemented** |
| event-driven signal → reviewer path | **not implemented** |
| INTERRUPT / RESTART | **not implemented** |
| App Server-based observation/control | **target architecture, not implemented** |

The current hook-collected corpus also exposed an observation limitation: only a small fraction of real tool results carried directly usable outcomes for the audit ledger. The target App Server migration should re-measure that ceiling rather than assume it is permanent.

---

## 1. Wink — asynchronous course correction for coding agents

**Wink: Recovering from Misbehaviors in Coding Agents**  
Rahul Nanda et al., 2026  
https://arxiv.org/abs/2602.17037

### Why it matters

Wink is the closest direct precedent for Spotter's core product idea: observe a coding agent while it works and inject a targeted course correction when its trajectory begins to misbehave.

Its framing is important for Spotter because it treats many coding-agent failures as **process failures**, not merely bad final diffs.

### What Spotter borrows

- persistent online observation rather than post-hoc-only review
- asynchronous course correction during an active coding trajectory
- a compact taxonomy of coding-agent misbehavior
- the principle that the observer should normally stay out of Main's way

### Where Spotter intends to differ

| Extension | Spotter status |
| --- | --- |
| deterministic pre-action blocking | implemented in the prototype |
| independent claim/evidence state | implemented, limited by observation coverage |
| stronger interruption/restart | not implemented |
| action-conditioned intervention choice | not implemented as a complete policy |
| explicit external side-effect handling | not implemented |

The project should not present the unimplemented rows as delivered differentiators.

### Architecture implication

Wink reinforces the decision that semantic supervision should be asynchronous. In the target architecture, App Server events feed `spotterd`; the reviewer runs in parallel; soft guidance is delivered only when ready and still relevant.

---

## 2. SWE-PRM — process reward models for SWE trajectory correction

**When Agents go Astray: Course-Correcting SWE Agents with PRMs**  
Shubham Gandhi et al., 2025  
https://arxiv.org/abs/2509.02360

### Why it matters

SWE-PRM treats software-engineering failures as trajectory-level problems and applies inference-time process supervision to redundant exploration, loops, premature stopping, and related inefficiencies.

### What Spotter borrows

- process-level failure categories
- inference-time correction without retraining Main
- interpretable feedback rather than a full competing implementation
- evaluation of trajectory efficiency as well as final success

### Design lesson

Taxonomy-guided feedback is more testable than unconstrained reviewer chatter. Spotter should keep reviewer outputs structured around explicit candidate failures, targets, probes, and control actions.

---

## 3. AgentForesight — online auditing and early failure prediction

**AgentForesight: Online Auditing for Early Failure Prediction in Multi-Agent Systems**  
Boxuan Zhang et al., 2026  
https://arxiv.org/abs/2605.08715

### Why it matters

AgentForesight frames auditing as a prefix-only problem: decide from the trajectory so far whether a meaningful failure has become visible, without relying on future hindsight.

### What Spotter borrows

- prefix-only judgment
- timing as a first-class objective
- the distinction between eventually detecting a failure and detecting it early enough to matter

### Design lesson

Spotter should report **regret / wasted actions before intervention** and intervention latency. A detector that is always correct twenty steps too late is not necessarily useful.

The daemon/App Server architecture improves the ability to measure real wall-clock timing because events and intervention delivery can carry explicit timestamps instead of only journal order.

---

## 4. FailFast-RestartSmart — stop bad trajectories and restart cleanly

**Fail-Fast, Restart-Smart: Early Failure Prediction and Restart for SWE Agentic Tasks**  
Chenyu Wang et al., 2026  
https://arxiv.org/abs/2608.03222

### Why it matters

This line of work asks whether a trajectory can become bad enough that continuing inside the same reasoning context is less useful than starting a fresh rollout while deliberately preserving useful external state.

### What Spotter borrows

- `RESTART` as a primitive distinct from `NUDGE`
- the possibility that reasoning context becomes contaminated
- preservation of useful repository state without blindly preserving the whole failed trajectory
- stricter false-positive budgets for stronger control

### Design lesson

Restart is not “send another message to the same context.” It is a state-selection problem.

Before Spotter implements it, the runtime needs:

- verified-state representation
- checkpoint lineage
- explicit retained artifacts
- external side-effect accounting

The target long-lived runtime makes those state boundaries more explicit, but does not solve them automatically.

---

## 5. Calibration Is Not Control — intervention advantage

**Calibration Is Not Control: Why LLM-Agent Oversight Needs Intervention**  
Chubin Zhang et al., 2026  
https://arxiv.org/abs/2606.21399

### Why it matters

Failure probability is not the same thing as control value. Two prefixes can look equally risky while only one benefits from intervention.

The relevant question is closer to:

> What is the expected outcome if we intervene now compared with letting this exact prefix continue?

### What Spotter borrows

- optimize for useful intervention, not maximum alarm rate
- condition the decision on the available action (`continue`, `verify`, `nudge`, `interrupt`, ...)
- use same-prefix branching to estimate intervention advantage

### Current Spotter status

The repository already has machinery for same-prefix counterfactual pairs, but the project still lacks a ground-truth task set and enough executed runs to make a causal claim.

This is one of the most important current evidence gaps.

---

## 6. interwhen — verifier-first runtime supervision

**interwhen: A Generalizable Framework for Verifiable Reasoning with Test-time Monitors**  
Vishak K. Bhat et al., 2026  
https://arxiv.org/abs/2602.11202  
Code: https://github.com/microsoft/interwhen

### Why it matters

interwhen separates verifiable properties from broad free-form reasoning and uses monitors only where they add information.

### What Spotter borrows

- turn deterministic properties into executable checks
- use external/verifiable evidence before semantic debate
- avoid forcing all reasoning into a rigid schema
- call the semantic reviewer only for what code cannot decide cheaply

### Architecture implication

This supports Spotter's split between:

```text
PreToolUse synchronous gate
  = narrow, deterministic, bounded

Async reviewer
  = semantic, selective, parallel to Main
```

---

## 7. Grounded Continuation — claim/evidence dependency tracking

**Grounded Continuation: A Linear-Time Runtime Verifier for LLM Conversations**  
Qisong He, Yi Dong, Xiaowei Huang, 2026  
https://arxiv.org/abs/2605.14175

### Why it matters

Grounded Continuation maintains explicit relationships between claims and evidence. When evidence is retracted, conclusions that depended on it can become stale.

### What Spotter borrows

- evidence-backed hypotheses instead of copying Main's conclusions as truth
- explicit stale-premise detection
- transitive invalidation

### Coding example

```text
E1: timeout appears under high concurrency
        ↓
H1: Redis pool exhaustion is the cause
        ↓
P1: modify Redis pool configuration

new evidence:
E2: stack trace points to upstream HTTP client

→ H1 becomes stale
→ P1 requires revalidation
```

### Current Spotter status

The prototype implements a typed audit ledger and stale propagation. Its practical reach is bounded by the observable event surface. Moving to richer App Server events is therefore not only an architecture cleanup; it is an experiment in whether the evidence graph becomes materially more useful.

---

## 8. AgentProcessBench — neutral exploration is not failure

**AgentProcessBench: Diagnosing Step-Level Process Quality in Tool-Using Agents**  
Shengda Fan et al., 2026  
https://arxiv.org/abs/2603.14465  
Code/data: https://github.com/RUCBM/AgentProcessBench

### Why it matters

Real tool-using trajectories contain actions that are not obviously useful but are also not errors. Current models can struggle to distinguish neutral exploration from genuine failure.

### What Spotter borrows

- uncertainty/neutrality must exist in the reviewer worldview
- repeated reads are not automatically a loop
- early intervention should be conservative around ambiguous exploration

### Design lesson

> “The agent is taking a while” is not itself a failure signal.

This is especially relevant for the planned cheap Signal Engine: signals must produce candidates, not verdicts.

---

## 9. HarnessFix — normalize traces and diagnose the harness too

**From Failed Trajectories to Reliable LLM Agents: Diagnosing and Repairing Harness Flaws**  
Mengzhuo Chen et al., 2026  
https://arxiv.org/abs/2606.06324

### Why it matters

HarnessFix compiles raw execution traces into a harness-aware intermediate representation and reasons about failures across both agent behavior and harness structure.

### What Spotter borrows

- normalize raw runtime streams into a backend-independent Trace IR
- keep provenance between normalized state and runtime events
- distinguish Main failure from Spotter/harness failure

### Architecture implication

The move from hooks to App Server should not leak Codex event shapes throughout Spotter core. The adapter should normalize events so the runtime, reviewer, metrics, and replay machinery remain transport-independent.

A repeated Spotter diagnosis may eventually imply that the right fix is a harness change rather than another intervention.

---

## 10. Refute-or-Promote — falsification and empirical gates

**Refute-or-Promote: An Adversarial Stage-Gated Multi-Agent Review Methodology for High-Precision LLM-Assisted Defect Discovery**  
Abhinav Agarwal, 2026  
https://arxiv.org/abs/2604.19049

### Why it matters

This work emphasizes killing candidate findings before promoting them and illustrates the danger of correlated model agreement without empirical evidence.

### What Spotter borrows

- try to falsify a consequential Main hypothesis rather than produce a competing solution
- treat cross-model diversity as a possible way to reduce correlated blind spots
- rank empirical verification above model consensus

### Design lesson

Ten models agreeing can still be weaker evidence than one decisive test.

---

# Synthesis

Spotter sits at the intersection of these ideas:

```text
Wink
  asynchronous coding-agent observer
          +
SWE-PRM
  trajectory failure taxonomy
          +
AgentForesight
  early prefix-only auditing
          +
FailFast-RestartSmart
  interrupt + clean restart
          +
Calibration Is Not Control
  intervention-value decision policy
          +
interwhen
  deterministic verifier-first design
          +
Grounded Continuation
  claim/evidence dependency state
          +
AgentProcessBench
  neutral exploration awareness
          +
HarnessFix
  normalized Trace IR / harness attribution
          +
Refute-or-Promote
  falsification + empirical gates
```

The target runtime architecture adds an engineering hypothesis underneath that synthesis:

```text
rich observation/control stream
          +
long-lived independent state
          +
selective async review
          +
bounded synchronous enforcement
```

should be a better substrate for studying runtime supervision than rebuilding state inside independent hook processes.

That engineering hypothesis must also be measured: richer infrastructure is not automatically better if its operational cost or fragility outweighs the supervision benefit.

---

# Research questions

## RQ1 — Intervention timing

How early should a coding-agent trajectory be reviewed?

Compare:

- periodic asynchronous review
- event-triggered review
- synchronous deterministic gate

Measure not only detection but wasted actions and wall-clock time before intervention.

## RQ2 — Intervention policy

When is `VERIFY` better than `NUDGE`? When does `INTERRUPT` help? When is `RESTART` better than continuing inside the same reasoning context?

The action should be chosen for expected intervention value, not only failure severity.

## RQ3 — Model diversity

Does a different reviewer model/family reduce correlated failures compared with same-model isolated-context review?

Compare:

- same model / isolated context
- different model / isolated context
- smaller specialized reviewer

Do not conflate “different family” with “stronger model”.

## RQ4 — Reviewer harm and sycophancy

How often does Spotter degrade a trajectory that would have succeeded on its own?

Related failure direction: how often does Main comply with a deliberately wrong but plausible Spotter nudge instead of refuting it with evidence?

Both should gate live injection policy.

## RQ5 — Deterministic vs semantic supervision

What fraction of useful supervision can be triggered or resolved by cheap executable checks before an LLM reviewer is needed?

This directly affects latency and cost.

## RQ6 — Observation surface

How much does the move from hook-collected events to App Server events change:

- visible tool outcomes
- failure observability ceiling
- goal/constraint availability
- timing precision
- trace fidelity

A better reviewer cannot compensate for missing evidence.

## RQ7 — Event-driven triggering

Can cheap candidate signals reduce reviewer calls without reducing useful detection?

Sweep:

- signal thresholds
- fallback cadence
- reviewer calls/session
- detection latency
- precision/miss rate
- token cost

## RQ8 — Runtime overhead

Does the standalone runtime reduce hot-path cost enough to justify its operational complexity?

Measure:

- hook p50/p95/p99 before/after
- App Server processing overhead
- idle/active daemon CPU and memory
- reconnect/recovery latency
- additional failure modes introduced by the runtime

## RQ9 — Replay/fork fidelity

How similar is a forked continuation to what the original prefix would have produced without intervention?

Before causal claims rely on forks, establish:

- identical-arm noise floor
- continuation similarity
- environmental failure rate

## RQ10 — User experience of supervision

Correct but annoying supervision can still be a product failure.

Measure or collect:

- dismiss/disagree signals
- blocks users override
- interventions perceived as useful vs disruptive
- whether the user can understand when Spotter is active, degraded, or disconnected

---

# Evaluation matrix

The eventual comparison should include at least:

```text
A. Vanilla coding agent

B. Periodic async reviewer
   fixed cadence, soft guidance

C. Event-driven Spotter
   cheap signals + semantic reviewer

D. Spotter + deterministic gate
   event-driven + PreToolUse enforcement

E. Full Spotter
   gate + steer + stronger interrupt/recovery primitives
```

The runtime substrate should be held constant where possible when comparing supervision policy, otherwise architecture changes become a confound.

## Primary metrics

### Outcome

- task resolution
- regression rate

### Efficiency

- main-agent tokens
- reviewer tokens
- tool-call count
- repeated-action count
- elapsed/tool time
- Spotter CPU/memory

### Observation quality

- event coverage
- observable outcome rate
- failure observability ceiling
- missing goal/constraint rate
- liveness / disconnect rate

### Intervention quality

- intervention precision
- sampled miss rate / recall estimate
- intervention advantage
- harm rate
- recovery rate
- ignored-intervention rate

### Timing

- wasted actions before intervention
- reviewer latency
- delivery latency
- stale intervention rate

### Recovery

- success after nudge
- success after interrupt
- success after restart
- useful work retained
- external state left unreverted

---

# Evidence gates for stronger behavior

The project should continue using an “earn the right to intervene” progression:

```text
observe
  ↓
measure detector/reviewer quality
  ↓
run counterfactuals
  ↓
soft intervention
  ↓
measure harm/recovery
  ↓
stronger intervention
```

A feature being implemented is not a reason to enable it by default.

Examples:

- deterministic gate: measure per-rule false positives and misses
- NUDGE/VERIFY: require reviewer quality + intervention-advantage evidence + wrong-nudge behavior
- INTERRUPT: require substantially stricter precision/harm budget
- RESTART: require recovery-state/side-effect correctness in addition to reviewer confidence

---

# Notes on research status

This is a fast-moving area. Several sources above are recent preprints rather than established production standards.

Spotter should treat them as:

- design evidence
- experimental hypotheses
- useful benchmark ideas

not as proof that every mechanism transfers to Codex, every repository, or every model generation.

The architecture should remain modular enough to remove mechanisms that do not survive measurement. A negative result—such as no measurable intervention advantage, excessive stale interventions, or negligible benefit from model diversity—is a useful project result, not something to hide behind additional complexity.
