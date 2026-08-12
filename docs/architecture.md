# Architecture

## Goal

Spotter should supervise a coding-agent trajectory with the smallest practical impact on normal execution latency.

The main design rule is:

> **Cheap checks first. Semantic review only when needed. Strong intervention only when justified.**

The initial target is Codex because its current extension/runtime surface exposes useful interception and control points.

## High-level architecture

```text
                         User Goal
                            │
                            ▼
                    ┌──────────────┐
                    │  Main Agent  │
                    │    Codex     │
                    └──────┬───────┘
                           │
              events / tools / diffs / results
                           │
                           ▼
                    ┌──────────────┐
                    │   Trace IR   │
                    └──────┬───────┘
                           │
               ┌───────────┴───────────┐
               ▼                       ▼
       Deterministic checks        Fast signals
               │                       │
               └───────────┬───────────┘
                           │ candidate issue
                           ▼
                    ┌──────────────┐
                    │ Pair Reviewer│
                    │ separate LLM │
                    └──────┬───────┘
                           │
                           ▼
                 Intervention Controller
                           │
       ┌──────────┬────────┼─────────┬─────────┐
       ▼          ▼        ▼         ▼         ▼
   CONTINUE    VERIFY    NUDGE      BLOCK   INTERRUPT
                                               │
                                            RESTART
```

## Components

### 1. Main Harness

The main Codex session performs the task normally.

Spotter should inject only a small pair-runtime contract at session start so Main understands supervision signals without waiting for approval on every action.

The contract should establish:

- the user remains the source of task intent
- Spotter messages are runtime supervision signals
- `VERIFY` and `NUDGE` require reassessment, not blind compliance
- blocked actions must not be trivially retried
- interruptions require replanning
- disagreements should be resolved with evidence where possible

### 2. Event Collector

Spotter subscribes to the observable execution surface and normalizes relevant events.

Potential Codex sources include:

- session / turn lifecycle
- plan and agent-message events
- reasoning-summary events when exposed by the runtime/model
- tool proposals and results
- command output
- patches and diffs
- test results
- interruption and completion states

Spotter should not depend on private chain-of-thought access. The design must remain useful with observable actions, summaries, repository state, and tool results alone.

### 3. Trace IR

Raw runtime events are noisy and backend-specific. Spotter should compile them into a compact intermediate representation.

Example:

```text
STEP 42
kind: TOOL_PROPOSAL
tool: apply_patch
intent: fix authentication timeout
files: [src/redis.ts]
depends_on: H3
constraints: [C1, C2]
risk: medium

H3
claim: Redis pool exhaustion is the timeout source
support:
  - high concurrency correlates with timeout
  - pool max is 10
missing:
  - stack trace has not confirmed Redis as source
status: UNVERIFIED
```

The IR allows rules, replay, benchmark generation, and reviewer-model changes without coupling them to a particular raw Codex event format.

### 4. Audit State

Spotter keeps an independent state rather than treating Main's summaries as ground truth.

Minimum useful state:

```text
Goal
Constraints
Hypotheses
Evidence
Open questions
Touched scope
Validation state
Recent tool failures
Intervention history
External side effects
```

Claims should be connected to evidence. When supporting evidence is retracted or contradicted, dependent hypotheses and actions become stale and require revalidation.

### 5. Deterministic Verifiers

Some supervision decisions should bypass the reviewer LLM entirely.

Examples:

- path allow/deny rules
- dependency changes
- destructive shell commands
- external-write operations
- test-result requirements
- file-count or diff-size thresholds
- explicitly forbidden tools

These checks are especially suitable for synchronous pre-action gating.

### 6. Fast Signals

Cheap heuristics generate **candidate failure hypotheses**, not final judgments.

Examples:

- same tool repeatedly invoked with equivalent arguments
- repeated failure exit codes
- repeated reads with no expansion in the dependency frontier
- sudden growth in touched files
- edits accumulating without validation
- a previously invalidated hypothesis still appearing in proposed actions
- budget anomalies in tool calls, tokens, or elapsed steps

A signal should say:

```text
candidate: POSSIBLE_EXPLORATION_LOOP
```

not:

```text
verdict: EXPLORATION_LOOP
```

The semantic reviewer decides ambiguous cases.

### 7. Pair Reviewer

The Pair Reviewer is an independently configured model.

Its job is not to solve the task again. Its job is to test failure hypotheses and propose the smallest useful intervention.

A useful reviewer input contains:

- user goal and explicit constraints
- relevant Trace IR window
- relevant audit-state subgraph
- candidate failure hypothesis
- available verification actions
- prior interventions around the same issue

A useful structured output:

```json
{
  "decision": "VERIFY",
  "target": "hypothesis:H3",
  "reason": "The pending edit depends on an unverified timeout-source assumption.",
  "probe": "Inspect the timeout stack trace before editing Redis configuration.",
  "confidence": 0.91
}
```

### 8. Intervention Controller

The controller translates review decisions into runtime actions.

#### CONTINUE

No action.

#### VERIFY

Inject a concise evidence request into Main's active work.

#### NUDGE

Inject a concise course correction.

#### BLOCK

Reject a pending action synchronously.

#### INTERRUPT

Stop the current turn and request replanning from the last sound state.

#### RESTART

Launch a fresh rollout with only the user goal, verified state, current repository state, and deliberately retained changes.

## Why two paths: asynchronous observer + synchronous gate

Using a reviewer model synchronously before every action would create unacceptable latency.

Spotter therefore separates supervision into two paths.

```text
                    Main
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
 Async trajectory review     Pre-action gate
 semantic / broad             narrow / fast
        │                         │
 VERIFY / NUDGE            ALLOW / BLOCK
```

Most reading, searching, and reversible exploration can proceed while Spotter observes asynchronously.

Actions with meaningful external or hard-to-reverse effects deserve stronger synchronous scrutiny.

## Action reversibility classes

A practical policy can classify actions by reversibility.

### Class A — freely reversible / observational

Examples:

- read
- grep / search
- ls
- git diff

Default: asynchronous observation.

### Class B — locally reversible

Examples:

- apply patch
- modify files
- local dependency installation

Default: checkpoint + asynchronous review; synchronous checks for known constraints.

### Class C — external or semantically irreversible

Examples:

- push
- deploy
- create external resources
- database writes
- issue / PR creation
- arbitrary MCP writes

Default: synchronous gate plus side-effect recording.

## Side-effect ledger

Reasoning rollback does not undo external state.

Spotter should record external effects before supporting restart/rollback semantics:

```text
EFFECT 18
kind: github.create_issue
resource: repo/foo
result: issue #392
reversible: false

EFFECT 19
kind: filesystem.patch
checkpoint: C14
reversible: true
```

The initial MVP can simply classify and record effects. Full compensating rollback can come later.

## Durability, retention, and ambiguity policy

Supervision data is only useful if it survives crashes, stays bounded on disk,
and is honest about what it could not judge. The three contracts below are what
the implementation actually guarantees today.

### Journal durability

The step journal is append-only JSONL, one file per session, written by however
many hook processes the runtime spawns.

- **Serialization.** Every append takes an exclusive `flock` on a sidecar lock
  file. Step numbers and proposal numbers are allocated inside that lock, so
  concurrent hook processes cannot produce duplicate or reordered steps.
- **Durability.** Each record is `flush`ed and `fsync`ed before the lock is
  released. A record is therefore complete on disk or absent — never half
  applied to the numbering.
- **Crash recovery.** A process killed mid-write can leave a partial trailing
  line. Readers keep the valid prefix; the next append truncates the torn tail
  before writing. Destructive readers (`prune`) instead load strictly and abort,
  because a torn tail may hold the newest snapshot reference and treating its
  absence as fact would delete live data.
- **Cost.** Step allocation reads a size-keyed sidecar cache rather than
  re-parsing the journal: measured 9.2 ms cold and 0.30 ms warm at 3,000
  records. A sidecar that does not match the file size is discarded and the
  full repairing load runs instead.

### Snapshot lifecycle and retention

- **When.** Snapshots are taken at mutation boundaries — before and after
  `apply_patch` — not on every event.
- **No-op suppression.** If the worktree tree is identical to the session's
  previous snapshot, that snapshot is reused and no new ref is created.
- **Pinning.** Each snapshot is a commit pinned under `refs/spotter/steps/`, so
  `gc` cannot remove a state a later fork depends on. The user's index, HEAD,
  and worktree are never touched; restores go to a detached worktree.
- **Reuse re-pins.** A deduped snapshot is re-pinned on every reuse. A pruned
  commit stays resolvable until `gc` runs, so reusing one without restoring its
  ref would hand the journal a sha that `gc` later destroys.
- **Retention.** `spotter prune` deletes snapshots no journal references, in a
  single `update-ref --stdin` transaction, dry-run by default. Referenced
  snapshots are kept indefinitely, because deleting one destroys the ability to
  fork that step. `--max-age-days N` opts into expiring referenced snapshots
  too. Age is measured from a snapshot's **creation**, and dedup reuses an
  unchanged snapshot without refreshing it, so expiry drops old *states* rather
  than old *steps*: a step recorded today that references a month-old unchanged
  state loses its fork with it. Because that cost is easy to miss, `prune`
  prints the exact `session`/`step` pairs each expired snapshot takes down, and
  the flag is never the default.

### Audit ledger (claim/evidence)

The reviewer does not treat the agent's own account as fact. A ledger is
rebuilt from the journal before every review:

- **Evidence** comes only from observable outcomes of tool calls. A reasoning
  summary can never become evidence — `EvidenceSource` has no member for it,
  so mypy rejects the call site.
- **Hypotheses** come from the reviewer's own flagged assumptions and enter as
  `unverified`.
- **Retraction is mechanical**: when the same command later produces a
  different outcome, the earlier outcome is retracted and every hypothesis
  resting solely on it goes stale, transitively. Stale premises are listed in
  the next review prompt so a killed assumption cannot quietly stay in view.

**Measured limit.** Only 33 of 340 real Codex tool results (10%) carry an
observable outcome at all — Codex reports an exit code for `apply_patch` and
nothing for shell commands. The ledger records outcomes where they exist and
stays silent where they do not; inferring pass/fail from output text would
retract evidence every time `git status` changed. This is an
observability-ceiling fact (P1), not a parser gap, and it bounds how much
stale-premise detection can do on Codex today.

### Gate ambiguity policy

Deterministic gates judge a parsed token stream. Where the parse cannot support
a decision, the gate **fails open** and says so, rather than guessing:

| Class | Behaviour | Telemetry |
| --- | --- | --- |
| Command that cannot be tokenized | allow, rule `unparseable_command` | `gate_fail_open` |
| Absolute path with no known workspace root | allow, rule `unknown_workspace` | `gate_fail_open` |
| Everything else | allow or block per rule | `gate_shadow_block` / `gate_block` |

Every fail-open decision is journaled as a `gate_fail_open` record carrying the
rule that abstained, so blindness is countable rather than invisible. Those
records surface in `spotter analyze` and are counted by `spotter metrics` as
**blind spots, reported separately from the false-positive rate**. They are
deliberately not labelable: a fail-open is an abstention, not a judgment, so
scoring it `tp`/`fp` would mix "the gate was wrong" with "the gate could not
tell" in one number.

## Codex integration points

Current official Codex documentation exposes the primitives that make Spotter plausible:

### Hooks

Codex supports lifecycle hooks such as `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and stop-related hooks.

`PreToolUse` is particularly important because it can run before supported tool execution and can influence whether/how the call proceeds.

Official documentation:

- https://developers.openai.com/codex/hooks

### App Server

Codex App Server exposes a programmatic thread/turn interface and streamed runtime events suitable for building richer clients and orchestration around active Codex work.

Spotter is particularly interested in:

- streamed turn/item events
- `turn/steer` for in-flight course correction
- `turn/interrupt` for terminating an active turn

Official documentation:

- https://developers.openai.com/codex/app-server

## First implementation boundary

The first implementation should intentionally avoid several advanced ideas:

- no model training
- no full graph database
- no automatic harness self-repair
- no general rollback engine
- no multi-agent debate
- no requirement for hidden reasoning traces

The MVP should prove one thing first:

> **Can an independent runtime spotter detect a useful subset of trajectory failures early enough that intervention improves task outcomes without becoming an expensive source of false positives?**
