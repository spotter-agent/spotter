# Roadmap

> **Status:** reset around the standalone-runtime direction in [#66](https://github.com/Bogyie/spotter/issues/66).  
> This roadmap separates **runtime/product work** from **evaluation work**. Shipping more mechanisms does not substitute for proving that intervention helps.

---

## 30-second summary

### Now

**P0 — App Server lifecycle / attach PoC**

Prove that `codex --remote <endpoint>` and Spotter can share the same external App Server,
observe the same thread/turn, and that `turn/steer` reaches the actual user session. Plain
`codex` auto-discovery has already been rejected; see [validation](app-server-validation.md).

### Next

```text
P1  standalone runtime foundation
 ↓
P2  product lifecycle: package/setup/status/doctor/teardown
 ↓
P3  move existing capabilities behind spotterd
 ↓
P4  App Server primary observation + Hook minimization
```

### Then

```text
P5  cheap signals → event-driven reviewer
 ↓
P6  live VERIFY / NUDGE
 ↓
P7  INTERRUPT / RESTART / side-effect-aware recovery
 ↓
P8  operational hardening
 ↓
P9  adaptation / learned policy only after evidence is strong enough
```

The corresponding evaluation path is:

```text
E0  observability ceiling
 ↓
E1  mechanically scored task set
 ↓
E2  replay/fork fidelity
 ↓
E3  precision + miss rate
 ↓
E4  intervention advantage / harm
 ↓
E5  real-session operational A/B
```

---

## Quick phase table

| Phase | Outcome | Hard dependency | Done when... |
| --- | --- | --- | --- |
| **P0** | App Server strategy selected | none | same real Codex turn can be observed and steered |
| **P1** | long-lived runtime boundary | P0 | `spotterd` owns live multi-thread state and reconnects |
| **P2** | product lifecycle | P1 | clean install → setup → `codex` → teardown works |
| **P3** | current features moved behind daemon | P1 | journal/gate/audit/reviewer/snapshot no longer depend on per-hook state rebuild |
| **P4** | App Server primary observation | P0–P3 | observation hooks removed where coverage is proven |
| **P5** | event-driven review | P4 | reviewer is triggered mainly by cheap candidate signals |
| **P6** | live soft intervention | P5 + E3/E4 evidence | `VERIFY/NUDGE` can reach correct active turn safely |
| **P7** | strong control/recovery | P6 + strong evidence | interrupt/restart operate with side-effect awareness |
| **P8** | operational hardening | P1–P7 | upgrades/crashes/purge/reinstall are boring and diagnosable |
| **P9** | adaptation | E4/E5 mature | learned policy can be evaluated for regressions |

---

# Baseline: what already exists

## Implemented in the current prototype

- Codex lifecycle/tool Hook ingestion;
- deterministic pre-action gates;
- shell-aware handling of several destructive command classes;
- crash-tolerant step journals;
- Git-backed snapshots and detached restore;
- snapshot deduplication and pruning;
- Codex continuation fork/replay machinery;
- model-backed reviewer in shadow mode;
- periodic/detached reviewer execution;
- typed claim/evidence ledger with stale propagation where outcomes are observable;
- human labels and coverage-aware metrics;
- same-prefix counterfactual experiment harness;
- Codex and Claude Code plugin packaging.

## Missing or unproven

- standalone runtime distribution/lifecycle;
- `spotterd` live-state ownership;
- App Server as primary Codex observation/control plane;
- event-driven signal layer;
- live `VERIFY / NUDGE` delivery;
- `INTERRUPT / RESTART`;
- complete side-effect/reversibility handling;
- a mechanically scored task corpus with enough executed experiments;
- detector miss-rate/recall evidence;
- positive intervention advantage;
- full runtime/operational cost accounting.

The project is therefore **implementation-rich but evidence-poor**: substantial machinery exists, but the central causal claim is still open.

---

# Runtime / product track

## P0 — App Server lifecycle / attach PoC

**Priority:** first.  
**Why:** every later App Server/daemon decision depends on this.

### Entry criteria

- current Codex version exposes App Server client/control surfaces worth testing;
- Spotter can run local integration experiments without changing production policy.

### Questions to answer

1. ~~Does plain `codex` reuse a pre-existing default external App Server daemon?~~ **No** — current docs and CLI source require `--remote`; see [validation](app-server-validation.md).
2. Can Spotter attach as a second client to the exact same server?
3. Do TUI and Spotter observe the same thread id and active turn id?
4. Does `turn/steer` affect the actual user-visible turn?
5. What happens with multiple simultaneous TUI sessions?
6. Which launcher/alias/wrapper can supply `--remote` safely across normal, resume, and failure paths?
7. What can Spotter still do when Codex chooses an embedded server?
8. What lifecycle/ownership guarantees does Codex's experimental daemon actually provide?

### Experiment A — explicit remote TUI

```text
start external Codex App Server
        ↓
`codex --remote <endpoint>`
        ↓
TUI connects to the selected server
        ↓
Spotter attaches as client B
        ↓
observe same thread/turn
        ↓
turn/steer reaches user session
```

Record:

- daemon endpoint/path;
- Codex CLI/version/config;
- thread ids seen by both clients;
- turn ids over several actions;
- event duplication/order;
- steer latency and visible result;
- disconnect/reconnect behavior.

### Experiment B — Spotter-managed launch UX

```text
Spotter starts external `codex app-server`
        ↓
TUI attaches same endpoint
        ↓
Spotter attaches same endpoint
```

Answer whether a normal plain-`codex` UX can still be preserved without relying on Codex's managed-daemon lifecycle.

### Experiment C — embedded baseline

Run ordinary Codex without an attachable external server and write down the real capability matrix:

```text
thread observation:    ?
tool result visibility:?
live steer:            ?
interrupt:             ?
PreToolUse gate:       ?
```

### Non-goals

- no daemon rewrite;
- no new reviewer policy;
- no Homebrew packaging;
- no Hook migration yet.

### Exit criteria

- one canonical App Server strategy selected;
- ordinary `codex` UX remains acceptable;
- same-thread/same-turn observation is proven;
- real `turn/steer` delivery is proven;
- concurrent sessions are understood;
- degraded/embedded mode is documented;
- ownership and reconnect rules are known.

**If a core property fails, stop and revise #66 before P1.**

---

## P1 — Standalone runtime foundation

### Goal

Create the smallest long-lived runtime boundary that can own state and connections.

### Entry criteria

- P0 selected an App Server integration strategy.

### Deliverables

#### `spotterd`

- per-user daemon process;
- CLI control RPC;
- graceful start/stop/restart;
- explicit protocol version.

#### Runtime state

- `ThreadState` registry;
- `TurnState` / active-turn identity;
- `RuntimeAttachment` identity;
- capability/connection state;
- multi-thread isolation.

#### App Server client

- connect/initialize;
- capability probe;
- event subscription;
- reconnect state machine;
- list/reconcile loaded threads.

#### Hook IPC

- Unix-domain-socket request/reply for `PreToolUse`;
- bounded timeout;
- fail-open when unavailable;
- startup/protocol handshake.

#### Service abstraction

- `ServiceManager` interface;
- managed-vs-portable runtime mode;
- defer exact launchd/systemd behavior until P0 result requires it.

### Non-goals

- no native hook rewrite unless measurements demand it;
- no event-driven reviewer yet;
- no live steering policy beyond a PoC control request.

### Exit criteria

- one `spotterd` handles multiple concurrent thread states;
- App Server events update memory without journal reconstruction;
- CLI can query daemon health/state;
- daemon restart reconnects and reconciles active threads;
- a dead daemon does not break the coding session.

---

## P2 — Product lifecycle

### Goal

Make Spotter installable and removable as a product, not merely runnable from a checkout.

### Entry criteria

- P1 runtime exists locally.

### Deliverables

```text
brew install spotter
spotter setup codex
spotter status
spotter doctor
spotter teardown codex
```

Implementation:

- release artifacts/Homebrew formula;
- package provenance/version detection;
- transactional setup (`INSPECT → PLAN → BACKUP → APPLY → VERIFY → COMMIT`);
- `IntegrationManifest`;
- legacy plugin detection/migration;
- service registration if required;
- App Server strategy installation/reconciliation;
- minimal Hook installation;
- synthetic `doctor` checks;
- teardown and interrupted-setup recovery;
- stable executable paths across upgrades.

### Fixtures/tests

- clean machine fixture;
- existing Codex config fixture;
- existing legacy Spotter plugin fixture;
- interrupted setup at each transaction stage;
- repeated setup three times;
- uninstall without teardown.

### Exit criteria

- clean install → setup → plain `codex` → teardown works;
- setup is idempotent;
- interrupted setup can be reconciled;
- Spotter removes only its own config changes;
- uninstall without teardown cannot make Codex unusable.

Detailed contract: [Lifecycle](lifecycle.md).

---

## P3 — Move existing capabilities behind the runtime boundary

### Goal

Preserve useful prototype behavior while changing who owns live state.

### Migrate/refactor

- journal writer;
- Trace IR normalization;
- audit ledger;
- deterministic gate;
- snapshot manager;
- fork/replay manager;
- reviewer scheduler;
- labels/metrics integration;
- experiment harness.

### Required architecture change

```text
BEFORE
hook → journal → rebuild state

AFTER
event → live state → policy
                  └→ journal append
```

### Specific work

- daemon becomes primary journal writer where possible;
- audit ledger updates incrementally from Trace IR;
- reviewer context is read from live state + bounded durable data, not full journal rebuild;
- snapshot calls become managed resources linked to thread/turn identity;
- existing commands continue to work against durable history.

### Exit criteria

- current safety tests remain valid;
- current session analysis/fork/metrics workflows still work;
- normal event handling does not reconstruct full state from disk;
- daemon restart/resume is the explicit reconstruction boundary.

---

## P4 — App Server primary observation + Hook minimization

### Goal

Move broad observation off Hooks and prove no critical signal is lost.

### Migration matrix

| Hook | Target |
| --- | --- |
| `SessionStart` | remove if App Server thread lifecycle covers it |
| `UserPromptSubmit` | remove if App Server user-message events cover it |
| `PreToolUse` | retain only for bounded deterministic enforcement |
| `PostToolUse` | remove in favor of App Server tool/result/diff events |

### Deliverables

- App Server event → Trace IR adapter;
- lifecycle/user-message/tool/result/diff/token coverage map;
- event ordering/idempotency handling;
- provenance from Trace IR back to App Server item/turn;
- reduced Hook configuration;
- Hook latency and invocation count measurement;
- re-measured observable-outcome ceiling.

### Measurement

Compare before/after:

```text
hook invocations / session
hook p50 / p95 / p99
observable tool-result rate
missing goal/constraint rate
App Server event loss/duplication
CPU/memory overhead
```

### Exit criteria

- App Server is the primary trajectory source;
- no required observation silently disappears;
- the old 10% hook-era outcome figure is replaced with a measured App Server-era result;
- the remaining Hook path is small, bounded, and deterministic.

---

## P5 — Event-driven semantic review

### Goal

Replace fixed periodic model calls with cheap candidate detection.

### Target flow

```text
runtime event
    ↓
cheap signal / verifier
    ↓
possible issue?
    ├─ no → done
    └─ yes
         ↓
     ReviewerJob
```

### Initial candidate signals

- repeated equivalent tool calls;
- failure streaks;
- repeated reads with no frontier expansion;
- sudden touched-scope growth;
- edits accumulating without validation;
- stale hypothesis reuse;
- repeated behavior after a deterministic block;
- tool/token/time budget anomalies.

### Data contract

Candidate example:

```json
{
  "candidate": "POSSIBLE_EXPLORATION_LOOP",
  "thread_id": "T1",
  "turn_id": "U8",
  "evidence": ["same search pattern repeated 4 times"],
  "signal_version": 1
}
```

A signal is not a verdict.

### Exit criteria

- candidate signals are journaled and labelable;
- reviewer calls/session decrease or become more targeted;
- detection latency/precision/miss rate are measured before and after;
- cadence, if retained, is an explicit fallback rather than the primary trigger.

---

## P6 — Live soft intervention

### Goal

Deliver `VERIFY / NUDGE` while Main continues in parallel.

### Entry criteria

- P5 produces reviewer jobs;
- E3 has enough precision/miss-rate evidence to set a conservative live policy;
- E4 has at least initial intervention-harm evidence.

### Deliverables

#### Reviewer job lifecycle

```text
QUEUED → RUNNING → DECIDED
                    ├─ DELIVERED
                    ├─ STALE
                    ├─ DISCARDED
                    └─ FAILED
```

#### Freshness

- every job anchors to `thread_id` + `target_turn_id`;
- only the intended active turn receives the verdict;
- late decisions follow explicit stale/defer policy.

#### Delivery

```text
VERIFY → concise evidence request via turn/steer
NUDGE  → concise course correction via turn/steer
```

#### Measurement

- reviewer latency;
- delivery latency;
- stale rate;
- ignored intervention rate;
- duplicate delivery prevention;
- user-visible supervision provenance.

### Exit criteria

- live decision reaches the correct real active Codex turn;
- the same decision cannot be delivered twice;
- stale decisions do not leak into unrelated turns;
- Main can distinguish Spotter supervision from user intent;
- live intervention remains off by default until measurement gates are met.

---

## P7 — Strong control and recovery

### Goal

Add higher-cost actions only after soft intervention is understood.

### `INTERRUPT`

Requirements:

- high precision threshold;
- shadow “would interrupt” mode first;
- `turn/interrupt` control path;
- clear post-interrupt replanning contract.

### `RESTART`

Requirements:

```text
last sound state
verified evidence set
checkpoint/snapshot lineage
explicit retained artifacts
external side-effect ledger
fresh continuation payload
```

Restart payload should be deliberately minimal:

```text
user goal
explicit constraints
verified evidence
current repository state
retained artifacts
```

Do not imply that external writes were rolled back.

### Exit criteria

- strong actions have explicit precision/harm budgets;
- shadow evidence precedes active control;
- external side effects remain visible in recovery UX;
- restart lineage is reproducible and auditable.

---

## P8 — Operational hardening

### Goal

Make months of use boring.

### Scope

- daemon/App Server reconnect;
- crash recovery;
- Codex upgrade capability negotiation;
- Spotter upgrade with running old daemon;
- config/journal/label/experiment schema migration;
- repository registry;
- retention policy;
- worktree/snapshot cleanup;
- `purge --dry-run`;
- uninstall/reinstall fixtures;
- multi-agent integration lifecycle;
- logs/diagnostic bundle if useful.

### Exit criteria

- upgrade and reinstall fixtures pass across supported schema ranges;
- stale resources are discoverable and cleanable;
- `status`/`doctor` explain partial failures;
- package uninstall and integration teardown have separate, predictable ownership.

---

## P9 — Experience and adaptation

Do not build learned/adaptive intervention policy until the evaluation harness can detect regressions.

Possible later directions:

- model-specific failure profiles;
- repository-specific signal thresholds;
- retrieval of prior useful/harmful interventions;
- offline policy learning;
- personalized intervention tolerance.

Entry gate:

- E4/E5 produce enough repeatable evidence to compare policy versions.

---

# Evaluation track

The evaluation track advances alongside runtime work.

## E0 — Observability ceiling

### Question

What fraction of important failures are visible **early enough to act** on the chosen observation surface?

Measure:

- directly observable tool outcomes;
- visible goal/constraint context;
- failure classes that are structurally invisible;
- earliest step/turn where a warranted intervention becomes observable.

Re-measure after App Server migration.

---

## E1 — Ground-truth task set

Build a small reproducible corpus where success is mechanically decidable.

Each task needs:

```text
repository/fixture version
user prompt
known initial failing state
objective check command
provenance
cost budget
```

Possible source: a carefully selected SWE-bench Verified subset or equivalent local fixtures.

Exit criterion: `spotter experiment --run --check ...` completes across enough tasks to produce real control/guidance outcomes.

---

## E2 — Replay/fork fidelity

Before using forks for causal claims, measure the instrument.

Experiments:

- identical control vs identical control;
- repeated continuations from the same prefix;
- worktree/environment failure rate;
- model nondeterminism distribution.

Report a noise floor before interpreting intervention deltas.

---

## E3 — Precision and miss rate

Positive labels alone are insufficient.

Measure:

- precision / false-positive rate on emitted candidates/verdicts;
- sampled miss rate / recall estimate on unflagged trajectories;
- coverage per failure class;
- abstention/blind-spot rate separately from incorrect verdicts.

A detector that never fires should not appear perfect.

---

## E4 — Intervention advantage and harm

For a shared prefix:

```text
control: continue
vs
intervention: VERIFY / NUDGE / later stronger action
```

Classify outcomes:

```text
intervention better
control better (harm)
tied
incomplete / infrastructure failure
```

Report sample size and coverage. Do not enable live intervention by default from anecdotal success.

Also test **wrong-nudge susceptibility**: Main should be able to reject plausible but incorrect Spotter advice when evidence contradicts it.

---

## E5 — Real-session operational A/B

Eventually compare full runtime configurations:

```text
A. Vanilla Codex
B. Periodic async reviewer
C. Event-driven Spotter
D. Event-driven Spotter + deterministic gate
E. Full Spotter with stronger recovery/control
```

This answers a population-level question different from same-prefix forks: **does running Spotter in real work improve outcomes enough to justify its cost?**

---

# Primary metrics

| Dimension | Metrics |
| --- | --- |
| Outcome | task success/resolution, regression rate |
| Main efficiency | main tokens, tool calls, repeated actions, elapsed/tool time |
| Spotter cost | reviewer tokens, model calls, CPU/memory, journal/storage growth |
| Observation | event coverage, observable outcome rate, missing goal/constraint rate, disconnect rate |
| Detection | precision, FP rate, miss-rate/recall estimate, abstention/blind spots |
| Intervention | intervention advantage, harm rate, recovery, ignored rate |
| Timing | detection delay, reviewer latency, delivery latency, stale rate, wasted actions before intervention |
| Recovery | success after nudge/interrupt/restart, retained useful work, unreverted external effects |
| Operations | Hook p50/p95/p99, reconnect time, daemon recovery time, upgrade/migration failure rate |

---

# Intervention evidence budgets

The stronger the action, the stronger the evidence requirement.

```text
VERIFY
  can tolerate uncertainty because it asks for evidence

NUDGE
  conservative semantic intervention

BLOCK
  near-deterministic policy only

INTERRUPT
  very high precision + shadow evidence first

RESTART
  extremely rare, auditable, likely user-visible/confirmable initially
```

Exact thresholds should come from evaluation, not intuition.

---

# Revised MVP

A useful public Spotter does **not** require a graph database, learned policy, or automatic rollback engine.

It must prove this loop on the standalone runtime boundary:

```text
1. Observe the real active coding-agent trajectory.
2. Maintain independent live state for goal, constraints, hypotheses, evidence, and progress.
3. Generate a small set of cheap candidate failure signals.
4. Use an independent reviewer only for ambiguous candidates.
5. Deliver async VERIFY/NUDGE to the correct active turn or deterministic BLOCK before execution.
6. Record timing, cost, freshness, delivery, and outcome.
7. Demonstrate that intervention helps more than it harms.
```

The architecture is successful only if it enables this experiment without itself becoming a major source of latency, fragility, or operational burden.

For current status, see [Status](status.md). For concrete runtime contracts, see [Architecture](architecture.md). For product lifecycle, see [Lifecycle](lifecycle.md).
