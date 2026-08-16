# Architecture

> **Status:** this document describes both the current hook-based prototype and the target architecture. Active implementation is tracked by the [Roadmap](roadmap.md) and native GitHub Milestones.
> The `spotterd` process/control foundation, bounded Hook gate IPC, shared-server PoC, production App Server transport, durable Trace IR ingestion, daemon reconnect/reconciliation, stable packaged runtime layout, and off-by-default live advisory delivery are implemented. Broader live enablement remains evidence-gated.

---

## 30-second summary

The target architecture fits on one page:

```text
                     ┌─────────────┐
                     │  Codex TUI  │
                     └──────┬──────┘
                            │ same thread / turn
                            ▼
                 ┌──────────────────────┐
                 │ External App Server  │
                 │ observation/control  │
                 └──────┬────────▲──────┘
                        │        │
              events    │        │ turn/steer
                        │        │ turn/interrupt
                        ▼        │
                 ┌──────────────────┐
                 │     spotterd     │
                 │                  │
                 │ Thread Manager   │
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
```

Six decisions define the design:

1. **Codex App Server becomes the primary observation source.**
2. **Semantic review is asynchronous.** Main does not wait for an LLM reviewer before ordinary actions.
3. **`PreToolUse` remains only for deterministic, atomic enforcement.**
4. **`spotterd` memory owns live supervision state.** The journal is durable history and recovery material.
5. **Reviewer decisions are anchored to a specific thread/turn.** Late decisions must pass a freshness check.
6. **The App Server lifecycle/attach PoC is a hard prerequisite.** If Spotter cannot share the user's real App Server, this architecture must be revisited.

---

## Quick navigation

| Looking for... | Section |
| --- | --- |
| Current vs target structure | [1. Current vs target](#1-current-vs-target) |
| Component responsibilities | [3. Component contracts](#3-component-contracts) |
| Exact runtime flows | [4. Runtime flows](#4-runtime-flows) |
| Why any Hook remains | [5. Enforcement path](#5-enforcement-path-pretooluse) |
| Reviewer timing/freshness | [7. Reviewer jobs and freshness](#7-reviewer-jobs-and-freshness) |
| State and persistence boundaries | [8. State ownership](#8-state-ownership) |
| Thread / turn / attachment terminology | [9. Identity model](#9-identity-model) |
| Disconnect / crash behavior | [11. Failure and degraded mode](#11-failure-and-degraded-mode) |
| Runtime files/sockets/resources | [12. Runtime resources](#12-runtime-resources) |
| Agent adapter interface | [14. Agent adapter contract](#14-agent-adapter-contract) |
| Runtime architecture gate | [15. Runtime gate: App Server lifecycle / attach PoC](#15-runtime-gate-app-server-lifecycle--attach-poc) |

---

# 1. Current vs target

## 1.1 Current prototype

Today, hooks carry both observation and execution-boundary responsibilities:

```text
Codex / Claude Code
       │
       │ SessionStart / UserPromptSubmit
       │ PreToolUse / PostToolUse
       ▼
  spotter-hook process
       │
       ├─ config load
       ├─ journal append
       ├─ deterministic gate
       ├─ snapshot
       └─ reviewer cadence trigger
```

This shape was useful because it is simple and fail-open by construction. It was enough to collect real trajectories and validate gates, snapshots, fork/replay, shadow review, labels, metrics, and counterfactual experiments.

Its limitations are now structural:

- every hook invocation pays process/bootstrap cost;
- durable journal files carry too much responsibility for shared state;
- there is no long-lived semantic supervision state;
- a reviewer cannot finish later and independently inject into an active turn through the hook that already returned;
- periodic review spends tokens even when nothing looks wrong;
- richer App Server events are not the primary observation surface.

## 1.2 Implemented runtime foundation

`spotterd` is an installable long-lived process with a versioned newline-delimited JSON control
protocol over a per-user UNIX socket. `spotter daemon start|stop|restart|reload|status` exercises a real
handshake rather than treating a PID file as liveness. The server serializes ownership, accepts
concurrent clients, reports `healthy` / `degraded` / `recovering`, and makes absence explicit as
`unavailable`. Reload resolution runs off the daemon event loop, then atomically applies only `HOT`
changes or stages the whole candidate for a later turn boundary; status reports active/pending
generations and the last rejected reload. Staged snapshots publish before normalization of the next
turn only after every daemon-owned thread is quiescent, so concurrent turns cannot mix reviewer or
MCP-effect semantics generations. PreToolUse requests independently carry the generation resolved
by their short-lived Hook invocation; the Hook journal and any correlated daemon block keep that
generation rather than inferring it from the daemon's current snapshot.

Manual process management implements the `ServiceManager` boundary used by the CLI. For a configured
endpoint, the daemon owns one App Server connection/reconnect loop, routes epoch-tagged Trace IR into
durable journals and `ThreadState`, and reconciles loaded threads before reporting control-ready.
It never owns or stops the shared App Server process; existing Hook enforcement remains independent.

## 1.3 Target runtime

The target hot path is daemon-owned:

```text
App Server event
      │
      ▼
spotterd
  ├─ normalize event
  ├─ update live state
  ├─ append journal
  ├─ evaluate cheap signals
  └─ schedule semantic reviewer when needed

Main continues

PreToolUse remains a separate synchronous path
```

The state model flips from:

```text
hook → journal → rebuild state when needed
```

to:

```text
event → live state → policy
                 └→ durable journal append
```

---

# 2. Runtime planes

Three planes are intentionally separate.

| Plane | Question | Codex target surface | Latency model |
| --- | --- | --- | --- |
| Observation | What is happening? | App Server event stream | asynchronous |
| Control | How can Spotter influence an active trajectory? | `turn/steer`, `turn/interrupt` | asynchronous request |
| Enforcement | What must be decided before an action executes? | `PreToolUse` | synchronous and bounded |

### Observation plane

Expected inputs include:

- thread start/resume/archive lifecycle;
- turn start/completion/interruption;
- user messages;
- plan updates;
- reasoning summaries when exposed by the runtime/model;
- command/tool start and completion;
- stdout/stderr/exit status where available;
- file changes and diffs;
- MCP calls;
- web searches;
- token usage.

### Control plane

Expected mappings:

```text
VERIFY     → turn/steer(evidence request)
NUDGE      → turn/steer(course correction)
INTERRUPT  → turn/interrupt
RESTART    → interrupt + fresh continuation (later phase)
```

### Enforcement plane

Only deterministic policy belongs here:

```text
PreToolUse
  ↓
ALLOW / DENY
```

No model call should be required to answer the synchronous request.

---

# 3. Component contracts

These boundaries are intended to be concrete enough to drive implementation.

| Component | Responsibility | Inputs | Outputs | State owned | Failure posture |
| --- | --- | --- | --- | --- | --- |
| `spotter` CLI | user control plane: setup/status/doctor/review/etc. | argv, config, daemon RPC | command output / mutations | short-lived command state | explicit command error |
| `spotterd` | long-lived supervision runtime | App Server events, Hook IPC, CLI RPC | journal records, reviewer jobs, interventions | thread/live state | degrade without breaking coding session |
| `CodexAppServerClient` | connect, subscribe, control | endpoint + capability negotiation | normalized runtime events, RPC results | connection state | reconnect / degraded |
| `SessionManager` | thread/turn/attachment lifecycle | normalized events | resolved identity/state transitions | thread registry | isolate failure to affected thread when possible |
| `TraceNormalizer` | backend event → Trace IR | raw adapter event | `TraceEvent` | none | preserve unknown event / skip with telemetry |
| `AuditState` | goal/constraint/claim/evidence/progress model | `TraceEvent` | current independent state | per thread | mark missing/unknown instead of inventing |
| `SignalEngine` | cheap candidate detection | event + live state | candidate signal | bounded rolling counters/state | no candidate |
| `ReviewerScheduler` | async model execution, budget, dedupe | candidate + context | `ReviewerJob` | queues, budgets | job fails; Main continues |
| `InterventionController` | freshness, delivery, escalation | reviewer decision + live turn state | steer/interrupt/no-op | intervention history | stale/discard/degraded |
| `GateEngine` | deterministic pre-action policy | `PreToolUse` proposal + config | allow/deny | rule config + bounded state | local fallback / fail-open |
| `JournalStore` | durable event history | normalized records | append/read | disk | write telemetry; never invent success |
| `SnapshotManager` | Git checkpoints and detached restore | repo/worktree state | snapshot ref/worktree | Git resources | snapshot failure does not break Main |
| `IntegrationManager` | setup/teardown/migration | agent config + runtime environment | integration mutations + manifest | integration manifest | transactional rollback |

The key architectural constraint is that **Spotter core policy must not depend directly on Codex transport/event shapes**.

---

# 4. Runtime flows

## 4.1 Codex startup

Ideal managed-mode flow:

```text
user login / runtime ready
        │
        ├─ spotterd ready
        └─ external App Server available

user runs: codex
        │
        ▼
Codex TUI attaches to external App Server
        │
        ├─ TUI = client A
        └─ Spotter = client B
```

Important constraint: plain `codex` does not auto-discover a separately started external
App Server in the current interface; the external endpoint requires `--remote`. Starting
Spotter only at the first `PreToolUse` is therefore too late for full observation/control.

That is why the Runtime App Server gate (#78) comes before the daemon migration.

## 4.2 Normal observation event

Example: a command completes.

```text
App Server
  │ command completed
  ▼
CodexAdapter
  │ raw event → TraceEvent
  ▼
SessionManager
  │ resolve thread/turn/item identity
  ▼
Live State
  ├─ update recent failures
  ├─ update validation state
  └─ update touched scope
  │
  ├──────────────► Journal append
  │
  ▼
SignalEngine
  │
  ├─ no candidate → done
  └─ candidate → ReviewerScheduler
```

Normal observation should not require a Hook process.

The first implemented `SignalEngine` family incrementally detects consecutive classified failures
for the same explicit normalized resource. It requires stable event identity and explicit file,
resource, test, or MCP tool scope; opaque shell commands remain unknown rather than being parsed or
guessed. The second failure journals an `active` candidate, later failures in the same streak journal
`cooled_down` suppression, and success or a turn/epoch/gap boundary records `resolved` or `stale`.
Candidate identity, target turn/epoch, triggering state version, bounded evidence IDs, resource scope,
and deterministic magnitude are durable. Recovery reconstructs cooldown state and backfills a
derived candidate if the process stopped after the source outcome append. Active candidates now
queue one durable reviewer job per signal lifecycle, bound to the exact immutable `ThreadState`
snapshot named by the candidate. Queue recovery replays that snapshot and backfills an interrupted
derived append; target turn/epoch changes discard pending jobs before execution. Reviewer model
input is now an immutable bounded slice of that snapshot: candidate features/evidence references,
goal and constraints, touched/unvalidated files, recent failures, validation state, and known
coverage gaps. Per-field truncation is explicit in the durable queue event; the full transcript is
not copied into the job. With explicit `reviewer.on_signals = true`, `spotterd` now runs these jobs
asynchronously through a cancellation-safe Codex subprocess, serializes paid calls, enforces the
existing session/day budget ledger before inference, and journals queue latency, inference timing,
token spend, errors/caps, and stale decisions. Delivery remains separately off by default. With
explicit active-mode `reviewer.deliver_on_signals = true`, a fresh signal-driven `VERIFY`/`NUDGE` is sent once
through the exact attachment/turn/epoch control target; `CONTINUE` and stale decisions never send.
The compact advisory states that it is not a new user requirement, retains current-turn scope in
durable control telemetry, and never retries an ambiguously accepted steer.

The implemented engine covers all eight initial families: failure streaks, repeated equivalent
calls, reads without frontier expansion, recurrence after a deterministic block, touched-scope
growth, edits without scope-matched validation, stale-hypothesis reuse, and relative budget
anomalies. Candidates from one immutable state can merge into a single priority-ordered, budgeted
review job without losing their individual evidence identities. Every queued job and terminal
reviewer decision records whether it was launched by a signal, the periodic Hook cadence, or a
manual request. Legacy queue identity is classified conservatively. This lets `spotter metrics`
compare reviewer calls, precision, and negative-decision misses by trigger instead of pooling the
periodic-only, event-driven, and mixed cohorts. The evidence decision about replacing periodic
review remains with #38/#24; trigger provenance does not itself prove better outcomes.

## 4.3 Semantic review

```text
SignalEngine
  │ POSSIBLE_TOOL_FAILURE_LOOP
  ▼
ReviewerScheduler
  │ create job(thread=T1, turn=U7)
  ▼
Reviewer runs asynchronously

          Main continues

ReviewerDecision
  │ NUDGE
  ▼
InterventionController
  │ target U7 still active?
  ├─ yes → turn/steer
  └─ no  → stale policy
```

## 4.4 Deterministic gate

```text
Codex proposes: git reset --hard
        │
        ▼
PreToolUse
        │ stdin JSON
        ▼
spotter-hook
        │ bounded IPC
        ▼
spotterd GateEngine
        │
        ├─ ALLOW
        └─ DENY(rule=git_reset_hard)
        │
        ▼
Hook response
```

No network/model call belongs on this path.

The implemented bridge sends only normalized command/file proposal data, deterministic gate config,
and the workspace root over the versioned local socket. It has a 200 ms total request deadline. A
missing daemon or timeout falls back to the same local deterministic Gate while recording `gate_ipc`
failure telemetry, so the manual-start lifecycle does not disable existing enforcement. Malformed or
version-mismatched responses and unsupported proposal shapes fail open. Daemon evaluation time, IPC
time, and total Hook time are recorded separately so latency percentiles can be computed without putting
aggregation on the synchronous path.

`spotter metrics` projects these durable records into separate accounting domains: Main semantic
actions (including command/file/tool family and classified-failure counts) and token observations,
Spotter semantic reviewer calls/tokens, deterministic Hook/IPC
latency, timing coverage, and journal storage. Main actions use one semantic identity across Hook and
App Server surfaces, so the same operation is counted once globally; per-surface raw observation
counts remain visible, and the App Server outcome is authoritative for an overlap. Token totals
remain `cumulative/unknown-scope`, and the latest total per session is used so cumulative updates are
not double-counted. Input, cached input, cache-write input, output, and reasoning-output fields each
retain their own session coverage. Reviewer token totals use the final cumulative spend observation
per reviewer session and report both session and call denominators as exact, partial, or unavailable;
missing token/timing/latency data is never presented as zero. Every new
durable record carries receipt wall time plus a process-local monotonic timestamp and clock-domain
ID. Signal, queue, and detection-to-decision latency is correlated through evidence, signal, and
review-job IDs and is subtracted only within one monotonic domain; daemon restarts therefore reduce
coverage instead of manufacturing a duration. Source wall time is never subtracted from receipt
wall time to manufacture a latency across clock domains. Signal-driven reviewer decisions are also
joined to the matching thread/turn/connection-epoch completion boundary. A decision before that
boundary reports supervision lead time; a decision after it reports lag. Missing identity, missing
boundaries, and cross-restart clock domains remain uncovered rather than falling back to wall time.
App Server events also retain a monotonically increasing arrival sequence within each connection
epoch, so equal source timestamps remain durably ordered across journal reloads.

Runtime control uses a stable Spotter control ID across dispatch, RPC acceptance, and terminal
failure records. Terminal outcomes distinguish an RPC rejection (`failed`), loss of transport after
dispatch (`unknown`), and an epoch/turn freshness rejection (`stale`). For steering, the same ID is
sent as App Server `clientUserMessageId`; the normalized `userMessage.clientId` becomes
`client_user_message_id`. Only a matching ID on the same thread, turn, and connection epoch counts
as an observed adoption, and that observation must be durably ordered after its dispatch. The live
reconciler annotates that input as `spotter_supervision`, journals a `control_observed_in_turn`
outcome, and reduces the advisory into supervision state without replacing the authoritative user
goal. A known intervention ID observed outside its target identity instead journals
`control_observed_outside_target` with `expired_advisory_visible`; it is diagnostic evidence, not a
new user goal. A control ID already present in durable runtime history is rejected before RPC;
ambiguous legacy history with
multiple dispatches for one ID is reported and excluded from adoption. RPC acceptance alone does
not prove observation. `spotter metrics` reports same-clock
dispatch and adoption latency, evidence-to-adoption latency, adoption lead/lag against the target
turn boundary, and decision-to-stale-delivery latency with coverage denominators. Dispatch lifecycle
records capture receipt timing inline but enter a bounded queue; journal locking, history recovery,
and fsync run on a worker rather than the control RPC path. Queue overflow and writer failures are
explicit runtime health counters, and graceful shutdown drains accepted records. Until live reviewer
delivery is enabled, sessions without it remain explicitly uncovered rather than implying a zero cost.

Codex `agentMessage.phase` is also a bounded terminal hint. A completed message explicitly marked
`final_answer` settles the target for soft intervention even if `turn/completed` has not arrived:
pending/running review jobs become stale and a later steer is rejected locally with
`terminal_answer_settled`. A started item, commentary phase, or absent phase does not trigger this
fence because providers do not populate the field consistently. Interrupt remains a separate
recovery control and is not disabled by this soft-intervention fence.

Accepted steers remain pending until correlated input or a trustworthy target boundary appears. If
the target emits a completed `final_answer` or `turn/completed` first, the controller journals a
terminal `rpc_accepted_only` outcome with the specific boundary reason. This reconciliation runs
both when the boundary arrives and immediately after RPC acceptance, so response/notification
ordering cannot lose the terminal classification. A matching input that becomes visible only after
the boundary is classified outside-target rather than retroactively proving in-turn adoption.

Codex control-error compatibility is confined to the App Server adapter. Structured
`activeTurnNotSteerable` data maps to `turn_not_steerable`; current version-specific messages for
no active turn and expected-turn mismatch map to `no_active_turn` and `turn_mismatch`. Core runtime
control sees only those semantic reasons: no-active/mismatch outcomes are stale, not generic RPC
failures, while a non-steerable active turn remains failed. Unknown messages remain `rpc_rejected`
rather than being guessed, and transport loss after dispatch remains acceptance-unknown.

Objective task outcomes remain outside ordinary user-session telemetry. For global reports,
`spotter metrics` reads versioned counterfactual and frozen-task result journals, joins each
mechanical classification to costs carried by the same stable run/pair/arm identity, and reports
coverage when agent-reported tokens or elapsed timestamps are unavailable. Complete pairs are formed
by stable pair identity before the report places aggregated control/guidance or neutral-arm cost
coverage directly beside the corresponding paired outcome summary; orphan arms are never paired by
role alone. The independent `spotter.experiment_result` schema v3 reads legacy v1-v3 rows and
refuses future, foreign, or corrupt history before each crash-durable append. It records
the parsed token total and same-process monotonic agent duration for each executed arm; it does not
persist raw agent stderr merely to recover the token count. Earlier v1/v2 rows remain readable.
Unsupported newer result schemas fail visibly instead of being guessed; a torn final JSONL row is
treated as an interrupted append and blocks further mutation. This projection does not label
unscored user sessions as success or failure. `spotter analyze` additionally joins task-arm outcomes
through `replay_source_session_id` and replay outcomes through the persisted arm session or fork manifest's
`prefix.source_session_id`. It does not infer a join from file names or timestamps; missing or broken
fork provenance is reported instead of guessed.

Frozen corpus inputs separately identify `spotter.task` and `spotter.task_set` v1 manifests. Their
append-only run output uses `spotter.task_batch` v1 on metadata, arm, and completion records. Legacy
schema-name-less v1 artifacts remain readable, while resume validates identity before preflight and
performs locked, crash-durable append or validated torn-tail repair without mutating incompatible
history.

Fork lineage uses the independent `spotter.fork_manifest` schema. Readers retain bounded v1-v6
compatibility, with schema-name-less manifests preserving their historical coverage limits. Every
current state transition validates an existing manifest under its resource lock before fsynced
atomic replacement, so an older writer cannot overwrite future, foreign, or corrupt lineage.

## 4.5 Turn completion

```text
turn/completed(U7)
      │
      ├─ active_turn = none
      ├─ finalize timing/tool counters
      ├─ finalize validation state
      └─ re-evaluate freshness of ReviewerJobs targeting U7
```

The thread remains durable.

## 4.6 Resume

```text
Codex resumes existing thread T1
        │
        ▼
App Server thread event
        │
        ▼
SessionManager finds Spotter durable history
        │
        ├─ hydrate missing live state from journal
        ├─ reconcile current App Server thread state
        └─ create a new RuntimeAttachment
```

Journal replay belongs at recovery/resume boundaries, not in the ordinary event loop.

---

# 5. Enforcement path: `PreToolUse`

The target Codex integration should keep only the hook surface that provides a unique atomic guarantee.

| Hook | Current use | Target |
| --- | --- | --- |
| `SessionStart` | legacy session baseline snapshot | App Server `thread/started` now owns the baseline; remove the redundant Hook |
| `UserPromptSubmit` | user goal capture | replace with App Server user-message events |
| `PreToolUse` | proposal observation + gate | **retain for deterministic atomic enforcement** |
| `PostToolUse` | legacy result/snapshot/reviewer cadence | App Server terminal items now own after-state snapshots; finish result/reviewer migration, then remove the Hook |

### Appropriate synchronous policies

- destructive command classes;
- forbidden paths;
- dependency-manifest policy;
- workspace escape;
- explicit user/project execution restrictions;
- later: external writes that can be classified deterministically enough.

### Inappropriate synchronous policies

- “this refactor looks unnecessary”;
- “this hypothesis is probably wrong”;
- “another implementation approach seems better”;
- any judgment that requires broad semantic repository analysis.

Those belong in the asynchronous reviewer path.

### Failure policy

If daemon IPC is unavailable or times out, the Hook evaluates the same deterministic Gate locally
and records the degraded IPC status. If the proposal itself cannot be judged safely, including:

```text
unsupported syntax
unknown workspace
```

the default posture remains **fail-open + telemetry**.

Spotter failure must not become a coding-agent outage.

---

# 6. Trace IR

Raw backend events should be normalized before policy consumes them.

```text
Codex App Server event
Claude event
future agent event
       │
       ▼
   Adapter
       │
       ▼
   Trace IR
       │
       ├─ live state
       ├─ signals
       ├─ reviewer
       ├─ metrics
       └─ journal
```

Minimum conceptual fields:

```text
TraceEvent
  event_id
  agent
  agent_thread_id
  turn_id
  item/tool_id
  timestamp
  connection_epoch / arrival_seq
  kind
  operation
  files/resources
  result/outcome
  repository/worktree
  provenance(raw event reference)
```

Policy-oriented fields can be layered on later:

```text
constraint ids
candidate hypothesis ids
causal hypothesis ids (`hypothesis_ids[]` only on actions with an explicit relation)
validation relation (`validated_paths[]` only when the source proves file/directory coverage)
side-effect class
```

Absent validation scope remains unknown: a passing test outcome does not clear unrelated
`edits_since_validation` entries. Likewise, an action without `hypothesis_ids` does not prove that
it reused or ignored any hypothesis.

Budget candidates also stay relative to observed capacity: token pressure uses an explicit model
context window, and duration anomalies require a same-operation/resource median baseline. Missing
capacity, duration, or resource identity remains unknown rather than falling back to one global
repository threshold.

Normalization must not discard provenance. Live control, replay, and causal analysis must be able to reconnect normalized records to the underlying runtime item/turn.

## Implemented App Server ingestion

`CodexTraceNormalizer` is the transport boundary. It maps App Server lifecycle and authoritative
completed-item records into the runtime-neutral fields above. Raw reasoning content is not copied;
only the App Server's exposed reasoning summary enters Trace IR. Unknown notification methods become
`runtime_event_unknown` records containing the method and any proven identity, rather than silently
disappearing or leaking a new wire shape into core consumers.

`AppServerTraceIngestor` writes one journal per Spotter thread identity, separately named from
Hook-era session journals. Its recovery scan rebuilds deduplication and lifecycle state after a
restart. Item starts and outcomes correlate by App Server item ID plus connection epoch, not journal adjacency. A terminal
event seen before its start is retained with `observed_start=false`; a later start cannot regress it.
Timestamp regressions are accepted but marked `out_of_order`, and conflicting terminal outcomes fail
explicitly. Hook records carry legacy-session provenance with unknown thread, turn, and attachment
dimensions, so consumers never need Codex transport objects and never infer that a Hook session is an
App Server thread.

Runtime metrics count unique resources only from explicit normalized fields: file paths, classified
Hook resources, and MCP server/tool identities. Lifecycle observations for one correlated action are
deduplicated, and reports include the number of semantic actions that declared a resource. Commands
and opaque tool arguments are not parsed to invent resource identity.

The daemon also writes a bounded, value-free source-shape audit beside session journals. It records
which App Server field paths existed, which normalized fields and evidence families survived, and
whether the fact remained available in live `ThreadState`. Source values, raw reasoning, command
output, and encrypted content are never copied into this audit. `spotter observability` compares the
Hook and App Server surfaces without treating adapter/state loss as a theoretical source limit. See
[Observability ceiling baseline](observability-baseline.md).

---

# 7. Reviewer jobs and freshness

Semantic review is modeled as a job lifecycle:

```text
QUEUED
  ↓
RUNNING
  ↓
DECIDED
  ├─ DELIVERED
  ├─ STALE
  ├─ DISCARDED
  ├─ CANCELLED
  └─ FAILED
```

Minimum `ReviewerJob` metadata:

```text
job_id
thread_id
target_turn_id
candidate_id
created_at
started_at
finished_at
reviewer model/config fingerprint
input coverage/truncation
verdict
confidence
delivery status
```

### Freshness rule

Main may finish a turn while the reviewer is thinking:

```text
U7 signal
 ↓
reviewer running
 ↓
U7 completes
 ↓
reviewer returns NUDGE
```

That NUDGE must not automatically be injected into U8.

A conservative initial policy:

```text
same target turn still active
  → deliver

target turn ended
  → STALE / DISCARD by default
  → only explicitly defined classes may be deferred
```

Always record enough timing data to measure:

- reviewer latency;
- delivery latency;
- stale rate;
- reviewer spend wasted on stale verdicts.

---

# 8. State ownership

## 8.1 Live state

`spotterd` should hold at least the following per thread:

```text
ThreadState
  identity
  repository/worktree
  user goal
  constraints
  active turn
  hypotheses
  evidence
  open questions
  touched files
  validation state
  recent failures
  recent actions
  intervention history
  pending reviewer jobs
  reviewer budget
  connection/capability state
```

The implemented `ThreadStateStore` is the daemon-owned, single-event-loop hot-state boundary.
`ThreadStateReducer` consumes only normalized `TraceEvent` values and returns deeply immutable,
versioned snapshots. It keeps task, evidence/verification, workspace, execution, supervision, and
coverage as distinct typed sub-state; reasoning summaries remain hypotheses, and verified facts can
only be created by an explicit verification-satisfied event. Duplicate event IDs are idempotent,
out-of-order lifecycle is recorded as partial coverage, and thread identities remain isolated.

Journal hydration deterministically replays Trace IR but clears active-turn/control readiness. A
recovered daemon receives a live `runtime_reconciled` event only after the App Server thread list/read
pass proves current attachment and exact active-turn identity. Until then control remains unavailable.

## 8.2 Durable journal

The journal exists for:

- crash recovery;
- resume hydration;
- offline analyze;
- labels/metrics;
- replay/fork;
- experiment provenance;
- forensic inspection.

It is **not** the live-state database.

Human labels remain in a separate append-only versioned store so they never enter reviewer input or
shift replay step identity. Label schema v6 records a stable rater identity, an independent
measurement scope, `tp|fp|unclear`
judgments for active signal candidates and reviewer interventions, and distinct
`miss|tn|unclear` judgments for correlation-proven unflagged tool proposals and explicit reviewer
`CONTINUE` decisions while retaining v0-v5 read compatibility. The independent
`spotter.signal_sampling` schema v1 reads schema-less v1 history, writes explicit current record
identity, and refuses future or foreign records before append. It stores
disjoint deterministic journal-suffix frames with their detector type, declared event-kind strata,
inclusion probability, exclusions, and target fingerprints before scoped silence labels are
accepted. Precision and miss metrics still use
the latest judgment per target; signal precision is stratified by signal type, while agreement
metrics independently use the latest judgment per rater and report exact agreement only for the
explicitly double-labeled subset. Unattributed legacy, stale, uncorrelatable, and out-of-frame
coverage remains visible; sampled miss rates state that they do not generalize beyond their declared
event-kind strata.

Intervention timing annotations use a separate opportunity schema v1. Each independent rater pins
semantic and observable earliest/latest bounds plus required evidence to stable Trace IR event IDs
and content fingerprints. Journal steps remain only locators; a changed or missing event makes the
annotation stale instead of silently moving the warranted window. Append-only corrections retain
the latest window per opportunity and rater. These annotations do not enter reviewer input and do
not assume that semantic and observable windows are nested. #24 metrics link a signal only when its
active candidate cites every required evidence event, classify it as `EARLY`, `WITHIN_WINDOW`,
`LATE`, `NEVER`, or `UNJUDGEABLE`, and report bounded step/source-clock delay plus deduplicated
post-window actions, failed outcomes, and files. Unrelated candidates never stop the opportunity
clock, stale annotations remain outside the distribution, and an open journal cannot become a
fabricated `NEVER`. An observation gap crossing the measured interval also makes delay and
post-window work `UNJUDGEABLE` rather than pretending the missing surface was quiet.
`NEVER` requires a terminal turn/thread/session event at or after the annotated window; unrelated
later activity in a still-live trajectory keeps the opportunity `UNJUDGEABLE` and re-derivable.
For an evidence-linked candidate, the same report follows durable `candidate_event_ids` into its
signal-driven review job and `review_job_id` through inference start and decision. It reports
candidate-to-queue, queue-to-inference, inference-to-decision, and queue-to-decision step coverage,
classifies the decision against the observable window, and keeps stale decisions, missing decisions,
and intervals crossed by observation gaps explicit instead of assigning them ordinary latency.
For non-stale `VERIFY`/`NUDGE` decisions, the report then follows `review_job_id` to the first control
attempt and `control_id` to RPC acceptance or a terminal control outcome. Dispatch timing is
classified against the same observable window; missing dispatch/resolution, stale controls,
failures, unknown outcomes, and gap-crossed intervals remain separate coverage states.
Accepted steers are adoption-eligible only when they expose a client message ID and exact
thread/turn/connection target. A matching `user_prompt` observed after dispatch is classified
against the opportunity window; a target-turn terminal without that observation is
`RPC_ACCEPTED_ONLY`, while stale-after-accept and identity/gap-limited cases stay separate.

## 8.3 Snapshot state

A Git snapshot is a separate resource representing repository state at a branch/recovery point.

```text
ThreadState ≠ Journal ≠ Snapshot
```

- `ThreadState`: current supervision state;
- `Journal`: event/history record;
- `Snapshot`: filesystem/Git state.

---

# 9. Identity model

The word “session” is overloaded. The target runtime should distinguish:

```text
Agent Thread
  long-lived coding task/conversation identity

Turn
  one user→agent execution unit

Runtime Attachment
  one TUI/client attachment period to a thread

Reviewer Job
  one asynchronous evaluation of a candidate/turn
```

Example:

```text
Thread T1
├─ Attachment A1 (today)
│  ├─ Turn U1
│  └─ Turn U2
└─ Attachment A2 (resumed tomorrow)
   ├─ Turn U3
   └─ Turn U4
```

State scope:

| State | Scope |
| --- | --- |
| goal / constraints / hypotheses | Thread |
| active work | Turn |
| reviewer freshness | Turn |
| connection latency | Runtime Attachment |
| durable journal lineage | Thread |
| fork branch point | Thread + Turn/Step |

## Provisional identity foundation

`RuntimeIdentityRegistry` sketches this transport-independent boundary:

- agent-scoped external thread and turn IDs are retained as provenance and mapped deterministically
  to Spotter IDs;
- explicit attachment records track active/closed state, and concurrent external attachment IDs do
  not merge;
- duplicate lifecycle events are idempotent, while conflicting terminal states fail explicitly;
- a terminal event observed before its start creates a terminal turn marked `observed_start=False`;
- Hook-era `session_id` becomes a `RuntimeIdentity` with unknown thread/turn/attachment fields rather
  than being promoted into an identity it cannot prove.

The registry owns identity and lifecycle only. App Server Trace normalization and the daemon's
semantic `ThreadState` consume those identities without importing Codex wire shapes. Each physical
connection gets a new attachment ID and monotonically recovered epoch; logical thread IDs survive it.

---

# 10. App Server connection and capability model

Spotter should negotiate capabilities instead of relying on one minimum Codex version string.

Candidate capabilities:

```text
observe_thread_lifecycle
observe_user_message
observe_tool_start
observe_tool_result
observe_diff
observe_token_usage
steer_active_turn
interrupt_active_turn
atomic_pretool_veto
```

Example status output:

```text
Codex integration
  observation.thread:       yes
  observation.tool_result:  yes
  observation.diff:         yes
  control.steer:            yes
  control.interrupt:        no
  enforcement.pretool:      yes
```

This allows a Codex upgrade to degrade one feature without forcing a binary “supported/unsupported” result for the whole integration.

`spotter status` now reports this split without probing external services. `spotter doctor` performs
the deeper App Server connection/initialize probe when the integration manifest has an endpoint.
When the integration manifest contains an endpoint, `spotterd` keeps the persistent consumer and a
full per-connection capability/server fingerprint. Endpoint selection remains explicit and pending in
setup rather than being inferred from a listener that merely answers a socket.

---

# 11. Failure and degraded mode

Health must be tracked per subsystem:

```text
spotterd health
App Server connection
Observation capability
Control capability
PreToolUse enforcement
Journal/storage health
Reviewer provider health
```

Example:

```text
Spotter daemon:       running
App Server:           reconnecting
Observation:          unavailable
Live steer:           unavailable
PreToolUse gate:      active
Journal:              writable
Reviewer:             available
```

The aggregate command health is healthy/degraded/broken (`0`/`1`/`2`). Known unimplemented or
optional unconfigured surfaces are informational rather than permanent warnings; absence of every
runtime registration still warns. In particular, a configured healthy daemon with no App Server
observation path is degraded while the independent PreToolUse enforcement surface can remain active.

### `spotterd` crash

```text
spotterd crash
  ↓
service manager restart
  ↓
App Server reconnect
  ↓
list active/loaded threads
  ↓
reconcile + journal hydrate
  ↓
READY
```

During daemon downtime, the Hook gate fails open.

### App Server disconnect

```text
DISCONNECTED → CONNECTING → RECONCILING → READY
                    ↑                       │
                    └─ BACKING_OFF ← DEGRADED
```

Every successful physical connection advances the durable-enough local epoch. Disconnect intervals
produce per-thread `observation_gap` records; current thread/turn state is queried rather than
invented. Control calls must name the exact reconciled epoch and active turn, so late results from an
older connection are rejected instead of silently retargeted.

A healthy daemon with no observation/control channel is not fully healthy.

### Reviewer failure

```text
reviewer timeout/error
  → ReviewerJob FAILED
  → Main unaffected
  → error/spend telemetry
```

---

# 12. Runtime resources

`RuntimeLayout` is the package/runtime path-discovery boundary. It resolves current package entry
points and logical config, durable-data, integration, runtime, and log locations without treating a
repository checkout or current working directory as packaged state. Install adapters may supply
explicit `SPOTTER_CLI_EXECUTABLE` / `SPOTTER_DAEMON_EXECUTABLE` paths; otherwise the invoked
`spotter` entry point wins over ambient `PATH`, and `spotterd` is derived beside it. Stable symlink
spelling is preserved rather than resolved into a versioned package directory.

The implemented compatibility layout is:

```text
PACKAGE_OWNED
  <stable-bin>/spotter
  <stable-bin>/spotterd
  <versioned-package>/spotter/*

INTEGRATION_MANAGED
~/.spotter/
  integrations/
    codex.json
    codex.lock

RUNTIME_EPHEMERAL
~/.spotter/runtime/
  spotterd.sock
  spotterd.lock

USER_DURABLE
~/.spotter/
  spotter.toml
  sessions/
  labels/
  opportunities/
  experiments/
  logs/
  repos.json
```

`SPOTTER_HOME` relocates all mutable Spotter-owned roots together for compatibility. The logical
properties remain distinct so a future platform-native config/data migration can be explicit rather
than inferred. No mutable path is derived from `package_assets_dir`, `sys.prefix`, or a Homebrew
prefix.

The current daemon control socket is `~/.spotter/runtime/spotterd.sock`. When a configured
`SPOTTER_HOME` would exceed the platform UNIX-socket path limit, Spotter uses a short, private,
user-and-home-specific runtime directory under `/tmp`. The socket handshake is the liveness source;
the adjacent lock only serializes ownership.

Persistent Hook and service commands require absolute stable entry points and reject a path with a
`Cellar` component. Generated Hooks call the packaged `spotter hook` bridge, carry an immutable
integration generation derived from build identity and layout, guard a missing executable, and end
with fail-open behavior. The manifest records that generation, exact package build, and stable
layout references, but never the versioned package-assets directory. The daemon handshake
independently reports its package build ID, so an old running process cannot masquerade as the newly
installed package merely because both use the same stable service path.

Repository/Git-owned Spotter resources may include:

```text
refs/spotter/...
Spotter-created detached worktrees
```

That is why uninstall and data purge are separate lifecycle operations. See [Lifecycle](lifecycle.md).

---

# 13. Snapshots, replay, and side effects

## Snapshots

Preserve current safety rules:

- do not modify user HEAD/index;
- use Spotter-owned Git refs;
- restore into detached worktrees;
- clean up through Git-aware operations.

## Replay / fork

These serve two different purposes:

1. recovery research;
2. same-prefix counterfactual evaluation.

Minimum lineage metadata:

```text
parent_thread
branch_point
snapshot
forked_rollout
experiment_id
control/guidance arm
```

## External side effects

A local Git snapshot cannot undo:

```text
git push
cloud deploy
database write
GitHub issue/PR creation
arbitrary MCP external write
```

Before `RESTART` becomes a serious recovery primitive, Spotter needs a side-effect ledger:

```text
Effect
  kind
  target/resource
  result
  timestamp
  reversible?
  compensation/checkpoint if known
```

A reasoning restart must never imply that external state was automatically rolled back.

The current Hook path performs a bounded, deterministic classification before execution. It
recognizes explicit Git, GitHub CLI, Kubernetes CLI, Terraform, HTTP method, and bounded database
metadata/write operations, unwraps a small set of common shell wrappers, and combines compound
commands by their strongest effect. Remote HTTP resources drop credentials, queries, and fragments
before persistence. Each result keeps classifier, semantic-operation, reason, resource, and
confidence provenance. Unsupported subcommands, scripts, SQL, malformed shell, and over-deep
wrappers remain explicitly unknown while mapping to conservative Class C behavior. Configured MCP
semantics are keyed by exact server/tool identity and cannot be promoted by model-authored
descriptions.

Hook Class C proposals remain proposal evidence rather than proof that the external world changed.
An App Server item start with the same bounded classification creates an `attempted` effect
observation; a terminal result enriches that logical effect through its native operation identity.
Terminal outcomes remain `failed`, `partial`, `succeeded`, or `unknown` according to explicit
protocol evidence, so process exit zero alone does not prove a remote mutation committed. Duplicate
Hook/App Server observations merge without discarding conflicts, and later reversal, compensation,
or reconciliation is appended as separate history instead of rewriting the original effect.

---

# 14. Agent adapter contract

Codex is the first adapter, not the permanent core API.

Conceptual interface:

```text
AgentAdapter
  connect()/disconnect()
  list_threads()
  observe_events()
  read_thread_state()

  optional:
    steer(turn, message)
    interrupt(turn)
    pre_action_veto(proposal)
    fork/resume primitives
```

Every adapter exposes capabilities explicitly.

```text
CodexAdapter
  observation: available after thread query probe
  steer: unknown until a successful call; unavailable on method-not-found
  interrupt: unknown until a successful call; unavailable on method-not-found
  pre-action veto: unavailable through App Server; Hook boundary remains

FutureAgentAdapter
  observation: maybe partial
  steer: maybe unavailable
  veto: maybe unavailable
```

When a capability is missing, Spotter should hide/disable the feature or report degraded mode. Codex method names should not leak into core policy interfaces.

---

# 15. Runtime gate result: App Server lifecycle / attach PoC

The experiment proved the control premise for Path B. Its harness and recorded result live in
[App Server lifecycle / attach PoC](app-server-poc.md).

## Path A — Explicit remote TUI

```text
start/ensure external Codex App Server
      ↓
codex --remote <endpoint>
      ↓
TUI attaches to selected external server
      ↓
Spotter attaches as second client
      ↓
observe same thread/turn
      ↓
turn/steer reaches TUI
```

Plain `codex` does not auto-discover the separately started server in the current
documented/source interface. See [App Server connection validation](app-server-validation.md).

## Path B — Spotter-managed App Server

```text
Spotter starts external `codex app-server`
      ↓
TUI + Spotter attach same endpoint
      ↓
can normal plain-codex UX still be preserved?
```

## Path C — Embedded baseline

Measure the real fallback capability set:

```text
Observation: limited/unavailable
Live steer:  unavailable
Interrupt:   unavailable
PreToolUse:  possibly available
```

## Result and remaining lifecycle work

- Path B proved a shared App Server/thread, live event delivery, and real `turn/steer` delivery.
- `CodexAppServerClient` provides the initialized transport, raw events, thread/control methods, and
  explicit per-capability degradation used by later runtime components.
- Path A remains unavailable for the tested Homebrew Cask because the managed daemon expects the
  standalone installer layout.
- The concurrent thread identity registry from #81 is consumed by normalized App Server event
  routing. The daemon connection loop reconciles identities and live state across reconnect epochs.
- Transactional Codex setup now owns only its recorded Hook/plugin/service mutations. App Server
  endpoint selection remains explicitly pending; setup neither owns nor stops a shared App Server.

---

# 16. Current prototype guarantees to preserve

The migration should not throw away hardening already earned by real use.

### Journal

- cross-process serialization;
- monotonic step/proposal allocation;
- fsync durability;
- torn-tail recovery;
- strict reads for destructive cleanup.

### Snapshot

- user HEAD/index untouched;
- detached restore;
- Spotter-owned refs;
- deduplication and conservative prune.

### Audit ledger

- Main summaries are not promoted to evidence;
- only observable outcomes become evidence;
- contradictory outcomes retract prior evidence;
- stale propagation is transitive.

### Gate

- shell-aware bounded parsing;
- ambiguous/unsupported cases fail open;
- blind spots are measured separately from false positives.

The hook-era corpus exposed directly usable outcomes in only 33 of 340 real tool results (10%). That is a measurement of the **current observation surface**, not a permanent architectural ceiling. The Observe stage must re-measure it after App Server migration.

---

# 17. Non-goals for the first migration

Do not turn the architecture migration into all of these at once:

- rewrite all of Spotter in Rust/Go;
- introduce a graph database;
- automatic compensating rollback;
- learned/adaptive intervention policy;
- support every coding agent immediately;
- finish `RESTART`;
- claim that reviewer interventions improve task outcomes.

The first runtime success criterion is narrower:

> **Observe the same real Codex thread, maintain live independent state, keep deterministic enforcement fast, and deliver an asynchronous reviewer decision safely to the correct active turn.**

For installation/upgrade/removal details, see [Lifecycle](lifecycle.md). For implementation order, see [Roadmap](roadmap.md). For a project dashboard, see [Status](status.md).
