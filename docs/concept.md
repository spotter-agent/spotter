# Concept

> **Purpose:** define the problem Spotter is trying to solve, the intervention model, and the principles that should remain true even if the implementation changes.

---

## 30-second summary

Spotter is a **runtime supervision system for coding agents**.

The main agent still owns the task. Spotter observes the execution path, keeps an independent view of claims/evidence/progress, and intervenes only when intervention is more useful than letting Main continue.

```text
User goal
   ↓
Main Agent ───────────────────────────► keeps working
   │
   │ observable trajectory
   ▼
Spotter
   ├─ maintain independent state
   ├─ detect possible trajectory failure
   ├─ verify ambiguous cases
   └─ choose the weakest useful intervention
```

Spotter is not trying to make the agent ask for approval before every action. Its core question is:

> **How do we keep a recoverable mistake from becoming an expensive trajectory?**

The target intervention ladder is:

```text
CONTINUE → VERIFY → NUDGE → BLOCK → INTERRUPT → RESTART
```

The target product boundary is also explicit: Spotter should be an **independent local runtime**, not a Codex plugin that happens to contain supervision logic. Codex is the first adapter.

---

## Quick reference

| Concept | Meaning |
| --- | --- |
| Main | the coding agent that performs the user's task |
| Spotter | independent runtime supervisor |
| Trajectory | the sequence of observations, hypotheses, actions, tool calls, edits, results, and revisions |
| Candidate signal | cheap evidence that something may be going wrong; not yet a semantic verdict |
| Reviewer | an independent model that evaluates ambiguous candidates |
| Deterministic gate | executable policy that can allow/deny a pending action without an LLM |
| Intervention | `VERIFY`, `NUDGE`, `BLOCK`, `INTERRUPT`, or `RESTART` |
| Audit state | Spotter's independent model of goal, constraints, hypotheses, evidence, progress, and failures |
| Intervention advantage | whether intervening from a prefix improves the outcome compared with continuing from the same prefix |

---

# 1. The problem: trajectory failures

Coding agents increasingly operate over long sequences:

```text
understand
  ↓
inspect
  ↓
hypothesize
  ↓
edit
  ↓
run
  ↓
observe
  ↓
revise
  ↓
validate
```

Many important failures are not a single wrong output. They are **trajectory failures**: one weak premise creates a path that keeps generating further work.

Example:

```text
E1: timeout happens under load
        │
        ▼
H1: Redis pool exhaustion is the cause
        │   (never verified)
        ▼
P1: change Redis pool configuration
        │
        ▼
new failures appear
        │
        ▼
agent compensates with more edits
        │
        ▼
scope expands and the original mistake becomes expensive
```

A post-hoc reviewer can still catch the bad diff, but by then the agent may already have spent most of the tokens, tool calls, elapsed time, and repository churn.

Spotter tries to act closer to the first meaningful deviation:

```text
H1 appears
  ↓
Spotter notices consequential assumption has weak evidence
  ↓
VERIFY: inspect stack trace / focused probe
  ↓
H1 confirmed → continue
or
H1 refuted → avoid the wrong branch
```

The optimization target is therefore not “detect every imperfect action.” It is **reduce wasted progress while preserving Main's autonomy**.

---

# 2. What Spotter observes

Typical failure classes include:

### Goal / scope failures

- drifting from the user's request;
- forgetting a constraint in a long run;
- expanding a local change into an unnecessary refactor;
- dependency or file-scope creep.

### Epistemic failures

- treating an unverified hypothesis as fact;
- continuing from evidence that was later contradicted;
- mistaking correlation for cause;
- over-trusting Main's own summary of what happened.

### Search / execution failures

- repeated low-information exploration;
- retrying equivalent failing commands;
- staying inside one hypothesis despite contradictory signals;
- continuing after a tool strategy repeatedly fails.

### Validation failures

- meaningful edits without targeted validation;
- tests run at the wrong layer;
- passing a partial check while the affected behavior remains untested;
- accumulating a large change before any feedback loop.

Not every instance is an error. Exploration can be neutral and repetition can be justified. Cheap signals therefore create **candidates**, while semantic judgment remains a separate step.

---

# 3. Core loop

The intended supervision loop is:

```text
1. OBSERVE
   collect runtime events and relevant repository/tool outcomes

2. UPDATE STATE
   maintain goal, constraints, hypotheses, evidence, touched scope,
   validation state, recent failures, and intervention history

3. DETECT CANDIDATE
   use cheap signals or deterministic verifiers

4. REVIEW WHEN NEEDED
   ask an independent reviewer only for ambiguous semantic judgment

5. CHOOSE ACTION
   prefer the weakest intervention likely to help

6. DELIVER / ENFORCE
   async steer for VERIFY/NUDGE; sync deny for deterministic BLOCK

7. MEASURE
   record timing, cost, precision, harm, recovery, and outcome
```

Spotter is successful only if this loop improves outcomes **without becoming a larger source of cost, latency, or noise than the failures it prevents**.

---

# 4. Core principles

## 4.1 Always observe, rarely interrupt

Silence is a valid and desirable Spotter outcome.

An observer that comments on every imperfect decision creates:

- latency;
- token cost;
- anchoring;
- unnecessary plan churn;
- a second source of mistakes.

Spotter should intervene only when expected intervention value exceeds the expected value of letting Main continue.

## 4.2 Review the path, not only the artifact

Static review asks “is this code correct?”

Spotter also asks:

- Why is this file being edited?
- Which hypothesis motivates this change?
- What observable evidence supports that hypothesis?
- Did later evidence invalidate it?
- Is the search frontier expanding or repeating?
- Has the agent changed meaningful behavior without validation?
- Is the current action still connected to the user's goal?

## 4.3 Prefer falsification over model debate

When Main and Spotter disagree, the preferred next step is a **cheap discriminating probe**, not a long conversation between models.

Examples:

```text
focused test
compiler/type checker
stack trace inspection
call-site search
repository state inspection
log query
small reproduction
```

The reviewer should not become a second coding agent racing Main to produce an alternate solution.

## 4.4 Deterministic facts deserve deterministic checks

Examples:

```text
"Do not add dependencies"
  → inspect manifest modification

"Only modify src/auth/**"
  → inspect proposed/changed paths

"Never run destructive git commands"
  → inspect pending command
```

These do not need an LLM.

Semantic review is reserved for claims such as:

```text
"This approach is drifting from the requested scope."
"The next edit depends on a weak assumption."
"The agent is repeating exploration without gaining information."
```

## 4.5 Semantic supervision should be asynchronous

Main should not wait for a reviewer before each ordinary action.

```text
Main continues ─────────────────────────────►

candidate
   ↓
reviewer thinks in parallel
   ↓
verdict arrives
   ↓
is target turn still relevant?
   ├─ yes → deliver
   └─ no  → stale policy
```

Only narrow deterministic enforcement belongs in a synchronous pre-action path.

## 4.6 Main and Spotter should fail differently

Spotter should preserve judgment independence:

- separate reviewer context;
- independently configured model where practical;
- Main's explanation is not automatically evidence;
- uncertainty and missing evidence remain explicit.

Different model families may reduce correlated errors, but that is an empirical question. Independent state/context is useful even when only the same model family is available.

## 4.7 Live state belongs in the runtime, not the log

Target ownership:

```text
memory  = live supervision state
journal = durable event history / recovery source
snapshot = repository state checkpoint
```

Journal replay belongs at resume/recovery/offline analysis boundaries, not on every ordinary event.

## 4.8 Integrations are adapters

Spotter should not be defined as “a Codex plugin.”

A coding-agent adapter may expose different capabilities:

```text
observe events
soft steer active turn
interrupt active turn
atomic pre-action veto
resume/fork
```

Spotter core should reason over normalized capabilities and Trace IR rather than Codex-specific event formats.

---

# 5. Intervention semantics

## `CONTINUE`

No useful intervention is justified.

Possible reasons:

- trajectory looks healthy;
- signal was a false alarm;
- uncertainty exists but the next action is cheap/reversible;
- intervening would create more disruption than value.

Expected behavior: **silent no-op**.

## `VERIFY`

A consequential decision depends on weak evidence.

Good VERIFY:

```text
Before changing the Redis pool size, inspect the timeout stack trace;
the current evidence does not yet identify Redis as the source.
```

Bad VERIFY:

```text
Think more carefully about the problem.
```

VERIFY should request a small, discriminating piece of evidence.

## `NUDGE`

The path shows early drift, omission, or inefficient commitment.

Good NUDGE:

```text
The last three edits expand beyond the requested auth timeout fix.
Re-check the user scope before continuing the refactor.
```

A NUDGE is not a replacement implementation plan.

## `BLOCK`

A pending action violates a deterministic constraint.

Examples:

```text
git reset --hard
forbidden path mutation
unapproved dependency manifest change
workspace escape
```

BLOCK should remain near-deterministic. Semantic disagreement must not silently become a deny.

## `INTERRUPT`

The current turn is likely to compound a bad trajectory faster than a soft correction can help.

INTERRUPT has a much higher false-positive cost than NUDGE, so it requires a stronger evidence/precision gate.

## `RESTART`

The reasoning context itself is no longer trustworthy.

A fresh continuation should receive deliberately selected state:

```text
user goal
explicit constraints
verified evidence
current repository state
intentionally retained artifacts
```

It should not automatically inherit the entire failed reasoning history.

RESTART must also account for external side effects. Restarting reasoning does not undo a push, deploy, database write, or external API mutation.

---

# 6. Pair-aware Main contract

Runtime supervision changes Main's environment. Main needs a small contract:

- the user remains the source of task intent;
- Spotter feedback is supervision, not a new requirement;
- `VERIFY` / `NUDGE` request reassessment, not blind compliance;
- disagreement should be resolved with evidence when possible;
- blocked actions should not be retried through superficial reformulation;
- an interrupt requires replanning;
- Main should not wait for Spotter during ordinary execution.

A useful rule is:

> **Pair feedback is a request to re-evaluate the path, not authority to redefine the goal.**

---

# 7. Independent working state

A useful first state model does not require a graph database.

```text
ThreadState
  Goal
  Constraints
  Active turn
  Hypotheses
  Evidence for/against
  Open questions
  Touched scope
  Validation state
  Recent actions
  Recent failures
  Intervention history
  Pending reviewer jobs
  Reviewer budgets
```

The key relationship is claim → evidence dependency.

Example:

```text
E1: failures correlate with high concurrency
        ↓ supports
H1: Redis pool exhaustion causes timeout
        ↓ motivates
P1: change Redis pool settings

E2 arrives: stack trace points to upstream HTTP client
        ↓
H1 becomes stale
        ↓
P1 must be revalidated
```

Main's own prose summary is not enough to turn H1 into verified evidence.

---

# 8. Observation, control, and enforcement

These are different product surfaces.

```text
Observation
  What is happening?

Control
  How can Spotter influence an active trajectory?

Enforcement
  What must be decided atomically before execution?
```

Target Codex mapping:

```text
Codex App Server
  → primary observation + live control

PreToolUse Hook
  → narrow deterministic enforcement

spotterd
  → state, signals, reviewer, policy, budgets, recovery
```

This mapping depends on the App Server lifecycle/attach PoC in #66.

---

# 9. Product lifecycle is part of the concept

A runtime supervisor is not complete if the user must manually orchestrate several background processes every time they use Codex.

Target experience:

```bash
brew install spotter
spotter setup codex
spotter doctor

# after setup
codex
```

The design must also define:

- login/startup behavior;
- multiple simultaneous coding sessions;
- thread resume;
- daemon/App Server crash recovery;
- Spotter upgrade;
- Codex upgrade;
- config/schema migration;
- teardown;
- uninstall without teardown;
- data purge;
- reinstall;
- legacy plugin migration.

See [Lifecycle](lifecycle.md) for the operational contract.

---

# 10. What Spotter is not

Spotter is not:

- a second coding agent racing Main to solve the same task;
- a static code-review bot;
- a security-only guardrail;
- a mandatory approval gate before every tool call;
- a multi-agent debate framework;
- a mechanism for maximizing the amount of reasoning;
- a Codex-only plugin as a permanent product boundary;
- a claim that every detectable mistake should be interrupted.

Its purpose is narrower: **reduce wasted progress while preserving Main's autonomy**.

---

# 11. What would count as success?

Implementation success and research success are different.

### Implementation success

Spotter can:

1. observe the real active coding-agent trajectory;
2. maintain independent live state;
3. detect candidates cheaply;
4. run semantic review asynchronously;
5. deliver a decision to the correct active turn;
6. deterministically block narrow policy violations;
7. survive restart/resume/upgrades without losing ownership clarity.

### Research success

Compared with a matched baseline, Spotter demonstrates:

- positive intervention advantage;
- acceptable false-positive and miss rates;
- reduced wasted actions/tokens/time;
- low intervention harm;
- acceptable runtime and operational overhead.

A plausible reviewer verdict is not enough. A polished daemon is not enough. The system must prove that intervention is worth its cost.

---

# 12. Trajectory Engineering

Spotter is an experiment in a broader layer: **Trajectory Engineering**.

- Prompt engineering shapes the instruction.
- Context engineering shapes what the model can see.
- Harness engineering shapes the environment, tools, and constraints.
- **Trajectory engineering shapes what happens while the agent is already executing.**

Its concerns include:

```text
observation
state tracking
verification
intervention timing
steering
blocking
interruption
restart boundaries
recovery
causal evaluation of interventions
```

The working hypothesis is:

> **A better agent system is not only one that makes fewer mistakes. It is also one that detects mistakes early enough that they remain cheap to correct.**

That hypothesis is not yet proven for Spotter. Null and negative results are first-class outcomes.

For current implementation state, see [Status](status.md). For runtime mechanics, see [Architecture](architecture.md). For implementation order, see [Roadmap](roadmap.md).
