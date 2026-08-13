# Spotter Status

> **Purpose:** the fastest way to answer three questions: **What works today? What blocks the project now? What comes next?**  
> Runtime details: [Architecture](architecture.md) · Sequence and evidence gates: [Roadmap](roadmap.md)

---

## 30-second summary

Spotter is currently a **Hook-based research prototype** with a standalone runtime foundation. It already has real trajectory journals, daemon-backed deterministic gates, Git snapshots, fork/replay, a shadow reviewer, an audit ledger, labeling/metrics, a counterfactual experiment harness, and a manually managed `spotterd` process with versioned local IPC.

The target product is a standalone runtime:

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

The shared App Server gate is resolved: an explicitly connected TUI and Spotter can observe and steer the same real turn. The Hook now uses bounded daemon IPC for deterministic enforcement. The immediate work is wiring the remaining process foundations into a managed lifecycle: App Server event routing, setup registration, and reconnect behavior remain separate issues. See [App Server connection validation](app-server-validation.md).
The roadmap no longer uses `P0–P9` / `E0–E5`. It is organized by named outcomes:

```text
Runtime → Observe → Detect → Intervene → Recover → Harden
```

The shared-server gate in [#78](https://github.com/spotter-agent/spotter/issues/78) passed for the
Spotter-managed external App Server path. Spotter now has a production WebSocket client and a
provisional Thread/Turn/Runtime Attachment registry with no production event consumer yet. It also
has a standalone daemon process and versioned local control/gate handshake; managed startup and
runtime consumers remain next.

---

## Current focus

### Runtime

[#78](https://github.com/spotter-agent/spotter/issues/78) demonstrated same-thread observation and
same-turn steering through a Spotter-managed external App Server. [#80](https://github.com/spotter-agent/spotter/issues/80)
turns that PoC into an async client with initialize/disconnect, raw event delivery, thread queries,
typed steer/interrupt methods, and explicit supported/unknown/unavailable capability state.
[#79](https://github.com/spotter-agent/spotter/issues/79) adds the `spotterd` process, versioned
local control handshake, explicit health states, manual lifecycle commands, and a testable
`ServiceManager` boundary. It deliberately does not own or stop a shared Codex App Server.
[#82](https://github.com/spotter-agent/spotter/issues/82) adds bounded deterministic gate requests,
local enforcement fallback on unavailable/timeout, and separate Hook/IPC timing telemetry.

The remaining implementation boundaries are split into native GitHub issues:

- [#83](https://github.com/spotter-agent/spotter/issues/83) — transactional Codex setup/teardown and integration ownership;
- [#84](https://github.com/spotter-agent/spotter/issues/84) — runtime-aware status/doctor;
- [#87](https://github.com/spotter-agent/spotter/issues/87) — daemon/App Server reconnect and identity reconciliation.

[#31](https://github.com/spotter-agent/spotter/issues/31) then moves independent supervision state into that runtime, fed by the App Server ingestion path in [#85](https://github.com/spotter-agent/spotter/issues/85).

> **Can Spotter provide an acceptable explicit launch path in which the Codex TUI and Spotter use the same external App Server, and can Spotter steer that exact active turn?**

The former assumption that merely starting the external server would make plain `codex`
reuse it was rejected by the [2026-08-13 validation](app-server-validation.md).
In parallel, the highest-value evidence foundations remain:

- [#21](https://github.com/spotter-agent/spotter/issues/21) — mechanically scored task set;
- [#42](https://github.com/spotter-agent/spotter/issues/42) — replay/fork fidelity and noise floor;
- [#37](https://github.com/spotter-agent/spotter/issues/37) — observability ceiling baseline/post-migration measurement;
- [#33](https://github.com/spotter-agent/spotter/issues/33) — runtime cost/timing/outcome telemetry.

1. **Explicit remote TUI** — start an external App Server, launch `codex --remote <endpoint>`, and verify Spotter can attach as a second client.
2. **Spotter launch UX** — validate a launcher/alias/wrapper that supplies the endpoint without claiming unsupported automatic discovery.
3. **Embedded baseline** — document exactly which Spotter capabilities remain when Codex chooses an embedded App Server that Spotter cannot attach to.
---

## Quick capability status

Legend: ✅ implemented · 🟡 partial/shadow · 🧪 proof required · 🎯 target · ❌ not implemented

| Area | Status | What exists now | Next concrete step |
| --- | --- | --- | --- |
| Hook ingestion | ✅ | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` are journaled | App Server ingestion #85, then remove redundant Hooks in #86 |
| Deterministic gate | ✅ | Shell-aware daemon evaluation over bounded local IPC; unavailable/timeout uses the local Gate, while incompatible responses fail open | Continue policy precision/miss-rate measurement |
| Journal | ✅ | Crash-tolerant JSONL, locking, fsync, torn-tail recovery | Feed it from identity-rich App Server Trace IR in #85 |
| Snapshot | ✅ | Git-backed snapshots, deduplication, pruning, detached restore | Preserve lineage through runtime/retention migration (#89) |
| Fork / replay | ✅ | Continue Codex from a shared prefix in a detached worktree | Measure fidelity/noise floor in #42 |
| Shadow reviewer | ✅ | Produces `CONTINUE`, `VERIFY`, `NUDGE`; verdicts are recorded only | Move to event-driven candidates, then live delivery |
| Audit ledger | 🟡 | Claim/evidence state and stale propagation where outcomes are observable | Move independent state into `spotterd` (#31) |
| Evaluation labels / metrics | ✅ | Coverage-aware labeling and precision/FP metrics | Broader cost/miss/harm metrics (#33, #38, #34) |
| Counterfactual harness | ✅ | Control/guidance same-prefix pairs can be prepared/run | Add mechanically scored tasks (#21) |
| Standalone runtime | 🟡 | Long-lived process, versioned control/gate IPC, health states, manual lifecycle, service abstraction | Register managed startup in #83; add runtime consumers in #31/#85 |
| Runtime identity | 🟡 | Provisional lifecycle registry and explicit legacy unknowns; no production consumer | Validate and refine it while routing normalized App Server events in #85 |
| App Server primary observation | 🟡 | Async client, raw events, thread queries, controls, capability degradation | Normalize, identity-route, and journal events in #85 |
| Managed Codex lifecycle | ❌ | Current path is source/plugin installation | transactional setup/teardown in #83; diagnostics in #84 |
| Runtime reconnect/recovery | ❌ | No long-lived App Server connection to recover | #87 after runtime/state boundaries exist |
| Event-driven detection | ❌ | Reviewer is still cadence-based | #28 after Runtime/Observe |
| Live `VERIFY` / `NUDGE` | ❌ | Reviewer decisions stop at the journal | #22 via `turn/steer` |
| `INTERRUPT` / `RESTART` | ❌ | No live recovery path | #26 + #30 after soft intervention is understood |
| Packaging / long-term operations | 🎯 | Current paths are source/plugin installs; local `prune` exists | #88 packaging, #89 purge/retention, #90 upgrade/config lifecycle |

---

## Roadmap at a glance

### Runtime

Prove and establish the standalone App Server/`spotterd` boundary.

### Observe

Use App Server events as the primary trajectory source and maintain live supervision state. Measure what is actually observable early enough to act on.

### Detect

Trigger semantic review from cheap candidate signals. Measure precision, miss rate, and detection delay together.

### Intervene

Deliver `VERIFY` / `NUDGE` to the correct active turn. Measure benefit, harm, recovery, ignored guidance, and wrong-nudge susceptibility.

### Recover

Add `INTERRUPT` / `RESTART` only with stronger evidence and explicit side-effect/reversibility handling.

### Harden

Make setup, upgrades, schemas, retention, cleanup, recovery, and long-term operation predictable.

See [Roadmap](roadmap.md) for detailed exit criteria and linked issues.

---

## Evidence status

Implementation progress and research evidence remain separate.

| Question | Evidence today |
| --- | --- |
| Can Spotter collect real coding-agent trajectories? | Yes |
| Can deterministic gates catch concrete policy violations? | Yes, with precision/miss-rate work still ongoing |
| Can Spotter produce plausible semantic reviewer verdicts? | Yes, in shadow mode |
| Can Spotter branch a shared prefix for counterfactual experiments? | Yes |
| Is the fork instrument's causal noise floor known? | **No — #42** |
| Is there a reproducible mechanically scored task corpus? | **No — #21** |
| Has live Spotter guidance been shown to improve outcomes? | **No** |
| Is the App Server control boundary operationally viable? | **Yes for the Spotter-managed external path; daemon lifecycle and reconnect remain** |

A mechanism being implemented does not prove it improves outcomes. Null and negative results are first-class outcomes for this project.

---

## Issue triage

Repository issues use GitHub native metadata rather than label namespaces:

- **Type** — work nature (`Task`, `Bug`, `Feature`, `Architecture`, `Experiment`);
- **Priority** — current sequencing pressure (`Urgent`, `High`, `Medium`, `Low`);
- **Effort** — change surface / validation burden (`XS`–`XL`);
- **Area** — primary product/problem domain;
- **Milestone** — roadmap stage owning completion;
- **Dependencies** — actual `blocked by` / `blocking` relationships.

The Milestones are the named roadmap outcomes:

```text
Runtime → Observe → Detect → Intervene → Recover → Harden
```

Labels are exceptional contributor signals only (`good first issue`, `help wanted` today). Detailed semantics live in [Repository Conventions](conventions.md#13-issue-metadata-and-triage).

---

## Documentation map

| If you want to know... | Read |
| --- | --- |
| What Spotter is and why it exists | [Concept](concept.md) |
| What is implemented right now | **This document** |
| Exact process/data/control boundaries | [Architecture](architecture.md) |
| Install → setup → run → recover → upgrade → remove | [Lifecycle](lifecycle.md) |
| What should be built in what order | [Roadmap](roadmap.md) |
| Prior work, hypotheses, and evidence | [Research](research.md) |
| Repository/issue conventions | [Conventions](conventions.md) |
