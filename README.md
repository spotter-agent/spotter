Maintained by [@bogyie / Bogyoeng Kim](https://github.com/Bogyie) and [@zerone / Youngjin Jung](https://github.com/YoungJinJung)

# Spotter

> **A runtime spotter for coding agents.**  
> Your coding agent drives. Spotter watches the trajectory, challenges bad assumptions, and steps in before wasted work compounds.

Spotter is an experimental **runtime supervision system for coding agents**, starting with Codex.

---

## 30-second summary

Spotter is interested in a more specific question than “is the agent wrong?”

> **Can we detect a bad assumption, loop, scope drift, or missing validation while the agent is still working, and intervene before the mistake becomes expensive?**

The repository is already beyond a scaffold. The current prototype implements hook-based trajectory collection, deterministic gates, crash-safe journals, Git snapshots, fork/replay, a shadow reviewer, claim/evidence state, labels/metrics, and counterfactual experiment machinery.

The current prototype and the target product architecture are different:

```text
CURRENT PROTOTYPE

Codex / Claude Code
       │ hooks
       ▼
 spotter-hook process
       │
       ├─ journal
       ├─ deterministic gate
       ├─ snapshot
       └─ periodic shadow review


TARGET ARCHITECTURE

Codex TUI
    │
    ▼
External Codex App Server
    ↕ events / steer / interrupt
 spotterd
    │
    └─ PreToolUse Hook
       (deterministic synchronous enforcement only)
```

The immediate next step is **not** to blindly build the daemon. First we must prove that the ordinary Codex TUI and Spotter can share the **same external App Server**, observe the same thread/turn, and that Spotter can steer the real active turn. That is P0 in the roadmap.

For the fastest project snapshot, read [Status](docs/status.md). To browse all documentation by question, start at [Documentation](docs/README.md). The umbrella design decision lives in [#66](https://github.com/Bogyie/spotter/issues/66).

---

## Quick navigation

| You want to know... | Read |
| --- | --- |
| What works today | [Current status](#current-status) / [docs/status.md](docs/status.md) |
| What Spotter is trying to solve | [Core idea](#core-idea) / [docs/concept.md](docs/concept.md) |
| The exact target process/data flow | [Target architecture](#target-architecture) / [docs/architecture.md](docs/architecture.md) |
| Install → setup → run → recover → upgrade → remove | [docs/lifecycle.md](docs/lifecycle.md) |
| What to build next and why | [docs/roadmap.md](docs/roadmap.md) |
| Research basis and evidence gaps | [docs/research.md](docs/research.md) |
| All docs by question/reading path | [docs/README.md](docs/README.md) |
| Umbrella direction | [#66](https://github.com/Bogyie/spotter/issues/66) |

---

## Current status

Legend: ✅ implemented · 🟡 partial/shadow · 🧪 PoC required · 🎯 target · ❌ not implemented

| Capability | Status | Notes |
| --- | --- | --- |
| Hook trajectory ingestion | ✅ | Current primary observation path |
| Deterministic pre-action gates | ✅ | Rule-based; safe/shadow-first posture |
| Crash-tolerant journal | ✅ | Locking, fsync, torn-tail recovery |
| Git snapshot / detached restore | ✅ | User HEAD/index remain untouched |
| Fork / continuation replay | ✅ | Same-prefix continuation machinery |
| Shadow reviewer | ✅ | Produces `CONTINUE / VERIFY / NUDGE`; no live delivery |
| Claim/evidence audit ledger | 🟡 | Works where observable outcomes exist |
| Labels / metrics | ✅ | Coverage-aware precision/FP evaluation |
| Counterfactual experiment harness | ✅ | Control/guidance same-prefix pairs |
| Standalone `spotterd` runtime | ❌ | Target in #66 |
| App Server primary observation | 🧪 | P0 PoC first |
| Event-driven signal engine | ❌ | Current reviewer trigger is periodic |
| Live `VERIFY / NUDGE` | ❌ | Target: `turn/steer` |
| `INTERRUPT` | ❌ | Target: `turn/interrupt` |
| `RESTART` | ❌ | Requires verified-state + side-effect-aware recovery |
| Homebrew / setup lifecycle | 🎯 | Target product path |

### Current blocker

The architecture depends on one concrete property:

> **When a user runs ordinary `codex`, can the TUI and Spotter attach to the same externally reachable App Server and can Spotter steer that exact active turn?**

P0 must prove:

1. same thread id is visible to TUI and Spotter;
2. Spotter receives live events for the same turn;
3. active `turn_id` tracking is reliable;
4. `turn/steer` affects the real user-visible TUI session;
5. multiple concurrent Codex sessions remain distinguishable;
6. reconnect/degraded behavior is observable and diagnosable.

If these properties fail, the daemon/App Server direction must be revisited before P1.

---

## Core idea

Coding agents work through long trajectories:

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

Many failures are not one bad output. They are **trajectory failures**.

A typical failure chain looks like this:

```text
Weak assumption
"the timeout must come from the Redis pool"
        │
        ▼
Search only Redis-related code
        │
        ▼
Edit Redis configuration
        │
        ▼
Tests fail for unrelated reasons
        │
        ▼
Add compensating changes
        │
        ▼
Scope grows; time/tokens are already spent
```

A post-hoc diff reviewer may eventually catch the mistake, but most of the cost has already been paid.

Spotter tries to intervene earlier:

```text
Weak assumption
      │
      ├─ insufficient evidence detected
      │       ↓
      │     VERIFY
      │       ↓
      │ inspect the stack trace / run focused probe
      │
      └─ avoid compounding the wrong branch
```

The goal is not to maximize the number of alarms. It is to reduce **wasted progress after the first meaningful deviation**.

---

## Intervention ladder

Spotter prefers the weakest intervention that is likely to help.

| Action | Meaning | Current state | Target runtime primitive |
| --- | --- | --- | --- |
| `CONTINUE` | No useful intervention | ✅ shadow reviewer | no-op |
| `VERIFY` | A consequential assumption needs evidence | ✅ decision only | async `turn/steer` |
| `NUDGE` | The trajectory is drifting or wasting effort | ✅ decision only | async `turn/steer` |
| `BLOCK` | A deterministic policy violation is about to execute | ✅ gate | synchronous `PreToolUse` deny |
| `INTERRUPT` | Continuing the active turn is likely to compound failure | ❌ | `turn/interrupt` |
| `RESTART` | The reasoning context itself is no longer trustworthy | ❌ | fresh continuation from verified state |

A semantic reviewer disagreement should **not** casually become a synchronous `BLOCK`. Stronger interventions require stronger evidence.

---

## Target architecture

### Four responsibilities

```text
Observation plane
  What is happening?
  → Codex App Server event stream

Control plane
  How can Spotter influence the active trajectory?
  → turn/steer, turn/interrupt

Enforcement plane
  What must be decided before execution?
  → PreToolUse deterministic gate

Supervision runtime
  Who owns state, signals, reviewers, budgets, recovery?
  → spotterd
```

### Target data flow

```text
                     ┌─────────────┐
                     │  Codex TUI  │
                     └──────┬──────┘
                            │
                            ▼
                 ┌──────────────────────┐
                 │ External App Server  │
                 └──────┬────────▲──────┘
                        │        │
                events  │        │ steer / interrupt
                        ▼        │
                 ┌──────────────────┐
                 │     spotterd     │
                 │                  │
                 │ Thread Manager   │
                 │ Live State       │
                 │ Trace IR         │
                 │ Audit State      │
                 │ Signal Engine    │
                 │ Reviewer         │
                 │ Intervention     │
                 │ Journal          │
                 └────────┬─────────┘
                          │
                 deterministic gate
                          │
                          ▼
                   PreToolUse Hook
```

State ownership becomes explicit:

```text
memory  = live supervision state for active/dormant threads
journal = durable event history + recovery/analysis source
snapshot = filesystem/Git state at a branch/recovery point
```

These are different resources and should not be conflated.

---

## Normal runtime behavior

For ordinary observations:

```text
App Server event
      │
      ▼
spotterd normalizes it
      │
      ├─ update live state
      ├─ append journal
      └─ evaluate cheap signals
                   │
              suspicious?
              ├─ no → done
              └─ yes
                    │
                    ▼
             async reviewer job
                    │
                    ├─ CONTINUE → no-op
                    ├─ VERIFY   → steer if target turn is still active
                    ├─ NUDGE    → steer if target turn is still active
                    └─ stronger action → separate high-confidence policy
```

Main continues while the reviewer thinks.

A reviewer verdict is tied to the turn that caused the review. If the verdict arrives after the target turn has ended, it must go through an explicit stale/defer/discard policy. Spotter must not blindly inject a late nudge into an unrelated later turn.

---

## Why keep one Hook?

The current prototype uses four Hook surfaces:

| Hook | Current role | Target role |
| --- | --- | --- |
| `SessionStart` | session/bootstrap observation | replace with App Server lifecycle if coverage is sufficient |
| `UserPromptSubmit` | goal capture | replace with App Server user-message events |
| `PreToolUse` | proposal observation + gate | **retain for atomic deterministic enforcement** |
| `PostToolUse` | result/snapshot/reviewer trigger | replace with App Server result/diff events |

The reason to retain `PreToolUse` is the execution guarantee:

```text
git reset --hard
        │
        ▼
PreToolUse
        │
        ▼
spotterd Gate Engine
   ├─ ALLOW
   └─ DENY  ← decided before execution
```

If a future App Server surface provides an equally reliable atomic veto for all relevant tools, a zero-hook integration becomes worth reconsidering.

---

## Use the current prototype

### Source installation

Python 3.11+:

```bash
git clone https://github.com/bogyie/spotter.git
cd spotter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Development extras:

```bash
python -m pip install -e '.[dev]'
```

Configuration:

```bash
cp spotter.example.toml spotter.toml
spotter --config spotter.toml
```

The example is passive by default:

```toml
observation_only = true

[main_agent]
adapter = "codex"

[reviewer]
model = "default"
```

### Current plugin compatibility path

Codex:

```bash
codex plugin marketplace add bogyie/spotter
codex plugin add spotter@spotter
```

Claude Code:

```bash
claude plugin marketplace add bogyie/spotter
claude plugin install spotter@spotter
```

Plugin installation is the **current prototype path**, not the long-term product boundary.

---

## Inspect and evaluate current sessions

Summarize a recorded session:

```bash
spotter analyze
```

Run the shadow reviewer:

```bash
spotter review --session <id>
```

Apply a human label:

```bash
spotter label \
  --session <id> \
  --step 7 \
  --verdict fp \
  --note "quoted text, not executed"
```

Metrics:

```bash
spotter metrics
```

Prepare a fork:

```bash
spotter fork --session <id> --step <k>
```

Run a counterfactual pair where success is mechanically decidable:

```bash
spotter experiment \
  --session <id> \
  --step <k> \
  --guidance "Verify the timeout source before editing Redis settings." \
  --check "pytest tests/test_timeout.py"
```

The experiment harness existing does **not** mean positive intervention advantage has been established. A ground-truth task set and enough executed runs are still major evidence gaps.

---

## Target installation and operations UX

The target first-run experience is intentionally small:

```bash
brew install spotter
spotter setup codex
spotter doctor
```

After setup:

```bash
codex
```

should be sufficient for ordinary use.

The user should not need to manually run:

```bash
spotter daemon start
codex app-server daemon start
```

Those remain operational/debug escape hatches.

Planned control-plane shape:

```text
spotter setup codex|claude|--all
spotter teardown codex|claude
spotter status
spotter doctor
spotter daemon start|stop|restart|status|logs
spotter sessions
spotter analyze
spotter review
spotter label
spotter metrics
spotter fork
spotter experiment
spotter prune
spotter config
spotter version
```

Detailed ownership, rollback, upgrades, uninstall, purge, and reinstall behavior are specified in [Lifecycle](docs/lifecycle.md).

---

## Implementation order

```text
P0  App Server lifecycle / attach PoC
 ↓
P1  spotterd + App Server client + IPC + thread/turn identity
 ↓
P2  package/setup/status/doctor/teardown lifecycle
 ↓
P3  move current journal/gate/audit/reviewer/snapshot capabilities behind spotterd
 ↓
P4  App Server primary observation + Hook minimization
 ↓
P5  cheap signals → event-driven reviewer
 ↓
P6  live VERIFY / NUDGE via turn/steer
 ↓
P7  INTERRUPT / RESTART / side-effect-aware recovery
 ↓
P8  upgrade/migration/retention/purge/multi-agent hardening
```

See [Roadmap](docs/roadmap.md) for deliverables, dependencies, and exit criteria.

---

## Documentation

- **[Docs index](docs/README.md)** — choose a document by question/reading path
- **[Status](docs/status.md)** — current implementation, blocker, and next steps
- **[Concept](docs/concept.md)** — problem definition, principles, and intervention semantics
- **[Architecture](docs/architecture.md)** — process, state, event, control, and failure contracts
- **[Lifecycle](docs/lifecycle.md)** — install → setup → run → recover → upgrade → teardown → purge
- **[Roadmap](docs/roadmap.md)** — dependency-driven implementation order and evaluation gates
- **[Research](docs/research.md)** — prior work, borrowed ideas, open hypotheses, and evidence gaps
- **[#66](https://github.com/Bogyie/spotter/issues/66)** — standalone-runtime umbrella issue

---

## Development

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

The migration should preserve a transport-independent boundary:

```text
agent-specific runtime events
        ↓
normalized Trace IR
        ↓
Spotter state / policy / evaluation
```

Codex App Server event shapes should not leak throughout Spotter core.

---

## Trajectory Engineering

Spotter is also an experiment in **Trajectory Engineering**:

- Prompt engineering — what the model is instructed to do
- Context engineering — what the model can see
- Harness engineering — what tools and constraints surround execution
- **Trajectory engineering — how an already-running path is observed, verified, steered, stopped, and recovered**

The working hypothesis is:

> **A better agent system is not only one that makes fewer mistakes. It is also one that detects mistakes early enough that they remain cheap to correct.**

That hypothesis is not yet proven for Spotter. Null and negative intervention results are first-class outcomes.

## License

MIT
