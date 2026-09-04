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

The shared App Server gate is resolved: an explicitly connected TUI and Spotter can observe and steer the same real turn. The Hook now uses bounded daemon IPC for deterministic enforcement, and `spotterd` owns reconnect/reconciliation and explicit thread subscriptions for configured App Server endpoints. The [#303 ceiling run](experiments/app-server-observability-v1-result.md) stopped after two attempts exposed no later-user-input notification: command outcomes were exact and timely after the subscription fix, but all three observation Hooks remain **NO-GO** for removal. See [Observability ceiling baseline](observability-baseline.md) for the instrument's earlier zero-sample state.
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
subsequently passed the same-thread/same-turn PoC. `spotter codex` is now the supported managed
launch: it reuses a reachable configured endpoint or starts a detached App Server when a TCP
endpoint is unreachable, waits for `spotterd` observation, and selects that endpoint explicitly.
Plain `codex` remains visibly degraded. Two real concurrent TUIs remained isolated, and a daemon
kill during an active command recovered the same thread/turn across a new connection epoch while
recording the gap and preserving a strict-readable journal
([result](experiments/app-server-live-observation-v1-result.md#update--managed-launch-and-in-flight-daemon-recovery)).
Setup still verifies the server identity and thread-query/observation surface before committing an
explicit `ws://` or `wss://` endpoint. Spotter may start the configured server for a later launch,
but never stops it or claims exclusive ownership.

In parallel, the highest-value evidence foundations remain:

- [#42](https://github.com/spotter-agent/spotter/issues/42) — replay/fork fidelity and noise floor;
- [#37](https://github.com/spotter-agent/spotter/issues/37) — implemented the observability
  instrument and zero-sample baseline; the [#303](https://github.com/spotter-agent/spotter/issues/303)
  run found a structural user-input gap and retained every observation Hook;
- [#38](https://github.com/spotter-agent/spotter/issues/38) and [#34](https://github.com/spotter-agent/spotter/issues/34) — use the completed #33 runtime cost/timing foundation to measure detection and intervention benefit or harm.

[#21](https://github.com/spotter-agent/spotter/issues/21) now has frozen dev/validation v2 sets,
preflight, resumable classified execution, and a [first real three-task development run](experiments/dev-v2-first-run.md).
All three pairs were tie-success; this qualifies the instrument but is not evidence of intervention
advantage.

Detector-quality reports preserve each declared multi-event-kind silence batch as one sampling
stratum, so its rate and eligibility/exclusion denominator describe the same frame. Gate
`fp / (tp + fp)` is named false discovery (`1 - precision`); true false-positive rate remains
unavailable until #38 has a per-rule true-negative sampling frame.

---

## Quick capability status

Legend: ✅ implemented · 🟡 partial/shadow · 🧪 proof required · 🎯 target · ❌ not implemented

| Area | Status | What exists now | Next concrete step |
| --- | --- | --- | --- |
| Hook ingestion | ✅ | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` are journaled; #303 found no removable observation Hook | Retain all Hooks; revisit only after a newer source surface closes the measured gaps |
| Deterministic gate | ✅ | Shell-aware daemon evaluation over bounded local IPC; unavailable/timeout uses the local Gate, while incompatible responses fail open; each Hook request and its IPC/correlated-block telemetry retain the exact resolved config generation; enforced and shadow blocks carry stable supervision IDs, rule versions, normalized effect/resource context, and an on-demand actionable explanation | Continue policy precision/miss-rate measurement |
| Journal | ✅ | Crash-tolerant JSONL stores identity-rich App Server Trace IR with locking, fsync, torn-tail recovery, recovery gaps, and the bounded retention lifecycle implemented in #89 | Keep retention and recovery behavior in the release regression gate |
| Snapshot | ✅ | App Server thread baselines and terminal local-mutation snapshots, Hook before-state snapshots, deduplication, pruning, detached restore | Remove only observation Hooks whose snapshot responsibilities pass #303/#86 parity |
| Reversibility / effects | ✅ | Hook proposals remain distinct from accepted work; App Server command/MCP/dynamic-tool starts create attempted effect observations that terminal results enrich through stable native correlation and evidence-backed outcomes; configured MCP semantics, append-only resolution history, explicit uncheckpointed Class B downgrades, and conservative unknown-adapter effects preserve uncertainty; metrics report exact/bounded coverage plus unknown buckets, reasons, and conservative-C counts | Keep new adapter gaps measurable; use the ledger as a recovery input in #26 |
| Fork / replay | ✅ | Continue Codex from shared prefixes with enforced provenance/parity; pair admission rejects prefixes carrying observation gaps or external effects before either arm starts; independently versioned, locked, crash-durable lineage manifests read v1-v6 and refuse incompatible replacement; declared file/directory/environment-variable source→fork checks include explicit virtualenv/cache loss, copied absolute-worktree-path rejection, separately classified submodule drift, and proposal, active-signal-followup, plus labeled-intervention-opportunity coverage reporting; pinned-low evidence includes an induced failure replay (6/6 failed), three validation-v2 passing-prefix pairs (6/6 passed), and three predeclared capture cohorts whose ten controls all passed; the two post-#307 cohorts captured replay sources for 14/14 arms; task v2 can materialize and verify immutable Git commit/tree sources while separating source infrastructure failures; a four-repository SWE-bench Verified cohort and one-run stop rule are frozen, and its first preflight stopped at pre-run gate 4 with 2/4 tasks `UNJUDGEABLE`, and a corrected source-materialisation fetch moved pytest-10356 to `READY` while requests-2931 still blocks execution at 3/4 because its pinned image lacks `pytest-httpbin`, a frozen-frame defect that reproduces without Spotter, so no paid arm has run ([result](experiments/fork-natural-failure-external-v1-result.md)); the [v2 frame](experiments/fork-natural-failure-external-v2-protocol.md) corrects only that task, narrowing its scorer to the pinned row's 85 graded node IDs after uniform node-ID scoring was rejected as unreconstructable from the truncated dataset row; its [paid run](experiments/fork-natural-failure-external-v2-result.md) completed all eight arms with zero infrastructure failure and 8/8 replay sources captured, but every control arm passed, so the stop rule published a predeclared null and no neutral pair ran; the [v3 hard-stratum run](experiments/fork-natural-failure-external-v3-result.md) completed all six arms with 6/6 sources captured and sampled one natural xarray control failure; that source had one exact pre-mutation branch point, but all six prescribed neutral continuations lost Git worktree metadata before scorer setup, leaving 0/3 pairs judgeable and exposing [#351](https://github.com/spotter-agent/spotter/issues/351); the harness honors isolated `CODEX_HOME`, holds each fork under a native Git worktree lock through scoring, and records a missing pre-scorer worktree as infrastructure; [v4](experiments/fork-natural-failure-external-v4-result.md) proved that Codex teardown can raw-delete locked metadata, and [v5](experiments/fork-natural-failure-external-v5-result.md) proved the repaired host path still exposed an invalid host-absolute `commondir` inside Docker, so both remained 0/3 judgeable and #351 is reopened; qualification remains NO-GO | Replace scorer metadata with a container-visible local Git clone in #351 before another successor |
| Shadow reviewer | ✅ | Produces `CONTINUE`, `VERIFY`, `NUDGE`; periodic Hook reviews and explicitly opted-in signal jobs run asynchronously, with delivery controlled by a separate off-by-default opt-in; signal jobs pin an immutable config generation/model at executor submission and journal that generation through paid outcomes; the mutable spend ledger is independently versioned, reads legacy state, refuses unknown schemas, and fsyncs atomic replacements | Measure live advisory safety and benefit in #23/#34 using the delivery implemented in #22 |
| Audit / live state | 🟡 | Daemon-owned typed ThreadState distinguishes constraints, hypotheses, observations, verified facts, summaries, interventions, and coverage; all eight initial incremental signal families cover failure streaks, equivalent calls, reads without frontier growth, exact-turn recurrence after deterministic gate blocks, bounded growth beyond explicit request scope, distinct edits without proven scope-matched validation, actions explicitly linked to stale hypotheses, and capacity/baseline-relative budget anomalies; signal-driven reviewer jobs merge same-state candidates, prioritize magnitude, bind immutable inputs, and journal their async lifecycle | Measure signal precision, misses, delay, and reviewer cost in #38/#24 using #33 telemetry |
| Evaluation labels / metrics | 🟡 | Independently versioned coverage-aware labels, signal-sampling records, and intervention opportunity annotations with legacy reads plus refusal-before-append for future/foreign sampling history; durable rater identity, independent measurement scopes, double-label agreement, active-signal precision by type, deterministic event-kind-stratified signal-silence frames with explicit inclusion/exclusion bias, miss-rate reporting for sampled silence, and stable-event-pinned semantic/observable intervention opportunity windows whose required evidence links through signal timing, post-window action/failure/file counts, reviewer queue/inference/decision timing, control dispatch/RPC resolution, and exact-identity observed steer adoption, together with globally deduplicated Main actions and per-surface raw observations, field-level Main-token and reviewer session/call token coverage, #28-derived repeated-action counts, same-clock signal→queue→review lifecycle latency, reviewer-decision and post-dispatch observed-adoption lead/lag against the target turn boundary, asynchronously persisted control dispatch/acceptance/adoption/stale-delivery coverage, terminal-job coverage, connection-epoch ordering, gate-latency, runtime-resource, storage, and paired objective-outcome projections with per-arm cost coverage plus neutral replay preflight- and infrastructure-failure categories/rates; wrong-nudge reports also join exact-source persistence follow-ups and independently labeled stale-repromotion/new-goal outcomes with explicit conflict, stale-label, and missing-label coverage; `spotter analyze` joins session interventions/costs to task or replay outcomes only through durable arm/fork provenance, and unavailable/cross-clock fields remain unknown | Label representative #24 opportunities and compare trigger modes; seed detector frames and double-label evidence in #38 |
| Counterfactual harness | ✅ | Same-prefix experiments preflight both forks and support explicit neutral-noise or guidance modes; executed arms keep their restored Git worktree locked through the scorer and reject a missing pre-scorer repository as infrastructure; their independently versioned result journal reads legacy history and refuses incompatible or corrupt history before fsynced appends, and durable aggregate reporting preserves neutral outcome disagreement, exact preflight-failure categories, and infrastructure-failure classifications. Frozen task, task-set, and resumable batch schemas are independently identified; batch resume validates before preflight, uses locked fsynced appends and atomic torn-tail repair, and retains optional replay-source session provenance; capture-requested batches prove the exact owned Hook, selected homes, config, and reversible journal round-trip before paid execution, then pin that readiness receipt across incomplete resume | Rerun the retained #42 natural-failure source under the corrected lifecycle before interpreting intervention effects |
| Standalone runtime | ✅ | Long-lived process, IPC, lifecycle, isolated per-thread state, explicit endpoint setup, one-command managed launch, visible plain-`codex` degradation, and live-validated daemon recovery | Keep the managed-launch/recovery matrix in the release regression gate |
| Runtime identity | ✅ | Logical threads/turns remain separate from per-connection attachment IDs and monotonically recovered epochs; signal-driven reviewer jobs retain their exact turn/epoch target | Enforce the same target identity at future reviewer delivery |
| App Server primary observation | 🟡 | Configured endpoints subscribe through `thread/resume` and route into independently versioned durable Trace IR with schema-validated, rebuildable journal sidecars and incremental ThreadState; reconciliation now attaches only to loaded threads, and one that cannot be subscribed degrades to a read and is recorded as such instead of dropping the connection; #303 proved exact timely successful-command outcomes but found no later-user-input source notification; the store now holds real App Server sessions for the first time — 54 of them, with two concurrent threads journaled in isolation and the unclassified event stream dominated by two lifecycle notifications ([result](experiments/app-server-live-observation-v1-result.md)) | Establish denominators for the gap and unknown-event counts, then quantify the remaining #86 source gaps |
| Managed Codex lifecycle | ✅ | Transactional setup/teardown, verified explicit external endpoints, `spotter codex` managed remote launch with start-on-unreachable detached App Server, an independently versioned ownership manifest with rollback, diagnostics, recovery, and temporary observation Hooks until #86 parity; shared App Server ownership remains external; endpoint setup, two-TUI isolation, automatic launch, and in-flight daemon restart are live-validated ([result](experiments/app-server-live-observation-v1-result.md#update--managed-launch-and-in-flight-daemon-recovery)) | Keep the lifecycle contract covered while #86 evaluates Hook removal |
| Runtime reconnect/recovery | ✅ | Explicit connect/reconcile/backoff states, restart hydration, durable gaps, capability/server fingerprints, stale-control fencing, and the retention/checkpoint lifecycle implemented in #89; a live daemon kill during an active command recovered healthy in about 4s and preserved the same thread/turn through completion ([result](experiments/app-server-live-observation-v1-result.md#update--managed-launch-and-in-flight-daemon-recovery)) | Keep reconnect, hydration, and retention behavior in the release regression gate |
| Event-driven detection | 🟡 | All eight initial families journal identity/evidence-rich active, cooled-down, resolved, and stale candidates for explicit normalized failures, equivalent calls, frontierless reads, deterministic-block recurrence, request-scope growth, scope-matched unvalidated edits, causal stale-hypothesis reuse, and relative token/duration pressure; opted-in active candidates merge into priority-ordered, budget-capped asynchronous reviews with bounded immutable inputs, while stale targets never deliver; queue/decision provenance and metrics now stratify signal, periodic, and manual launches | First live firing recorded: `failure_streak` raised with escalating severity and linked evidence, three further candidates correctly suppressed, and `signal_delay` measured at avg 1340ms / max 4206ms where it had been `unknown (0/0)`; every resulting reviewer job was discarded `target_not_active` because the turn ended first ([result](experiments/app-server-live-observation-v1-result.md)) — compare event-driven, periodic-only, and mixed fallback-cadence precision/cost in #38/#24 |
| Live `VERIFY` / `NUDGE` | 🟡 | Separate off-by-default opt-in delivers fresh signal-driven decisions once through exact attachment/turn/epoch `turn/steer`; every advisory carries a short stable Spotter ID in its visible marker and durable lifecycle, `spotter interventions` lists recent BLOCK/VERIFY/NUDGE/INTERRUPT states, `spotter explain --supervision-id ...` separates policy facts or model judgment from evidence, target, remedy, and delivery outcome, and `spotter feedback` appends independently versioned, structured redacted human evaluation without rewriting history or ground truth; correlated inputs become observed-in-turn outcomes, while an unobserved accepted steer closes as durable `rpc_accepted_only`, including when the target terminal event is consumed before the steer ACK; same-turn live review deliveries remain serial, deterministically ordered, and deduplicated by durable job identity; expired advisory inputs resurfacing in later turns are diagnosed as outside-target supervision and cannot replace the later user goal; a hashed v1 wrong-nudge corpus freezes seven falsifiable failure families, prepares independent equivalent-prefix forks, starts identical continuations through App Server, and uses real `turn/steer` only for raw/advisory/VERIFY-first arms while keeping rejection, unknown acceptance, and observed completion distinct; completed arms reuse frozen task checks and fsync per-arm result rows with delivery and mechanical classifications kept separate, while arms without observed completion stay unjudgeable; secondary annotations independently journal exact-result fingerprints, rater identity, task ownership, conservative behavior relations, and non-exclusive susceptibility classes, rejecting unsupported evidence-refutation, mechanical-compliance, or delivery claims; offline reports revalidate pair provenance and expose framing-stratified delivery, completion, judgeability, harm, label/conflict/stale coverage, refutation, compliance, replacement, constraint-loss, and persistence counts/rates; `spotter wrong-nudge run` validates frozen corpus/scorer inputs before an explicit paid four-arm execution, `persist` starts a versioned next-boundary follow-up only for a complete accepted arm set and fsyncs exact-source-linked outcomes independently, exact-follow-up annotations distinguish harmless history, stale re-promotion, new-goal contamination, and operationally unjudgeable cases without rewriting execution evidence, and `report` joins those persistence results and annotations with explicit provenance and coverage without rerunning arms; a live `turn/steer` against a real App Server was accepted and observably adopted — it arrived as a user message inside the target turn and the model acted on it — which verifies the delivery premise for the first time, though a steer measured against a still-generating response did not preempt it — the full answer completed first, so live steering changes what happens after the current response, not the work inside it ([result](experiments/app-server-live-observation-v1-result.md)) | Execute framing and wrong-nudge susceptibility measurements in #23/#34 before broader enablement |
| `INTERRUPT` / `RESTART` | 🟡 | No live recovery path; the off-by-default `reviewer.shadow_interrupt` opt-in journals a labelable `would_interrupt` record for every `NUDGE`, naming the exact thread/turn/connection-epoch target it would have aborted, and marking a target that settled during review as suppressed instead of dispatchable. `turn/interrupt` is never sent by the reviewer path, so Codex never persists its canonical abort marker; a directly dispatched interrupt now settles only on an observed `turn/completed`, separating `turn_aborted` from a target that finished anyway through one classifier both the boundary and post-ACK paths share, so message ordering cannot change the reported outcome, and never treating a `final_answer` as an abort boundary; a bounded deadline closes a silent turn's interrupt as `unknown` instead of leaving the lifecycle open, and survives a restart through durable acceptance hydration without re-settling an already terminated interrupt; `spotter interventions` and `spotter explain` surface that lifecycle by control ID, since interrupts carry no advisory-input correlation to key on; RESTART does not exist | Judge the shadow records in #34/#38 before enabling live interruption |
| Packaging / long-term operations | ✅ | Exact version tags publish verified release artifacts; the dedicated Homebrew tap installs immutable releases and opens repeatable Formula-update PRs; the macOS lifecycle gate proves clean install, live G1→G2 upgrade/reconcile, teardown-less fail-open uninstall, retained data, and reinstall/teardown without Cellar assumptions; every implemented durable Spotter family has an independent schema identity, bounded legacy behavior, and explicit non-destructive future/corrupt handling; snapshot refs, detached restore worktrees, daemon logs, and reviewer logs carry exact versioned ownership records, while purge revalidates repository ownership and preserves journal/recovery/fork/experiment-result/worktree/manual-pin reachability; focused data/integration/snapshot/log scopes remain independently usable, and destructive `purge --all` composes them in lifecycle order; configuration now has one validated layered resolver, source-aware generations, exhaustive per-key activation boundaries, an atomic active/pending snapshot store, explicit and file-triggered daemon reload, quiescent cross-thread next-turn activation, submission-pinned signal reviewer generations, generation-attributed Hook gate requests, and generation-pinned daemon Trace/signal provenance; invalid/racing reloads preserve prior valid active and pending state; CLI↔daemon handshakes publish protocol ranges, named capabilities, exact build/runtime generations, a non-secret effective runtime-construction fingerprint, and current App Server connection epoch/capability identity, classify compatible stale/legacy-unknown peers, diagnose construction drift, restart known same-build construction mismatches, journal server/capability changes across reconciled epochs, and reject incompatible control with actionable restart diagnostics; Codex setup/runtime bootstrap consistently rejects malformed, prerelease, and pre-0.147.0 hosts while newer stable hosts proceed through capability negotiation, and mixed setup/runtime host versions remain operationally capability-gated but diagnose the required setup reconciliation; `spotter update` detects Homebrew, pipx, uv tool, pip, editable, and source ownership and prints advisory-only version/upgrade/reconciliation commands without modifying package files; setup stages candidate runtime inputs before restarting a changed integration generation, verifies the reconciled runtime, and restores both the prior manifest and runtime inputs on failure | Keep the mixed-version acceptance matrix in the release regression gate |

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
| Can Spotter produce plausible semantic reviewer verdicts? | Yes; periodic review is shadow-only, while fresh signal-driven advisories have a separate off-by-default live-delivery opt-in |
| Can Spotter branch a shared prefix for counterfactual experiments? | Yes |
| Is the fork instrument's causal noise floor known? | **No — #42** |
| Is there a reproducible mechanically scored task corpus? | **Yes — the synthetic v2 foundation and immutable external cohorts are frozen; v3 produced one natural control failure, while its neutral run exposed #351 and remained 0/3 judgeable (#42)** |
| Has live Spotter guidance been shown to improve outcomes? | **No** |
| Is the App Server control boundary operationally viable? | **Yes for a configured Spotter-managed external path, including daemon reconnect/reconciliation** |
| Is the post-App-Server visible-in-time ceiling measured? | **Partially — #303 stopped by rule after the later-user-input row failed twice; command success was visible in time, but every observation Hook remains NO-GO for removal** |

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
