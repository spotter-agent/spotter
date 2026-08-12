# Concept

## What is Spotter?

Spotter is a **runtime supervision system for coding agents**.

A primary coding agent remains responsible for doing the work. Spotter observes the execution trajectory, keeps an independent view of what is known and uncertain, and intervenes only when intervention is likely to improve the outcome.

The metaphor is intentional: a good spotter does not perform the lift. They watch closely, avoid unnecessary interference, and step in before a recoverable mistake becomes a failure.

> **The user defines the goal. Spotter reviews the path.**

The project started as a hook/plugin-centered prototype. The target product boundary is now broader: Spotter should be an **independent local runtime** that integrates with coding agents such as Codex through their observation, control, and enforcement surfaces. The agent integration is an adapter; it is not the identity of Spotter itself.

## The problem

Coding agents increasingly operate over long trajectories:

```text
understand → inspect → hypothesize → edit → run → observe → revise → validate
```

Many failures are not single incorrect outputs. They are **trajectory failures**.

A weak assumption can become a plan. The plan can trigger unnecessary edits. Those edits can create secondary failures. The agent then spends more time compensating for consequences of the original mistake. A final-diff reviewer may detect the problem, but only after most of the cost has already been incurred.

Common examples include:

- drifting away from the requested scope
- treating an unverified hypothesis as fact
- continuing to use a premise after new evidence invalidates it
- repetitive exploration that produces little new information
- repeatedly retrying a failing tool strategy
- expanding a local fix into an unnecessary refactor
- forgetting a user constraint midway through a long run
- editing before gathering enough evidence
- making changes without validating the behavior they affect

Spotter aims to catch these failures **while the trajectory is still recoverable**.

## What changes with runtime supervision?

Traditional review mainly examines an artifact after execution:

```text
request → implementation → diff → review
```

Runtime supervision adds another loop:

```text
request
   ↓
Main Agent ───────────────► execution continues
   │
   │ observable trajectory
   ▼
Spotter
   │
   ├─ maintain independent state
   ├─ detect possible failure
   ├─ verify ambiguous cases
   └─ intervene only when useful
```

The key distinction is timing. Spotter tries to reduce the amount of **wasted progress after a meaningful deviation**, not merely increase the number of errors noticed eventually.

## Core principles

### 1. Always observe, rarely interrupt

Silence is a valid and desirable Spotter action.

An observer that comments on every imperfect decision becomes another source of noise, latency, and anchoring. Spotter should intervene only when the expected value of intervention exceeds the expected value of letting Main continue.

### 2. Review the process, not only the artifact

Static review asks whether the code is correct.

Spotter also asks:

- Why is this file being edited?
- What evidence supports the current hypothesis?
- Is this action still connected to the user's goal?
- Did the agent already invalidate the premise behind this plan?
- Is exploration producing new information?
- Has the agent accumulated changes without validating them?

### 3. Prefer falsification over opinion

When Main and Spotter disagree, the preferred resolution is not a longer conversation between models.

Spotter should look for the cheapest discriminating evidence:

- run a focused test
- inspect the stack trace
- search the call sites
- check the compiler or type system
- inspect repository state
- query logs

The reviewer is not a competing implementation agent. It is an independent falsifier that tries to expose fragile assumptions and move the trajectory toward evidence.

### 4. Deterministic facts deserve deterministic checks

Known constraints do not need an LLM judge.

Examples:

- “Do not add dependencies” → inspect manifest changes
- “Only modify this directory” → inspect affected paths
- forbidden commands → judge the pending tool call
- explicit execution boundaries → deterministic policy

LLM review should be reserved for semantic ambiguity: scope drift, unsupported reasoning, missed requirements, stale assumptions, premature abstraction, and similar judgments.

This leads to two different control paths:

```text
semantic supervision        deterministic enforcement
asynchronous                synchronous when required
VERIFY / NUDGE              ALLOW / BLOCK
```

### 5. Main and Spotter should fail differently

Spotter should be independently configurable and, where practical, use a different reviewer model or model family from Main.

Its state should also preserve judgment independence. Main's conclusions must not automatically become Spotter's facts. A claim can be recorded as a hypothesis with supporting and contradicting evidence rather than copied into shared state as truth.

Model diversity is still an empirical question. Independent context is valuable even when only the same model family is available.

### 6. Intervention should be incremental

Spotter uses an escalation ladder:

```text
CONTINUE
   ↓
VERIFY
   ↓
NUDGE
   ↓
BLOCK
   ↓
INTERRUPT
   ↓
RESTART
```

The weakest sufficient intervention is preferred.

### 7. Supervision must not become the bottleneck

The main agent should not wait for a semantic reviewer before every action.

The target architecture therefore keeps semantic supervision asynchronous: the agent continues, Spotter thinks in parallel, and a decision is delivered to the active turn only if it is still relevant.

Only narrow deterministic enforcement belongs on a synchronous pre-action path.

### 8. Live state belongs in the runtime, not in the log

The prototype reconstructs substantial state from hook-written journals. That is acceptable for experimentation but not the desired steady-state architecture.

Target responsibility:

```text
memory  = live supervision state
journal = durable history / recovery source
```

Journal replay should happen at recovery, resume, and analysis boundaries—not on every ordinary event.

### 9. Agent integrations are adapters

Spotter should not be defined by Codex plugin packaging.

For Codex, the target is to use App Server as the primary observation/control plane and keep hooks only where they provide a unique enforcement primitive such as atomic pre-tool blocking.

A future agent integration may expose different primitives. Spotter core should depend on normalized capabilities rather than a specific hook or transcript format.

## Intervention semantics

### CONTINUE

No meaningful issue is detected, or an issue exists but intervention is unlikely to help. Spotter remains silent.

### VERIFY

A consequential decision depends on a weakly supported assumption. Spotter asks Main to gather a small amount of discriminating evidence before compounding the assumption.

### NUDGE

The trajectory shows early drift, inefficiency, or omission. Spotter injects a concise course correction without taking over the task.

### BLOCK

A pending action clearly violates a known deterministic constraint or configured execution boundary. The action is stopped before execution.

`BLOCK` should remain near-deterministic. Semantic reviewer disagreement should not casually become a synchronous denial.

### INTERRUPT

The active turn has entered a trajectory where continued execution is likely to compound waste or damage. Spotter stops the turn and requires reassessment instead of allowing the same plan to continue indefinitely.

### RESTART

The current reasoning context itself is considered contaminated by stale assumptions or cascading failures. A fresh continuation begins from the user's goal, verified evidence, current repository state, and deliberately retained artifacts.

This should be rare and should not pretend that external side effects have been rolled back when they have not.

## Pair-aware Main

Runtime supervision changes the environment in which the primary agent operates. Main therefore benefits from a small execution contract.

It should understand that:

- Spotter signals are runtime supervision, not new user requirements.
- Spotter feedback should not be accepted blindly.
- disagreement should preferably be resolved with external evidence.
- a blocked action should not be retried through superficial reformulation.
- an interrupt requires reassessment before continuing.
- Main should not wait for Spotter approval during normal execution.

A useful rule is:

> **Pair feedback is a request to re-evaluate the path, not authority to redefine the goal.**

The exact delivery mechanism is runtime-specific. In the target Codex architecture, soft feedback should be delivered through the live App Server control path rather than by keeping a hook process alive.

## Independent working state

Spotter needs more than a raw transcript, but the first useful implementation does not need a large graph database.

A compact state is enough:

```text
Goal
Constraints
Current hypotheses
Evidence for / against
Open questions
Current action
Files touched
Validation state
Recent failures
Intervention history
Active thread / turn
Reviewer jobs / budgets
```

The important relationship is between **claims and evidence**. If evidence is invalidated, downstream hypotheses and plans that depended on it should become stale and require revalidation.

## Observation, control, and enforcement are different planes

A useful conceptual split is:

```text
Observation plane
  → what happened / is happening?

Control plane
  → how can Spotter steer or stop the active trajectory?

Enforcement plane
  → what must be decided atomically before an action executes?
```

For the current Codex target, the intended mapping is:

```text
App Server      → primary observation + live control
PreToolUse Hook → narrow synchronous enforcement
spotterd        → state, detection, review, policy, orchestration
```

This mapping is a target architecture and depends on the App Server lifecycle/attach PoC tracked by [#66](https://github.com/Bogyie/spotter/issues/66).

## Product lifecycle is part of the design

A supervision runtime that works only after manually starting several background processes is not a complete product.

The target UX is:

```bash
brew install spotter
spotter setup codex
spotter doctor

# thereafter
codex
```

Spotter must also define behavior for:

- login/startup
- multiple simultaneous agent sessions
- thread resume
- daemon or App Server crash
- Spotter upgrade
- Codex upgrade
- schema migration
- integration teardown
- uninstall without teardown
- data purge
- reinstall

See [Lifecycle](lifecycle.md) for the full operational model.

## What Spotter is not

Spotter is not:

- a second coding agent racing Main to solve the same task
- a static code-review bot
- a security-only guardrail
- a mandatory approval gate for every tool call
- a multi-agent debate system
- a mechanism for maximizing the amount of reasoning
- a Codex-only plugin as a product boundary

Its purpose is to reduce **wasted progress** while preserving Main's autonomy.

## Trajectory Engineering

Spotter is an experiment in a broader layer we call **Trajectory Engineering**.

- Prompt engineering shapes the instruction.
- Context engineering shapes what the model can see.
- Harness engineering shapes the execution environment, tools, and orchestration.
- **Trajectory engineering shapes what happens while the agent is already executing.**

Its concerns include observation, state tracking, verification, intervention timing, steering, interruption, rollback boundaries, restart, and recovery.

The working hypothesis behind Spotter is:

> Better agents are not only agents that reason better. They are agents whose mistakes are detected early enough that they remain cheap to correct.

That hypothesis is not yet proven for Spotter. The project should keep mechanisms behind measurement gates and treat negative or null intervention results as first-class outcomes.
