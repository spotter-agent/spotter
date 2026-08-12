# Architecture

> **Status:** current prototype + target architecture.  
> The target standalone runtime is tracked by [#66](https://github.com/Bogyie/spotter/issues/66) and is not fully implemented yet.

## 1. Goal

Spotter should supervise coding-agent trajectories with the smallest practical impact on normal execution latency.

The design rule is:

> **Observe broadly. Check deterministic facts cheaply. Review semantic ambiguity asynchronously. Intervene only when intervention is worth its cost.**

The first target is Codex because it exposes both runtime observation/control through App Server and synchronous tool interception through hooks.

## 2. Current prototype vs target runtime

The repository currently proves several pieces of the supervision loop through hooks, journals, snapshots, replay, a shadow reviewer, labels, metrics, and counterfactual experiments.

Current prototype shape:

```text
Codex / Claude Code
       │
       │ lifecycle + tool hooks
       ▼
  spotter-hook process
       │
       ├─ journal
       ├─ deterministic gate
       ├─ snapshot
       └─ periodic shadow review trigger
```

This was intentionally simple, but it makes every hook invocation pay process/bootstrap and durable-state reconstruction costs.

The target architecture moves state and orchestration into a long-lived runtime:

```text
                     ┌─────────────┐
                     │  Codex TUI  │
                     └──────┬──────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ External App Server  │
                 │ observation/control  │
                 └──────┬────────▲──────┘
                        │        │
             event stream       │ turn/steer
                        │        │ turn/interrupt
                        │        │ thread injection
                        ▼        │
                 ┌──────────────────┐
                 │     spotterd     │
                 │                  │
                 │ SessionManager   │
                 │ Live State       │
                 │ Trace IR         │
                 │ Audit State      │
                 │ Signal Engine    │
                 │ Gate Engine      │
                 │ Reviewer         │
                 │ Intervention     │
                 │ Journal          │
                 └────────┬─────────┘
                          │
                 bounded deterministic
                      request/reply
                          │
                          ▼
                   PreToolUse Hook
                   if still required
```

Responsibility becomes:

```text
Codex App Server = primary observation + live control plane
spotterd         = long-lived supervision state and orchestration
PreToolUse Hook  = narrow synchronous enforcement plane
journal          = durable event log / recovery source
CLI              = user control plane
```

The architecture should remain adapter-oriented so another coding agent can expose different observation/control/enforcement surfaces without changing Spotter core policy.

## 3. Three runtime planes

Separating the planes prevents every agent event from being forced through the same mechanism.

### Observation plane

Answers: **What is happening?**

Target Codex source: App Server event stream.

Relevant information includes:

- thread and turn lifecycle
- user messages
- plan updates
- reasoning summaries exposed by the runtime/model
- command/tool start and completion
- stdout/stderr/exit status where available
- file changes and diffs
- MCP calls
- web search
- token usage

### Control plane

Answers: **How can Spotter influence an already-running trajectory?**

Target Codex primitives include:

- `turn/steer` for soft course correction
- `turn/interrupt` for strong intervention
- thread/context injection primitives where appropriate

Control is asynchronous relative to most observation. Main does not pause while an LLM reviewer thinks.

### Enforcement plane

Answers: **What must be decided atomically before execution?**

Target Codex primitive: `PreToolUse` while it remains the reliable synchronous veto boundary.

Only narrow deterministic policy belongs here.

## 4. Why App Server becomes primary for Codex

Hook collection proved useful, but it is a limited observation surface and a poor place for long-lived semantic supervision.

The target architecture prefers App Server because it can provide richer trajectory events and a live control channel in the same integration.

The project must first validate one critical lifecycle property: Spotter must attach to the **same App Server used by the user's Codex TUI**. A separate App Server process that hosts a different thread cannot steer the active user session.

A plain Codex TUI may choose an embedded App Server when no reusable external daemon is available. Therefore the architecture cannot rely on waking Spotter for the first time at `PreToolUse`; by then the App Server target may already be fixed.

The canonical App Server strategy is deliberately gated on an E2E PoC. See [Lifecycle](lifecycle.md) and #66.

## 5. `spotterd`

`spotterd` is the target owner of live supervision state.

Conceptual responsibilities:

```text
Session / Thread Manager
Trace normalization
Independent audit state
Cheap signal detection
Deterministic gate policy
Reviewer scheduling and budgets
Intervention freshness and delivery
Journal persistence
Recovery / reconciliation
```

The key state rule is:

```text
memory  = live state
journal = durable history + recovery
```

Journal replay is appropriate at daemon restart, thread resume, offline analysis, and experiment boundaries. It should not be the normal way to reconstruct state for every event.

## 6. Agent thread, turn, and attachment model

The word “session” is currently overloaded. The target runtime should distinguish:

```text
Agent Thread
  └─ Turn
       └─ Events

Runtime Attachment
  = one TUI/runtime connection period to the thread
```

A thread can outlive one TUI process:

```text
Thread A
├─ attachment #1
│  ├─ turn 1
│  └─ turn 2
└─ attachment #2 after resume
   ├─ turn 3
   └─ turn 4
```

Audit state generally belongs to the thread. Runtime health/latency may belong to an attachment. Intervention freshness belongs to a specific turn.

The durable/runtime model should preserve enough identity to correlate:

- agent thread id
- Spotter thread/session id if separate
- runtime attachment id
- turn id
- tool/item id
- repository/worktree

## 7. Event collector and Trace IR

Backend-specific runtime events should be normalized before they reach policy.

Conceptually:

```text
App Server event / Hook event / future adapter event
                   │
                   ▼
                Trace IR
                   │
       ┌───────────┴───────────┐
       ▼                       ▼
   live state               journal
       │
       ├─ signals
       ├─ reviewer
       └─ metrics
```

Example IR:

```text
STEP 42
thread: T7
turn: U12
kind: TOOL_PROPOSAL
tool: apply_patch
files: [src/redis.ts]
constraints: [C1, C2]
risk: medium
```

The IR should retain provenance. A normalized event must still be traceable to the underlying runtime event/tool id so live intervention and replay do not rely on journal adjacency.

## 8. Independent audit state

Spotter should not treat Main's own explanation as ground truth.

Minimum useful live state:

```text
Goal
Constraints
Hypotheses
Evidence
Open questions
Touched scope
Validation state
Recent failures
Intervention history
Active turn
Pending reviewer jobs
Reviewer budget
External side effects (later)
```

Claims should be linked to evidence. When supporting evidence is retracted or contradicted, dependent hypotheses and plans become stale and require revalidation.

The current prototype already implements a typed claim/evidence ledger and mechanical stale propagation for observable outcomes. In the target runtime this ledger should become live state instead of being rebuilt before every review.

## 9. Deterministic Gate Engine

Some supervision decisions should bypass the reviewer LLM entirely.

Examples:

- path allow/deny rules
- dependency-manifest changes
- destructive shell commands
- workspace escape
- explicit user execution restrictions
- eventually external-write classes with clear rules

Target synchronous path:

```text
PreToolUse
    │
    ▼
spotter-hook
    │ Unix socket / bounded IPC
    ▼
spotterd Gate Engine
    ├─ ALLOW
    └─ DENY
```

Do not perform these operations synchronously:

- semantic LLM review
- long journal replay
- whole-state reconstruction
- metrics aggregation
- broad repository analysis

The gate must have a measured latency budget and fail-open semantics when Spotter itself is unavailable, unless a future explicitly configured fail-closed mode is designed separately.

## 10. Fast Signal Engine

Cheap heuristics create **candidate failure hypotheses**, not semantic verdicts.

Candidate signals include:

- same tool repeatedly invoked with equivalent arguments
- failure streaks
- repeated reads with no expansion in the dependency frontier
- sudden growth in touched scope
- edits accumulating without validation
- stale hypothesis reuse
- unusual tool/token/time budgets
- attempted circumvention after a deterministic block

Output should look like:

```text
candidate: POSSIBLE_EXPLORATION_LOOP
support: repeated equivalent search 4x
```

not:

```text
verdict: EXPLORATION_LOOP
```

The reviewer remains responsible for ambiguous semantic judgment.

The signal layer is also how Spotter should move away from fixed periodic reviewer calls once the signal quality is measured.

## 11. Reviewer Scheduler

The Pair Reviewer is independently configured from Main where practical.

Its job is not to solve the task again. It tests candidate failure hypotheses and proposes the weakest useful intervention.

Useful reviewer input:

- user goal and explicit constraints
- relevant Trace IR window
- relevant independent audit state
- candidate failure hypothesis
- available verification actions
- prior interventions for the same issue
- which inputs are missing or truncated

Example output:

```json
{
  "decision": "VERIFY",
  "target": "hypothesis:H3",
  "reason": "The proposed change depends on an unverified timeout-source assumption.",
  "probe": "Inspect the timeout stack trace before editing Redis configuration.",
  "confidence": 0.91
}
```

### Reviewer job lifecycle

Reviewer work is asynchronous:

```text
QUEUED
  ↓
RUNNING
  ↓
DECIDED
  ├─ DELIVERED
  ├─ STALE
  ├─ CANCELLED
  └─ FAILED
```

Each job is anchored to the thread/turn that motivated it.

## 12. Intervention Controller

Spotter prefers the weakest sufficient action.

```text
CONTINUE    → no-op
VERIFY      → async steer / evidence request
NUDGE       → async steer
BLOCK       → synchronous deterministic deny
INTERRUPT   → turn interrupt
RESTART     → fresh continuation from verified state
```

### Freshness

A reviewer verdict can become obsolete while Main continues.

```text
signal at turn U12
      ↓
reviewer thinks
      ↓
verdict ready
      ↓
is U12 still the active target?
   ├─ yes → deliver
   └─ no  → stale policy
```

A stale verdict must not be blindly injected into a later turn.

Record:

- detection time/step
- reviewer start/end
- target turn
- attempted delivery
- delivered/stale/discarded outcome
- intervention latency

This turns responsiveness into a measurable quantity.

## 13. Pair-aware Main contract

Runtime supervision changes Main's environment. A small contract should clarify:

- the user remains the source of task intent
- Spotter messages are supervision, not new requirements
- `VERIFY` and `NUDGE` request reassessment, not blind compliance
- blocked actions should not be retried through superficial reformulation
- interruptions require replanning
- disagreement should be resolved with evidence where possible
- Main should not wait for Spotter during ordinary execution

The target delivery mechanism should not depend on keeping a hook process alive after it returns.

## 14. Action reversibility and side effects

A practical policy distinguishes actions by reversibility.

### Class A — observational / freely reversible

Examples: read, grep/search, ls, diff.

Default: asynchronous observation.

### Class B — locally reversible

Examples: patching files, local builds, local dependency operations.

Default: checkpoint where useful + asynchronous review; deterministic gates for explicit constraints.

### Class C — external or semantically irreversible

Examples: push, deploy, database writes, issue/PR creation, arbitrary MCP writes.

Target: stronger synchronous policy and side-effect recording.

Reasoning rollback does not undo external state. Before `RESTART` can claim meaningful recovery, Spotter needs an effect ledger that can say what remains changed outside the local worktree.

## 15. Snapshots, replay, and counterfactuals

The current prototype already provides Git-backed snapshots, detached-worktree restore, Codex continuation fork machinery, and a same-prefix experiment harness.

These remain important in the target architecture because they support two different goals:

1. recovery primitives
2. causal evaluation of intervention advantage

Resource lifecycle must be explicit:

```text
snapshot / fork worktree
CREATED → IN_USE → RETAINED or CLEANED
```

Never manipulate user worktrees or refs outside Spotter-owned namespaces. Cleanup should be Git-aware.

## 16. Durability and recovery

### Current journal contract

The prototype step journal is append-only JSONL and includes cross-process locking, fsync, torn-tail recovery, and strict reads for destructive operations.

That durability work remains useful, but the target runtime should reduce the number of independent writers because `spotterd` becomes the primary live writer.

### Target recovery path

On daemon restart:

```text
spotterd starts
    ↓
reconnect App Server
    ↓
list/reconcile active threads
    ↓
hydrate durable Spotter state as needed
    ↓
resume live supervision
```

Recovery is a boundary condition, not the normal event-processing loop.

### Degraded state

Observation/control health must be visible independently from daemon health:

```text
Spotter daemon:       running
Codex App Server:     unavailable
Observation:          unavailable
Live intervention:    unavailable
PreToolUse gate:      available
```

Silence cannot mean both “nothing is wrong” and “Spotter is disconnected”.

## 17. Current prototype implementation notes

The architectural migration should preserve the useful guarantees already built.

### Journal durability

- cross-process append serialization
- monotonic step/proposal allocation
- flush/fsync before releasing the write lock
- torn-tail recovery for normal readers
- strict reads for destructive cleanup decisions

### Snapshot safety

- snapshots use Git objects/refs without touching user HEAD/index
- restore happens in detached worktrees
- refs are pinned under Spotter-owned namespaces
- unchanged states can be deduplicated
- prune is conservative and dry-run by default

### Audit ledger

- Main reasoning summaries are not accepted as evidence
- only observable outcomes can become evidence
- contradicting outcomes retract previous evidence
- stale propagation is transitive

The hook-collected prototype measured observable outcomes on only 33 of 340 real tool results (10%). This figure is specific to the current observation surface. The App Server migration must re-measure it instead of treating 10% as a permanent architectural limit.

### Gate ambiguity

Where deterministic parsing cannot support a safe judgment, the current gate fails open and records the blind spot separately from false-positive metrics.

That distinction should survive the migration.

## 18. Integration lifecycle and service ownership

Runtime architecture does not end at the event loop. Spotter must also own setup, service lifecycle, upgrades, teardown, and repair coherently.

Target user flow:

```bash
brew install spotter
spotter setup codex
spotter doctor

# normal use
codex
```

The user should not need to manually start `spotterd` or an App Server before each Codex launch.

Because an external App Server may need to exist before Codex chooses its target, a fully lazy “start on first tool hook” daemon can conflict with the full target experience. The App Server lifecycle PoC decides whether managed mode needs a login-scoped service.

Detailed lifecycle and ownership rules live in [lifecycle.md](lifecycle.md).

## 19. Adapter boundary

Codex is the first target, not the permanent core API.

An agent adapter should expose normalized capabilities such as:

```text
observe thread/turn events
observe tool/result/diff
synchronous pre-action veto (optional)
soft steer active turn (optional)
interrupt active turn (optional)
thread resume/fork primitives (optional)
```

Spotter core should degrade explicitly when a capability is missing rather than assuming every agent has Codex's exact surfaces.

## 20. First migration boundary

The architecture migration should intentionally avoid turning into a rewrite of everything at once.

First prove:

1. a stable external App Server path can host the same Codex TUI thread that Spotter observes
2. Spotter can receive the event stream as a second client
3. Spotter can track the active turn
4. `turn/steer` reaches the real user session
5. the `PreToolUse` gate can remain a small bounded synchronous path

Only then move the existing reviewer/state/metrics machinery into the daemon.

The MVP hypothesis remains:

> **Can an independent runtime spotter detect a useful subset of trajectory failures early enough that intervention improves task outcomes without becoming an expensive source of false positives or latency?**

Architecture makes that experiment possible; it does not answer it by itself.
