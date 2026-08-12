# Spotter Status

> **Purpose:** the fastest way to answer three questions: **What works today? What architecture are we moving to? What should be built next?**  
> Direction: [#66](https://github.com/Bogyie/spotter/issues/66) · Implementation order: [Roadmap](roadmap.md) · Runtime details: [Architecture](architecture.md)

---

## 30-second summary

Spotter is currently a **hook-based research prototype**. It already has real trajectory journals, deterministic gates, Git snapshots, fork/replay, a shadow reviewer, an audit ledger, labeling/metrics, and a counterfactual experiment harness.

The target product is different:

```text
CURRENT
Codex hooks
   ↓
a new Spotter process per hook
   ↓
journal / gate / snapshot / periodic shadow review

TARGET
Codex TUI
   ↓
External Codex App Server
   ↕ events / steer / interrupt
spotterd
   ↓
PreToolUse Hook only
(deterministic synchronous enforcement)
```

The **immediate blocker** is not writing `spotterd`. It is proving that ordinary `codex` and Spotter can share the **same externally reachable App Server**, so Spotter can observe the real thread and steer the real active turn. If that PoC fails, the target architecture must be revisited before the daemon migration continues.

### Read next

- Implementing runtime internals → [Architecture](architecture.md)
- Implementing install/setup/update/remove → [Lifecycle](lifecycle.md)
- Deciding what to build first → [Roadmap](roadmap.md)
- Evaluating whether the idea actually works → [Research](research.md)

---

## Quick status

Legend: ✅ implemented · 🟡 partial/shadow · 🧪 PoC required · 🎯 target · ❌ not implemented

| Area | Status | What exists now | Next concrete step |
| --- | --- | --- | --- |
| Hook ingestion | ✅ | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` are journaled | Replace broad observation with App Server events |
| Deterministic gate | ✅ | Shell-aware checks for destructive commands, path/dependency constraints, fail-open ambiguity handling | Move gate behind daemon IPC and re-measure latency |
| Journal | ✅ | Crash-tolerant JSONL, cross-process locking, fsync, torn-tail recovery | Make it durable history, not the hot state store |
| Snapshot | ✅ | Git-backed snapshots, deduplication, pruning, detached restore | Integrate into runtime resource lifecycle |
| Fork / replay | ✅ | Continue Codex from a shared prefix in a detached worktree | Measure replay fidelity/noise floor |
| Shadow reviewer | ✅ | Produces `CONTINUE`, `VERIFY`, `NUDGE`; verdicts are recorded only | Trigger from signals, then deliver live |
| Audit ledger | 🟡 | Claim/evidence state and stale propagation where outcomes are observable | Rebuild around richer App Server observations |
| Labels / metrics | ✅ | Coverage-aware labeling and precision/FP metrics | Add miss-rate, timing, cost, harm/recovery metrics |
| Counterfactual experiment harness | ✅ | Control/guidance same-prefix pairs can be prepared/run | Add a mechanically scored task set and execute real experiments |
| Standalone runtime | ❌ | No long-lived owner of live supervision state | Implement `spotterd`, IPC, service abstraction |
| App Server primary observation | 🧪 | Design only | Complete P0 lifecycle/attach PoC |
| Event-driven signal engine | ❌ | Reviewer is still cadence-based | Cheap signal → reviewer dispatch |
| Live `VERIFY` / `NUDGE` | ❌ | Reviewer decisions stop at the journal | Deliver via `turn/steer` |
| `INTERRUPT` | ❌ | No control path | Add `turn/interrupt` after evidence gate |
| `RESTART` | ❌ | Fork/replay is research machinery, not live recovery | Define verified-state + side-effect-aware recovery |
| Homebrew / setup lifecycle | 🎯 | Current paths are source/plugin installs | `brew install spotter`, `spotter setup codex`, `doctor`, `teardown` |

---

## The one thing to build first

### P0 — Codex App Server lifecycle / attach PoC

The question is intentionally narrow:

> **Can a user run ordinary `codex`, while the Codex TUI and Spotter use the same external App Server, and can Spotter steer that exact active turn?**

Test three paths:

1. **Codex-managed daemon** — start/ensure the Codex App Server daemon, then verify plain `codex` reuses it and Spotter can attach as a second client.
2. **Spotter-managed App Server** — Spotter starts/owns the external `codex app-server` process while preserving a normal `codex` UX.
3. **Embedded baseline** — document exactly which Spotter capabilities remain when Codex chooses an embedded App Server that Spotter cannot attach to.

PoC exit criteria:

- TUI and Spotter observe the same thread id.
- Spotter receives live events for the same active turn.
- Active `turn_id` tracking remains correct across multiple tool calls.
- `turn/steer` changes the actual user-visible Codex turn.
- Multiple concurrent Codex sessions are distinguishable.
- Reconnect behavior is understood.
- `status` / `doctor` can distinguish healthy, degraded, and disconnected states.

**Do not proceed to the daemon migration if these properties are not proven.**

---

## Current user path

The current prototype can still be used from source:

```bash
git clone https://github.com/Bogyie/spotter.git
cd spotter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Current Codex plugin compatibility path:

```bash
codex plugin marketplace add bogyie/spotter
codex plugin add spotter@spotter
```

Useful current commands:

```bash
spotter analyze
spotter review --session <id>
spotter label --session <id> --step <n> --verdict fp
spotter metrics
spotter fork --session <id> --step <n>
spotter experiment --session <id> --step <n> --guidance "..." --check "..."
```

---

## Target user path

After the standalone runtime migration, ordinary setup should look like this:

```bash
brew install spotter
spotter setup codex
spotter doctor

# normal use afterwards
codex
```

Operational/debug commands remain available, but should not be required for normal use:

```bash
spotter status
spotter sessions
spotter daemon status
spotter daemon restart
spotter doctor
spotter teardown codex
```

The user should **not** need to run either command before every Codex session:

```bash
spotter daemon start
codex app-server daemon start
```

---

## Implementation sequence

```text
P0  App Server lifecycle / attach PoC
 ↓
P1  spotterd + App Server client + IPC + thread/turn identity
 ↓
P2  install/setup/status/doctor/teardown/package lifecycle
 ↓
P3  move journal/gate/audit/reviewer/snapshot behind the runtime boundary
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
 ↓
P9  experience / adaptation
```

Each phase has explicit dependencies and exit criteria in [Roadmap](roadmap.md).

---

## Evidence status

Implementation progress and research evidence are separate.

| Question | Evidence today |
| --- | --- |
| Can Spotter collect real coding-agent trajectories? | Yes |
| Can deterministic gates catch concrete policy violations? | Yes, with measured false-positive/blind-spot work still ongoing |
| Can Spotter produce plausible semantic reviewer verdicts? | Yes, in shadow mode |
| Can Spotter branch a shared prefix for counterfactual experiments? | Yes |
| Has live Spotter guidance been shown to improve task outcomes? | **No** |
| Is intervention advantage positive on a ground-truth task set? | **Not measured yet** |
| Is the App Server target architecture operationally viable? | **PoC required** |

A mechanism being implemented does not prove it improves outcomes. Null and negative results are first-class outcomes for this project.

---

## Documentation map

| If you want to know... | Read |
| --- | --- |
| What Spotter is and why it exists | [Concept](concept.md) |
| What is implemented right now | **This document** |
| Exact process/data/control boundaries | [Architecture](architecture.md) |
| Install → setup → run → recover → upgrade → remove | [Lifecycle](lifecycle.md) |
| What should be built in what order | [Roadmap](roadmap.md) |
| Prior work, borrowed mechanisms, and open research questions | [Research](research.md) |
| The umbrella design decision | [#66](https://github.com/Bogyie/spotter/issues/66) |

---

## Status terminology

- ✅ **Implemented** — code exists and has at least tests or real-use evidence.
- 🟡 **Partial / shadow** — only part of the behavior exists, or it intentionally cannot affect Main yet.
- 🧪 **PoC required** — a design dependency that must be proven end-to-end before becoming an architectural assumption.
- 🎯 **Target** — agreed direction, not yet implemented.
- ❌ **Not implemented** — no production path exists yet.
