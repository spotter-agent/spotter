# Roadmap

> **Status:** roadmap reset around the standalone runtime direction in [#66](https://github.com/Bogyie/spotter/issues/66).

Spotter should still earn complexity through evaluation, but the implementation order needs to change.

The hook/plugin prototype already proved enough of the idea to expose a structural problem: the next features—event-driven detection, live steering, richer audit state, interruption, multi-session supervision—want a long-lived runtime and a richer observation/control channel. Building them deeply into the current per-hook-process model would create work that must immediately be migrated again.

The roadmap therefore splits into two tracks that must advance together:

```text
Runtime/Product track
  build the architecture that can supervise reliably

Evaluation track
  prove that the supervision actually helps
```

Neither substitutes for the other.

## Current baseline

### Implemented in the prototype

- Codex lifecycle/tool hook ingestion
- deterministic pre-action gates
- shell-aware handling for several destructive command classes
- crash-tolerant step journals
- Git-backed snapshots and detached restore
- snapshot dedup/pruning
- Codex continuation fork/replay machinery
- shadow-mode model-backed reviewer
- periodic/detached reviewer execution
- typed claim/evidence ledger with stale propagation where outcomes are observable
- labels and coverage-aware metrics
- same-prefix counterfactual experiment harness
- Codex and Claude Code plugin packaging

### Still missing or unproven

- standalone runtime distribution and lifecycle
- `spotterd` live state ownership
- App Server as Codex's primary observation/control plane
- event-driven signal layer
- live `VERIFY` / `NUDGE` delivery
- `INTERRUPT` / `RESTART`
- complete side-effect/reversibility handling
- a ground-truth task corpus and enough executed experiments to estimate intervention advantage
- recall/miss-rate evidence for detectors
- full efficiency/cost accounting

This means the project is **implementation-rich but evidence-poor**: the measurement machinery exists, but the central claim that Spotter improves outcomes is not yet established.

---

# Runtime / product roadmap

## P0 — App Server lifecycle and attach PoC

**Priority: first.**

Before building the daemon migration, validate the premise that makes the target Codex architecture possible.

### Goal

Prove that the user's ordinary Codex TUI and Spotter can share the same externally reachable App Server, and that Spotter can observe and steer the real active turn.

### Experiments

#### A. Codex-managed daemon

```text
external App Server daemon
        ↓
plain `codex` attaches
        ↓
Spotter attaches as second client
        ↓
receive same thread/turn events
        ↓
turn/steer reaches the user session
```

#### B. Spotter-managed external App Server

Test whether Spotter can safely ensure the external App Server when relying on Codex's experimental daemon lifecycle is undesirable.

#### C. Embedded/degraded baseline

Document exactly what Spotter can and cannot do when Codex chooses an embedded App Server that Spotter cannot attach to.

### Exit criteria

- canonical App Server ownership/attach strategy chosen
- ordinary `codex` UX remains possible
- Spotter receives live events for the user's real thread
- active turn identity is reliable
- `turn/steer` works end to end
- multi-client/multi-session behavior is understood
- degraded mode is explicit

If this phase fails, revisit the target architecture before continuing.

## P1 — Standalone runtime foundation

### Goal

Create the smallest long-lived runtime boundary without yet rewriting every supervision feature.

### Scope

- `spotterd` process/runtime
- user-service/lifecycle abstraction where required
- Unix-domain-socket IPC for the remaining synchronous hook bridge
- App Server manager/client
- capability negotiation
- thread / turn / runtime-attachment identity model
- live state container
- connection/reconnect state machine
- protocol/version handshake

### Design constraint

Do not rewrite the whole project in Rust/Go. The current Python implementation is sufficient to validate the runtime boundary. A native `spotter-hook` is a later optimization if cold-start measurements justify it.

### Exit criteria

- daemon can own more than one concurrent session/thread state
- App Server event stream updates live memory
- daemon restart can reconnect/reconcile state
- current coding session does not fail when Spotter is unavailable

## P2 — Product lifecycle

### Goal

Make the runtime installable, diagnosable, upgradable, and removable as a product rather than a development harness.

### Scope

Target distribution/control plane:

```text
brew install spotter
spotter setup codex
spotter doctor
spotter status
spotter teardown codex
```

Implementation areas:

- Homebrew/release packaging
- `setup` transaction
- integration manifest
- legacy plugin migration
- service registration if managed mode requires it
- `doctor` and `status`
- `teardown`
- stable executable paths across package upgrades
- config/schema versioning foundation

### Exit criteria

- clean install → setup → ordinary `codex` → teardown round trip works
- setup is idempotent
- partial setup failure is recoverable
- uninstall without teardown cannot break Codex through a dangling Spotter hook

See [Lifecycle](lifecycle.md) for the complete target lifecycle.

## P3 — Move existing capabilities behind the runtime boundary

### Goal

Preserve the useful prototype mechanisms while changing who owns their live state.

### Move/refactor

- journal writer
- Trace IR/state updates
- audit ledger
- gate engine
- snapshot manager
- replay/fork manager
- reviewer scheduler
- labels/metrics integration
- experiment harness

### Key change

```text
before:
hook → journal → rebuild state

after:
event → live state → policy
                  └→ durable journal
```

### Exit criteria

- current tests/guarantees remain meaningful
- journal becomes durable history/recovery, not the hot-path state database
- reviewer does not rebuild whole state from disk on every invocation

## P4 — Make App Server primary and minimize hooks

### Goal

Move broad observation away from hooks.

### Target migration

| Hook | Target state |
| --- | --- |
| `SessionStart` | remove if App Server lifecycle covers it |
| `UserPromptSubmit` | remove if App Server user-message events cover it |
| `PreToolUse` | retain only for bounded deterministic blocking |
| `PostToolUse` | remove in favor of App Server results/diffs |

### Scope

- App Server → Trace IR normalization
- thread/turn lifecycle handling
- command/result/diff coverage
- reasoning/plan signals where exposed
- token/cost observation where exposed
- re-measure observability ceiling
- measure hook latency/invocation reduction

### Exit criteria

- App Server is the primary trajectory source
- no required observation silently disappears when hooks are removed
- existing 10% hook-era outcome observability figure is replaced with a measured App Server-era figure
- synchronous hook path is small and bounded

## P5 — Event-driven semantic supervision

### Goal

Replace fixed periodic review with cheap candidate signals followed by semantic review only when warranted.

```text
runtime event
    ↓
cheap signal / verifier
    ↓
possible issue?
    ├─ no → continue
    └─ yes
         ↓
     Pair Reviewer
```

Candidate signals:

- repeated equivalent actions
- failure streaks
- stagnant exploration frontier
- touched-scope growth
- edits without validation
- stale premise reuse
- block circumvention
- budget anomalies

Signals produce candidates, not verdicts.

### Exit criteria

- candidate signals are journaled and labelable
- reviewer dispatch is primarily event-driven
- calls/session and detection behavior are measured before/after the change

## P6 — Live soft intervention

### Goal

Deliver useful reviewer decisions without blocking Main while the reviewer thinks.

### Runtime actions

- `CONTINUE`
- `VERIFY`
- `NUDGE`

### Flow

```text
signal at turn T
    ↓
reviewer runs asynchronously
    ↓
Main continues
    ↓
verdict ready
    ↓
T still active?
  ├─ yes → turn/steer
  └─ no  → stale/defer/discard policy
```

### Required support

- reviewer job state machine
- target turn identity
- intervention freshness policy
- delivery journaling
- ignored/stale intervention metrics
- pair-runtime contract for Main

### Exit criteria

- a shadow-earned reviewer decision can reach the active real Codex turn
- repeat delivery is impossible
- stale decisions do not leak into unrelated later turns
- intervention latency and stale rate are measurable

## P7 — Strong control and recovery

### Goal

Add high-cost actions only after soft intervention has evidence.

### Runtime actions

- `BLOCK` — already deterministic, but re-measured in new runtime
- `INTERRUPT`
- `RESTART`

### Required support

- high precision budgets per action
- last-sound-state representation
- checkpoint metadata
- side-effect/reversibility ledger
- explicit retained-state selection
- restart lineage

### Restart payload

A fresh continuation should receive only what is deliberately retained:

- user goal
- explicit constraints
- verified evidence
- current repository state
- intentionally preserved artifacts

Do not imply that a reasoning restart undoes external state.

## P8 — Operational hardening

### Scope

- daemon/App Server reconnect recovery
- Codex upgrade capability negotiation
- Spotter upgrade and graceful daemon restart
- config/journal/label/experiment schema migration
- retention policies
- repository registry
- purge
- reinstall
- multi-agent integration lifecycle

### Goal

A Spotter installation should remain understandable after months of normal upgrades, crashes, resumes, and repository churn.

## P9 — Experience and adaptation

Only after intervention quality is measurable should Spotter learn from prior runs.

Possible directions:

- model-specific failure profiles
- repository-specific signal thresholds
- retrieval of previous useful/harmful interventions
- offline learning of intervention policy

Do not introduce adaptive behavior without an evaluation harness capable of detecting regressions.

---

# Evaluation roadmap

The runtime migration does not relax the project's measurement discipline. It makes better measurement possible.

## E0 — Observability ceiling

Re-measure what failures are visible before they compound after App Server becomes the observation surface.

Questions:

- what fraction of failed/degraded sessions were observable early enough?
- what fraction of tool outcomes are directly observable?
- which failure classes remain invisible?

A smarter reviewer cannot beat an observation surface that never exposes the relevant evidence.

## E1 — Ground-truth task set

Create a small reproducible task corpus with mechanical checks so counterfactual experiments can actually run.

Requirements:

- fixture repository/task
- prompt
- objective `--check`
- provenance
- cost budget

The first measured intervention result should be recorded even if negative or inconclusive.

## E2 — Replay/fork fidelity

Before causal claims rely on fork pairs, measure the instrument itself:

- identical-arm noise floor
- continuation similarity
- environmental failure rate
- effect of detached worktree differences

Any claimed intervention effect should be interpreted relative to this noise floor.

## E3 — Precision and miss rate

Positive-only labeling is insufficient.

Measure both:

- precision / false-positive rate on flags and reviewer findings
- sampled miss rate / recall estimate on unflagged trajectories or proposals

A detector that never fires should not appear perfect.

## E4 — Intervention advantage and harm

Use same-prefix counterfactuals to ask:

> Did this intervention improve the outcome compared with continuing from the same prefix?

Report at least:

- guidance better
- control better (harm)
- tied
- incomplete/failed

Do not enable live injection by default before useful intervention advantage and wrong-nudge/sycophancy behavior are measured.

## E5 — Operational effect

Eventually compare real-session configurations:

```text
A. Vanilla Codex
B. Periodic async reviewer
C. Event-driven Spotter
D. Event-driven Spotter + deterministic gate
E. Full Spotter with stronger control
```

This answers a different question from fork pairs: whether running Spotter at all improves real work across a population of sessions.

---

# Primary metrics

## Outcome

- task success / resolution
- regression rate

## Efficiency

- main-agent tokens
- reviewer tokens
- tool calls
- repeated-action count
- elapsed/tool-execution time
- Spotter CPU/memory overhead

## Observation quality

- observable outcome rate
- event coverage
- missing-goal/constraint rate
- supervision liveness

## Intervention quality

- interventions per run
- precision
- miss rate / sampled recall
- intervention advantage
- harm rate
- recovery rate
- ignored-intervention rate

## Timing

- steps from warranted intervention range to verdict
- wasted actions before intervention
- reviewer latency
- delivery latency
- stale intervention rate

## Recovery

- success after nudge
- success after interrupt
- success after restart
- useful work retained across restart
- external effects left unreverted

## Operations

- hook p50/p95/p99
- App Server reconnect latency
- daemon restart/recovery latency
- storage growth
- migration/upgrade failures in fixtures

---

# False-positive budgets

Strong interventions need increasingly strict evidence.

Initial philosophy:

- `VERIFY`: can tolerate uncertainty because it requests evidence
- `NUDGE`: conservative
- `BLOCK`: near-deterministic
- `INTERRUPT`: very high confidence
- `RESTART`: extremely rare and likely user-visible/confirmable

Exact thresholds should be learned from evaluation rather than chosen once from intuition.

---

# Revised MVP definition

A useful public Spotter does not need a graph database, learned policy, or automatic rollback engine.

It needs to demonstrate this loop reliably **on the standalone runtime boundary**:

```text
1. Observe an active coding-agent trajectory through a reliable runtime surface.
2. Maintain independent live state for goal, constraints, hypotheses, evidence, and progress.
3. Detect a small set of trajectory problems with cheap signals/verifiers.
4. Ask an independently configured reviewer to verify ambiguous candidates.
5. Intervene with async VERIFY/NUDGE or deterministic BLOCK.
6. Record timing, cost, delivery, and outcome.
7. Measure whether intervention helped more than it harmed.
```

The architecture is successful only if it enables this experiment without itself becoming a major source of latency, fragility, or operational burden.
