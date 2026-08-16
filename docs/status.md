<div align="center">

<h1>Spotter Status</h1>

<p><strong>What works today, what remains partial, and what is not live yet.</strong></p>

<p>
  <a href="../README.md">← README</a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#30-second-summary">30-second summary</a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#quick-capability-status">Capabilities</a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#evidence-status">Evidence</a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="roadmap.md">Roadmap</a>
</p>

</div>

> **Purpose:** the fastest way to answer three questions: **What works today? What blocks the project now? What comes next?**  
> Runtime details: [Architecture](architecture.md) · Sequence and evidence gates: [Roadmap](roadmap.md)

---

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>✅ Active today</strong><br />
      <sub>Hook ingestion, deterministic gates, journals, snapshots, replay, and diagnostics.</sub>
    </td>
    <td width="33%" valign="top">
      <strong>🟡 Partial or shadow</strong><br />
      <sub>App Server observation, live state, event-driven detection, semantic review, and opt-in live advisories.</sub>
    </td>
    <td width="33%" valign="top">
      <strong>❌ Not live yet</strong><br />
      <sub>Broadly enabled <code>VERIFY</code>/<code>NUDGE</code> and live recovery interventions.</sub>
    </td>
  </tr>
</table>

## 30-second summary

Spotter is currently a **Hook-based research prototype** with a standalone runtime foundation. It already has real trajectory journals, daemon-backed deterministic gates, Git snapshots, fork/replay, a shadow reviewer, an audit ledger, labeling/metrics, a counterfactual experiment harness, identity-rich App Server ingestion, and daemon-owned per-thread live state.

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

The shared App Server gate is resolved: an explicitly connected TUI and Spotter can observe and steer the same real turn. The Hook now uses bounded daemon IPC for deterministic enforcement, and `spotterd` owns reconnect/reconciliation for configured App Server endpoints. The #37 instrument now separates Hook, App Server source, Trace IR, and ThreadState coverage, but the available snapshot has no App Server or labeled failure samples, so the post-migration ceiling remains unmeasured. See [Observability ceiling baseline](observability-baseline.md).
The roadmap no longer uses `P0–P9` / `E0–E5`. It is organized by named outcomes:

```text
Runtime → Observe → Detect → Intervene → Recover → Harden
```

The `spotter metrics` headings `P1`, `P3`, and `P4` are retained legacy measurement names for
output compatibility; they are not roadmap stages or issue-triage codes.

The shared-server gate in [#78](https://github.com/spotter-agent/spotter/issues/78) passed for the
Spotter-managed external App Server path. Spotter now has a production WebSocket client and a
Thread/Turn/Runtime Attachment registry, durable normalized ingestion, and an incremental immutable
ThreadState reducer. The standalone daemon owns isolated state snapshots, a versioned local
control/gate handshake, and the App Server connection/recovery loop.

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
[#83](https://github.com/spotter-agent/spotter/issues/83) adds versioned integration manifests,
transactional Codex Hook/plugin migration, and managed `launchd`/`systemd --user` registration.
[#84](https://github.com/spotter-agent/spotter/issues/84) adds runtime-aware `status`/`doctor`,
manifest and Hook ownership checks, App Server probing when an endpoint exists, and explicit
degraded consequences when observation/control are unavailable but Hook enforcement remains.

[#31](https://github.com/spotter-agent/spotter/issues/31) adds daemon-owned immutable ThreadState,
deterministic Trace IR reduction, durable replay hydration, explicit coverage gaps, and conservative
control readiness after restart. [#87](https://github.com/spotter-agent/spotter/issues/87) connects
that state owner to a single-owner App Server loop with connection epochs, bounded backoff,
multi-thread reconciliation, durable observation gaps, and exact epoch/turn control fencing.

The assumption that merely starting the external server would make plain `codex` reuse it was
rejected by the [2026-08-13 validation](app-server-validation.md). The explicit `--remote` path
subsequently passed the same-thread/same-turn PoC. Multi-TUI concurrency, reconnect, and the
embedded-server degraded baseline remain unresolved productization boundaries. Setup records the
App Server strategy as pending; it does not invent an endpoint or claim ownership of a shared server.

In parallel, the highest-value evidence foundations remain:

- [#42](https://github.com/spotter-agent/spotter/issues/42) — replay/fork fidelity and noise floor;
- [#37](https://github.com/spotter-agent/spotter/issues/37) — observability ceiling baseline/post-migration measurement;
- [#38](https://github.com/spotter-agent/spotter/issues/38) and [#34](https://github.com/spotter-agent/spotter/issues/34) — use the completed #33 runtime cost/timing foundation to measure detection and intervention benefit or harm.

[#21](https://github.com/spotter-agent/spotter/issues/21) now has frozen dev/validation v2 sets,
preflight, resumable classified execution, and a [first real three-task development run](experiments/dev-v2-first-run.md).
All three pairs were tie-success; this qualifies the instrument but is not evidence of intervention
advantage.

---

## Quick capability status

Legend: ✅ implemented · 🟡 partial/shadow · 🧪 proof required · 🎯 target · ❌ not implemented

| Area | Status | What exists now | Next concrete step |
| --- | --- | --- | --- |
| Hook ingestion | ✅ | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` are journaled | Measure App Server parity in #37, then remove redundant Hooks in #86 |
| Deterministic gate | ✅ | Shell-aware daemon evaluation over bounded local IPC; unavailable/timeout uses the local Gate, while incompatible responses fail open; each Hook request and its IPC/correlated-block telemetry retain the exact resolved config generation; enforced and shadow blocks carry stable supervision IDs, rule versions, normalized effect/resource context, and an on-demand actionable explanation | Continue policy precision/miss-rate measurement |
| Journal | ✅ | Crash-tolerant JSONL stores identity-rich App Server Trace IR with locking, fsync, torn-tail recovery, and recovery gaps | Add bounded retention/checkpoint lifecycle in #89 |
| Snapshot | ✅ | App Server thread baselines and terminal local-mutation snapshots, Hook before-state snapshots, deduplication, pruning, detached restore | Remove redundant observation Hooks in #86; preserve lineage through runtime/retention migration (#89) |
| Reversibility / effects | ✅ | Hook proposals remain distinct from accepted work; App Server command/MCP/dynamic-tool starts create attempted effect observations that terminal results enrich through stable native correlation and evidence-backed outcomes; configured MCP semantics, append-only resolution history, explicit uncheckpointed Class B downgrades, and conservative unknown-adapter effects preserve uncertainty; metrics report exact/bounded coverage plus unknown buckets, reasons, and conservative-C counts | Keep new adapter gaps measurable; use the ledger as a recovery input in #26 |
| Fork / replay | ✅ | Continue Codex from shared prefixes with enforced provenance/parity and independently versioned, locked, crash-durable lineage manifests that read v1-v6 and refuse incompatible replacement; declared file/directory/environment-variable source→fork checks include explicit virtualenv/cache loss, copied absolute-worktree-path rejection, separately classified submodule drift, and proposal, active-signal-followup, plus labeled-intervention-opportunity coverage reporting; pinned-low evidence now includes an induced failure replay (6/6 failed) and three validation-v2 passing-prefix pairs (6/6 passed), all without disagreement; qualification v1 remains NO-GO for representative causal use | Capture naturally occurring final failures and qualify broader natural drift in #42 |
| Shadow reviewer | ✅ | Produces `CONTINUE`, `VERIFY`, `NUDGE`; periodic Hook reviews and explicitly opted-in signal jobs run asynchronously, with delivery controlled by a separate off-by-default opt-in; signal jobs pin an immutable config generation/model at executor submission and journal that generation through paid outcomes; the mutable spend ledger is independently versioned, reads legacy state, refuses unknown schemas, and fsyncs atomic replacements | Measure live advisory safety and benefit in #22/#23/#34 |
| Audit / live state | 🟡 | Daemon-owned typed ThreadState distinguishes constraints, hypotheses, observations, verified facts, summaries, interventions, and coverage; all eight initial incremental signal families cover failure streaks, equivalent calls, reads without frontier growth, exact-turn recurrence after deterministic gate blocks, bounded growth beyond explicit request scope, distinct edits without proven scope-matched validation, actions explicitly linked to stale hypotheses, and capacity/baseline-relative budget anomalies; signal-driven reviewer jobs merge same-state candidates, prioritize magnitude, bind immutable inputs, and journal their async lifecycle | Measure signal precision, misses, delay, and reviewer cost in #38/#24 using #33 telemetry |
| Evaluation labels / metrics | 🟡 | Independently versioned coverage-aware labels, signal-sampling records, and intervention opportunity annotations with legacy reads plus refusal-before-append for future/foreign sampling history; durable rater identity, independent measurement scopes, double-label agreement, active-signal precision by type, deterministic event-kind-stratified signal-silence frames with explicit inclusion/exclusion bias, miss-rate reporting for sampled silence, and stable-event-pinned semantic/observable intervention opportunity windows whose required evidence links through signal timing, post-window action/failure/file counts, reviewer queue/inference/decision timing, control dispatch/RPC resolution, and exact-identity observed steer adoption, together with globally deduplicated Main actions and per-surface raw observations, field-level Main-token and reviewer session/call token coverage, #28-derived repeated-action counts, same-clock signal→queue→review lifecycle latency, reviewer-decision and post-dispatch observed-adoption lead/lag against the target turn boundary, asynchronously persisted control dispatch/acceptance/adoption/stale-delivery coverage, terminal-job coverage, connection-epoch ordering, gate-latency, runtime-resource, storage, and paired objective-outcome projections with per-arm cost coverage; `spotter analyze` joins session interventions/costs to task or replay outcomes only through durable arm/fork provenance, and unavailable/cross-clock fields remain unknown | Label representative #24 opportunities and compare trigger modes; seed detector frames and double-label evidence in #38 |
| Counterfactual harness | ✅ | Same-prefix experiments preflight both forks and support explicit neutral-noise or guidance modes; their independently versioned result journal reads legacy history and refuses incompatible or corrupt history before fsynced appends. Frozen task, task-set, and resumable batch schemas are independently identified; batch resume validates before preflight, uses locked fsynced appends and atomic torn-tail repair, and retains optional replay-source session provenance | Execute and report replay noise (#42) before interpreting intervention effects |
| Standalone runtime | 🟡 | Long-lived process, IPC, lifecycle, isolated per-thread state, and App Server recovery ownership | Select and verify the shared endpoint during setup |
| Runtime identity | ✅ | Logical threads/turns remain separate from per-connection attachment IDs and monotonically recovered epochs; signal-driven reviewer jobs retain their exact turn/epoch target | Enforce the same target identity at future reviewer delivery |
| App Server primary observation | 🟡 | Configured endpoints route through `spotterd` into independently versioned durable Trace IR with schema-validated, rebuildable journal sidecars and incremental ThreadState; a bounded value-free, independently versioned source audit and conformance corpus measure projection coverage, including Spotter advisory inputs as intervention delivery rather than dropped user-goal evidence; unknown audit schemas degrade visibly without interrupting primary journals | Collect labeled App Server failures to finish #37, then reduce Hooks in #86 |
| Managed Codex lifecycle | 🟡 | Transactional setup/teardown, an independently versioned ownership manifest with legacy migration and crash-durable atomic host-state writes, managed service, diagnostics, configured-endpoint recovery, and temporary observation Hooks until #86 parity | Add verified endpoint selection without claiming shared-process ownership |
| Runtime reconnect/recovery | ✅ | Explicit connect/reconcile/backoff states, restart hydration, durable gaps, capability/server fingerprints, and stale-control fencing | Add retention/checkpoints in #89 |
| Event-driven detection | 🟡 | All eight initial families journal identity/evidence-rich active, cooled-down, resolved, and stale candidates for explicit normalized failures, equivalent calls, frontierless reads, deterministic-block recurrence, request-scope growth, scope-matched unvalidated edits, causal stale-hypothesis reuse, and relative token/duration pressure; opted-in active candidates merge into priority-ordered, budget-capped asynchronous reviews with bounded immutable inputs, while stale targets never deliver; queue/decision provenance and metrics now stratify signal, periodic, and manual launches | Compare event-driven, periodic-only, and mixed fallback-cadence precision/cost in #38/#24 using #33 telemetry |
| Live `VERIFY` / `NUDGE` | 🟡 | Separate off-by-default opt-in delivers fresh signal-driven decisions once through exact attachment/turn/epoch `turn/steer`; every advisory carries a short stable Spotter ID in its visible marker and durable lifecycle, `spotter interventions` lists recent BLOCK/VERIFY/NUDGE states, `spotter explain --supervision-id ...` separates policy facts or model judgment from evidence, target, remedy, and delivery outcome, and `spotter feedback` appends independently versioned, structured redacted human evaluation without rewriting history or ground truth; correlated inputs become observed-in-turn outcomes, while an unobserved accepted steer closes as durable `rpc_accepted_only`; advisory inputs remain supervision rather than replacing the user goal | Test enqueue/consume activity races and history contamination in #22/#23/#45 before broader enablement |
| `INTERRUPT` / `RESTART` | ❌ | No live recovery path | #26 + #30 after soft intervention is understood |
| Packaging / long-term operations | ✅ | Exact version tags publish verified release artifacts; the dedicated Homebrew tap installs immutable releases and opens repeatable Formula-update PRs; the macOS lifecycle gate proves clean install, live G1→G2 upgrade/reconcile, teardown-less fail-open uninstall, retained data, and reinstall/teardown without Cellar assumptions; every implemented durable Spotter family has an independent schema identity, bounded legacy behavior, and explicit non-destructive future/corrupt handling; snapshot refs, detached restore worktrees, daemon logs, and reviewer logs carry exact versioned ownership records, while purge revalidates repository ownership and preserves journal/recovery/fork/experiment-result/worktree/manual-pin reachability; focused data/integration/snapshot/log scopes remain independently usable, and destructive `purge --all` composes them in lifecycle order; configuration now has one validated layered resolver, source-aware generations, exhaustive per-key activation boundaries, an atomic active/pending snapshot store, explicit and file-triggered daemon reload, quiescent cross-thread next-turn activation, submission-pinned signal reviewer generations, generation-attributed Hook gate requests, and generation-pinned daemon Trace/signal provenance; invalid/racing reloads preserve prior valid active and pending state; CLI↔daemon handshakes publish protocol ranges, named capabilities, exact build/runtime generations, classify compatible stale/legacy-unknown peers, and reject incompatible control with actionable restart diagnostics; `spotter update` detects Homebrew, pipx, uv tool, pip, editable, and source ownership and prints advisory-only version/upgrade/reconciliation commands without modifying package files; setup stages candidate runtime inputs before restarting a changed integration generation, verifies the reconciled runtime, and restores both the prior manifest and runtime inputs on failure | Continue #90 with effective runtime-construction fingerprint diagnostics |

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
| Is there a reproducible mechanically scored task corpus? | **Yes for the synthetic v2 foundation — six frozen tasks and a reportable 6/6-arm dev run; external/ecosystem breadth remains future work (#21)** |
| Has live Spotter guidance been shown to improve outcomes? | **No** |
| Is the App Server control boundary operationally viable? | **Yes for a configured Spotter-managed external path, including daemon reconnect/reconciliation** |
| Is the post-App-Server visible-in-time ceiling measured? | **No — the instrument exists, but the current sample has 0 App Server sessions and 0/9 labeled sessions (#37)** |

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
| Build immutable package artifacts from a version tag | [Releasing](releasing.md) |
| What should be built in what order | [Roadmap](roadmap.md) |
| Prior work, hypotheses, and evidence | [Research](research.md) |
| Repository/issue conventions | [Conventions](conventions.md) |
