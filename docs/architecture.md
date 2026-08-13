# Architecture

> **Status:** this document describes both the current hook-based prototype and the target architecture. Active implementation is tracked by the [Roadmap](roadmap.md) and native GitHub Milestones.
> The `spotterd` process/control foundation, bounded Hook gate IPC, shared-server PoC, production App Server transport, and durable Trace IR ingestion are implemented. Daemon event routing and live supervision delivery remain **target behavior**.

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
protocol over a per-user UNIX socket. `spotter daemon start|stop|restart|status` exercises a real
handshake rather than treating a PID file as liveness. The server serializes ownership, accepts
concurrent clients, reports `healthy` / `degraded` / `recovering`, and makes absence explicit as
`unavailable`.

Manual process management implements the `ServiceManager` boundary used by the CLI. Managed
`launchd` / `systemd --user` registration belongs to setup lifecycle work. This daemon currently
owns no App Server, semantic `ThreadState`, Hook IPC, or event routing, so stopping it cannot stop a
shared Codex App Server and existing Hook behavior remains independent.

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
| `SessionStart` | session bootstrap/observation | replace with App Server lifecycle if coverage is sufficient |
| `UserPromptSubmit` | user goal capture | replace with App Server user-message events |
| `PreToolUse` | proposal observation + gate | **retain for deterministic atomic enforcement** |
| `PostToolUse` | result/snapshot/reviewer cadence | replace with App Server result/diff events |

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
validation relation
side-effect class
```

Normalization must not discard provenance. Live control, replay, and causal analysis must be able to reconnect normalized records to the underlying runtime item/turn.

## Implemented App Server ingestion

`CodexTraceNormalizer` is the transport boundary. It maps App Server lifecycle and authoritative
completed-item records into the runtime-neutral fields above. Raw reasoning content is not copied;
only the App Server's exposed reasoning summary enters Trace IR. Unknown notification methods become
`runtime_event_unknown` records containing the method and any proven identity, rather than silently
disappearing or leaking a new wire shape into core consumers.

`AppServerTraceIngestor` writes one journal per Spotter thread identity, separately named from
Hook-era session journals. Its recovery scan rebuilds deduplication and lifecycle state after a
restart. Item starts and outcomes correlate by App Server item ID, not journal adjacency. A terminal
event seen before its start is retained with `observed_start=false`; a later start cannot regress it.
Timestamp regressions are accepted but marked `out_of_order`, and conflicting terminal outcomes fail
explicitly. Hook records carry legacy-session provenance with unknown thread, turn, and attachment
dimensions, so consumers never need Codex transport objects and never infer that a Hook session is an
App Server thread.

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

The registry owns identity and lifecycle only; App Server Trace normalization and journal recovery
consume it without promoting it into semantic state. Semantic `ThreadState` remains #31, and daemon
connection reconciliation remains #87.

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
Until #85 wires a persistent runtime consumer, thread counts and reviewer queue state are reported
as unknown/unavailable instead of being derived from incompatible Hook-era sessions.

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
CONNECTED
  ↓
DEGRADED
  ↓
RECONNECTING
  ├─ CONNECTED
  └─ UNAVAILABLE
```

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

Exact platform paths are implementation details, but ownership should be explicit from the start.

Conceptual layout:

```text
~/.config/spotter/
  config.toml
  integrations/
    codex.json

~/.local/state/spotter/     # use platform-appropriate state directory
  sessions/
  labels/
  experiments/
  logs/
  runtime/
    spotter.sock
    daemon metadata
  repos.json
```

The current prototype uses `~/.spotter`; migration must preserve compatibility or provide an explicit data move.

The current daemon control socket is `~/.spotter/runtime/spotterd.sock`. When a configured
`SPOTTER_HOME` would exceed the platform UNIX-socket path limit, Spotter uses a short, private,
user-and-home-specific runtime directory under `/tmp`. The socket handshake is the liveness source;
the adjacent lock only serializes ownership.

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
- A provisional concurrent thread identity registry exists from #81 but is not wired into production;
  App Server event routing in #85 must validate it, while reconnect reconciliation remains #87.
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

The hook-era corpus exposed directly usable outcomes in only 33 of 340 real tool results (10%). That is a measurement of the **current observation surface**, not a permanent architectural ceiling. P4 must re-measure it after App Server migration.

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
