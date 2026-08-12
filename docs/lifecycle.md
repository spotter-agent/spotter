# Lifecycle and Operations

> **Status:** target design, tracked by [#66](https://github.com/Bogyie/spotter/issues/66).  
> The current prototype is still hook/plugin-centered. This document defines the lifecycle Spotter should converge on; it is not a claim that every command or runtime component described here ships today.

Spotter is moving from an agent plugin that runs work inside individual hooks to a standalone local supervision runtime. That change is only useful if the entire product lifecycle is coherent: install, integration setup, normal use, session resume, crashes, upgrades, teardown, uninstall, purge, and reinstall.

This document defines that lifecycle and the ownership rules behind it.

## 1. Lifecycle at a glance

From the user's point of view:

```text
UNINSTALLED
    │
    │ brew install spotter
    ▼
INSTALLED
    │
    │ spotter setup codex
    ▼
SETTING_UP
    │
    ├─ detect / compatibility check
    ├─ migrate legacy integration
    ├─ prepare Spotter runtime/service
    ├─ ensure a usable Codex App Server path
    ├─ install the minimum required hook surface
    └─ verify end-to-end health
    │
    ▼
READY
    │
    │ codex
    ▼
ACTIVE
    │
    ├─ observe
    ├─ update live state
    ├─ detect
    ├─ review asynchronously
    ├─ steer / block when justified
    └─ persist durable history
    │
    ├───────────────┐
    │               │
Codex exits     Spotter/Codex upgrade
    │               │
    ▼               ▼
READY           MIGRATING
                    │
                    └──────────► READY

READY
  │
  │ spotter teardown codex
  ▼
INSTALLED
  │
  │ brew uninstall spotter
  ▼
UNINSTALLED

User data remains until an explicit purge policy removes it.
```

Internally this is not one lifecycle. It is the composition of several independent ones:

```text
Spotter package
Spotter agent integration
spotterd
Codex App Server
Codex Thread
Codex Turn
Reviewer Job
Journal / Snapshot / Fork Worktree
```

Each must have explicit ownership, start/stop semantics, and recovery rules.

## 2. Distribution and release lifecycle

The package lifecycle starts before installation:

```text
source
  ↓
version
  ↓
test
  ↓
build/package
  ↓
release artifacts
  ↓
Homebrew formula
```

The project should version independently evolving contracts rather than treating the package version as sufficient:

```text
spotter_version
ipc_protocol_version
config_schema_version
journal_schema_version
label_schema_version
experiment_schema_version
integration_manifest_version
```

This is necessary because upgrades can temporarily produce combinations such as a new CLI talking to an older running daemon or a new reader opening old journals.

### Required implementation

- release automation for supported macOS/Linux artifacts
- stable package-manager paths for `spotter`, `spotterd`, and `spotter-hook`
- protocol/version handshakes
- explicit reader compatibility ranges
- schema migration rules
- refusal rather than guessing when a newer incompatible schema is encountered

Do not embed Homebrew Cellar version paths in hooks or service definitions. Use stable `bin`/`opt` paths so upgrades do not leave dead integrations.

## 3. Installation

Target primary installation:

```bash
brew install spotter
```

Installation should place the Spotter binaries/runtime on the machine but should not silently mutate Codex, Claude Code, or other agent configuration.

Installation and integration setup are intentionally separate operations:

```text
brew install spotter     = package operation
spotter setup codex      = integration operation
```

This separation keeps both upgrade and uninstall behavior predictable.

Target installed entry points:

```text
spotter       user CLI / control plane
spotterd      long-lived supervision runtime
spotter-hook  minimal synchronous hook bridge, if still required
```

The current Python package remains the implementation substrate during the migration; a native hook client is an optimization, not a prerequisite.

## 4. First-time agent setup

Target command:

```bash
spotter setup codex
```

Setup is the integration installer. It should be idempotent and transactional.

Recommended flow:

```text
inspect
  ↓
plan
  ↓
backup
  ↓
apply
  ↓
verify
  ↓
commit integration manifest
```

A partial failure must not leave duplicate hooks, broken Codex configuration, or a service definition that points at missing binaries.

### Setup responsibilities

1. Detect OS, Spotter version, Codex path/version/install type.
2. Detect current and legacy Spotter integrations.
3. Probe required Codex capabilities instead of relying only on a minimum version number.
4. Choose the verified App Server integration strategy.
5. Install/register the Spotter user runtime if the selected mode requires it.
6. Install only the minimum hook surface that remains necessary.
7. Preserve user configuration and create recoverable backups where appropriate.
8. Run an end-to-end synthetic health check.
9. Persist an integration manifest describing exactly what Spotter changed.

### Integration manifest

Spotter needs durable knowledge of its own mutations. Conceptually:

```text
~/.config/spotter/integrations/codex.json
```

The manifest should record at least:

```json
{
  "schema": 1,
  "setup_by": "0.x.y",
  "agent": "codex",
  "agent_path": "...",
  "agent_version": "...",
  "app_server_strategy": "...",
  "hooks_added": ["PreToolUse"],
  "service_installed": true,
  "previous_config_fingerprint": "..."
}
```

This is what makes `setup`, `repair`, and `teardown` safe and idempotent.

## 5. The App Server prerequisite

The target Codex architecture requires Spotter to observe and control the **same App Server** used by the user's TUI session.

A plain Codex TUI can use an embedded App Server when no reusable external daemon is available. Once that choice has been made, waking Spotter later at the first tool hook is too late to retroactively create an attachable control plane.

Therefore the first architecture PoC in #66 must choose and validate a canonical external App Server strategy.

Candidate paths:

### A. Reuse Codex-managed App Server daemon

```text
Codex App Server daemon already/automatically available
        ↓
plain `codex` attaches
        ↓
Spotter attaches as a second client
```

### B. Spotter ensures an external App Server process

```text
Spotter runtime
    ↓
external App Server ready
    ↓
Codex TUI and Spotter both attach
```

### C. Embedded/degraded mode

If Spotter cannot acquire the observation/control channel, it must expose that state explicitly instead of looking merely quiet:

```text
Observation:       unavailable/limited
Live NUDGE:        unavailable
INTERRUPT:         unavailable
PreToolUse gate:   available or unavailable independently
```

### Ownership rule

An App Server may be shared by consumers other than Spotter.

```text
pre-existing App Server
    → attach
    → never stop it merely because Spotter stops

App Server started during Spotter ensure
    → record provenance
    → do not automatically couple its lifetime to spotterd exit
    → stop only under an explicit, safe cleanup policy
```

`spotter daemon stop` means “stop Spotter”, not “kill Codex infrastructure”.

## 6. User-login / background-service lifecycle

The original daemon idea was fully lazy: start at the first hook and exit when idle. That conflicts with the target full Codex mode if an external App Server must exist **before** the user types `codex`.

The requirement should be stated independently of mechanism:

> In managed mode, whatever runtime is required for full Spotter observation/control must be ready before an ordinary `codex` invocation chooses its App Server target.

If the App Server PoC confirms that requirement, the likely default is a login-scoped lightweight service:

```text
user login
   ↓
spotterd ready
   ↓
ensure external App Server path
   ↓
user runs `codex` at any later time
```

Possible implementations:

- macOS: `launchd`
- Linux: `systemd --user`

A portable mode may intentionally trade capability for zero persistent service installation:

```bash
spotter setup codex --portable
```

Portable mode must report the capabilities it cannot guarantee.

## 7. Normal runtime lifecycle

The hot path should be App Server-driven, not hook-driven:

```text
Codex App Server event
        │
        ▼
     spotterd
        │
        ├─ normalize to Trace IR
        ├─ update live state
        ├─ append durable journal
        └─ run cheap signals
                  │
             suspicious?
             ├─ no
             └─ yes
                  ↓
            Reviewer Job
```

The journal becomes the durable history and recovery source. It should not be reparsed on every event merely to reconstruct state that the daemon already owns in memory.

### Primary App Server observations

Where available, Spotter should prefer App Server data for:

- thread / turn lifecycle
- user messages
- plan updates
- reasoning summaries exposed by the runtime
- command execution start/completion
- tool results and exit status
- file changes / diffs
- MCP calls
- web search
- token usage

The current 10% observed-outcome ceiling was measured on hook-collected data and must be re-measured after the App Server migration.

## 8. Minimal synchronous hook lifecycle

Codex currently still benefits from one hook class: an execution-before-commit boundary for deterministic policy.

Target steady state:

```text
PreToolUse
    │
    ▼
spotter-hook
    │ IPC
    ▼
spotterd
    │
Gate Engine
    ├─ ALLOW
    └─ DENY
```

Only bounded deterministic checks belong here:

- destructive command policy
- forbidden paths
- dependency policy
- workspace escape
- similarly auditable rules

Do not put semantic LLM review, large journal replay, full-state reconstruction, or metrics calculations on this synchronous path.

Target hook reduction:

| Hook | Target |
| --- | --- |
| `SessionStart` | replace with App Server lifecycle if coverage is sufficient |
| `UserPromptSubmit` | replace with App Server user-message events |
| `PreToolUse` | retain for atomic deterministic blocking |
| `PostToolUse` | replace with App Server result/diff events |

If Codex later exposes a reliable atomic veto primitive through the App Server, a zero-hook integration becomes worth reconsidering.

## 9. Thread, turn, and attachment lifecycle

“Session” is too overloaded for the target runtime. Spotter should distinguish at least:

```text
Agent Thread
  └─ Turn
       └─ Events

Runtime Attachment
  = one client/runtime attachment to that thread
```

A thread may continue across multiple TUI launches:

```text
Thread A
├─ attachment #1
│  ├─ turn 1
│  └─ turn 2
└─ attachment #2 after resume
   ├─ turn 3
   └─ turn 4
```

Audit state naturally belongs primarily to the thread. Some operational metrics belong to an attachment/run.

The internal identity model should therefore preserve:

```text
agent_thread_id
spotter_thread_id (if needed)
runtime_attachment_id
turn_id
```

At first attachment Spotter should journal provenance such as Spotter version, agent/App Server versions, repository, config fingerprint, reviewer configuration, and observed capabilities.

## 10. Semantic review and intervention lifecycle

Semantic supervision runs asynchronously. Main should continue while Spotter thinks.

```text
signal
  ↓
Reviewer Job
  ↓
QUEUED → RUNNING → DECIDED
                    │
                    ├─ DELIVERED
                    ├─ STALE
                    ├─ CANCELLED
                    └─ FAILED
```

Every reviewer job is bound to the thread/turn that motivated it.

If the target turn is still active when the verdict arrives:

```text
VERIFY / NUDGE → turn/steer
critical case  → later turn/interrupt policy
```

If the target turn has already ended, the verdict must pass an explicit stale policy rather than being blindly injected into a different turn.

Track at least:

- signal time/step
- reviewer start/end time
- target thread/turn
- delivery attempt
- delivered/stale/discarded state
- intervention latency

This makes stale rate and actual supervision latency measurable.

## 11. Turn completion, TUI exit, and resume

### Turn completion

On turn completion:

- clear/update active-turn state
- finalize turn metrics
- update validation state
- mark late reviewer jobs stale or defer them according to policy

The thread remains alive.

### TUI exit

TUI exit closes an attachment, not the durable thread model:

```text
attachment closed
      ↓
thread becomes dormant
      ↓
journal/audit state remains
```

### Resume

On resume:

```text
existing agent thread
      ↓
find Spotter durable history
      ↓
hydrate live state
      ↓
reconcile with App Server thread state
      ↓
continue
```

Journal replay belongs at recovery/resume boundaries, not on every event.

## 12. Fork and experiment lifecycle

Spotter already uses detached Git worktrees and forked continuations for counterfactual experiments. In the target runtime these remain first-class managed resources.

Conceptual lifecycle:

```text
CREATED
  ↓
RUNNING
  ↓
COMPLETE / FAILED
  ↓
CLEANED
```

Lineage should remain explicit:

```text
parent_thread
branch_step / branch_turn
snapshot
control/guidance arm
experiment_id
```

Cleanup must use Git-aware worktree/ref operations rather than deleting directories blindly.

## 13. Crash and degraded-mode lifecycle

### spotterd crash

In managed mode the service manager should be able to restart the runtime:

```text
spotterd crash
    ↓
service restart
    ↓
reconnect App Server
    ↓
list/reconcile active threads
    ↓
hydrate from journal where needed
    ↓
READY
```

During daemon unavailability the synchronous gate must retain the project's fail-open posture unless an explicit stronger policy is introduced later.

### App Server disconnect/crash

Observation/control has its own state machine:

```text
CONNECTED
   ↓
DEGRADED
   ↓
RECONNECTING
   ↓
CONNECTED or UNAVAILABLE
```

A healthy `spotterd` with no App Server control plane must not be reported as fully healthy.

Example status:

```text
Spotter daemon:       running
Codex App Server:     unavailable
Observation:          unavailable
Live intervention:    unavailable
PreToolUse gate:      active
```

## 14. Status, doctor, and repair

`spotter status` is the concise runtime view. `spotter doctor` is the diagnostic view.

Target doctor surface:

```text
Spotter
  ✓ binary/version
  ✓ config
  ✓ storage permissions
  ✓ daemon/service
  ✓ IPC

Codex
  ✓ installed
  ✓ capability-compatible
  ✓ external App Server path
  ✓ Spotter attached
  ✓ event stream
  ✓ turn/steer
  ✓ turn/interrupt (when required)
  ✓ PreToolUse gate

Data
  ✓ journal writable
  ✓ schemas supported
  ✓ repository/snapshot state

Integration
  ✓ manifest consistent
  ✓ no duplicate hooks
  ✓ stable executable paths
```

A future `spotter doctor --repair` can repair safe, well-understood drift. Destructive repair should require explicit user intent.

## 15. Configuration lifecycle

A reasonable target precedence is:

```text
runtime override
    > repository config
    > user/global config
    > defaults
```

Conceptual locations:

```text
~/.config/spotter/config.toml
<repo>/spotter.toml
CLI/runtime overrides
```

Configuration fields should declare whether they support hot reload or require runtime/integration restart.

Examples:

```text
reviewer model/cadence     potentially hot-reloadable
gate rules                 potentially hot-reloadable
socket/service strategy    restart required
agent integration strategy setup/migration required
```

## 16. Codex upgrade lifecycle

Codex and Spotter have independent release cycles. Spotter should negotiate capabilities rather than assuming one global compatible version.

After a Codex upgrade, independently assess features such as:

```text
thread/turn events
command/result visibility
diff visibility
turn/steer
turn/interrupt
hook veto support
```

This allows explicit degraded modes:

```text
Observation:  available
Steer:        available
Interrupt:    unsupported
Gate:         available
```

The experimental Codex App Server daemon lifecycle must remain behind a `CodexAppServerManager` abstraction so Spotter can change strategy without rewriting the supervision runtime.

## 17. Spotter upgrade lifecycle

Target package update remains package-manager-owned:

```bash
brew upgrade spotter
```

A running daemon may still be the previous binary image after the package files change. The control plane therefore needs version negotiation and graceful restart:

```text
new CLI/hook installed
      ↓
connect old spotterd
      ↓
version/protocol check
      ↓
compatible → continue
incompatible → graceful daemon restart
      ↓
schema migration if required
      ↓
App Server reconnect
      ↓
READY
```

`spotter update` should not overwrite Homebrew-owned files. It can check/report availability or delegate to the package manager.

## 18. Schema migration lifecycle

For journals, labels, experiments, config, and integration manifests:

- prefer read-old/write-new compatibility where reasonable
- migrations must be explicit and versioned
- destructive migrations should follow backup → migrate → verify → replace
- older readers encountering unknown newer formats should refuse rather than silently reinterpret them
- mixed-version durable data needs tests

## 19. Teardown, uninstall, purge, and reinstall

These are intentionally separate operations.

### Agent teardown

```bash
spotter teardown codex
```

Teardown should:

- read the integration manifest
- detach Spotter from Codex observation/control
- remove only hooks/config changes that Spotter owns
- restore preserved agent configuration when appropriate
- remove the integration manifest
- leave Spotter itself installed
- not stop a shared Codex App Server merely because Spotter detached

Rule:

> Never delete configuration Spotter did not create or explicitly own.

### Package uninstall

```bash
brew uninstall spotter
```

Uninstall removes binaries/package state. It should not silently delete durable user research/history data.

The integration must be resilient even if the user uninstalls without running `teardown` first: a dangling Spotter hook must fail open rather than breaking Codex.

### Purge

An explicit purge handles durable Spotter-owned data/resources:

```bash
spotter purge --data
spotter purge --snapshots
spotter purge --all
```

Purge may need to clean resources outside `~/.spotter`, including:

- `refs/spotter/*`
- detached fork worktrees / Git worktree metadata
- repository-specific state

This implies a repository registry or equivalent provenance store that records which repositories Spotter has touched.

### Reinstall

On reinstall/setup:

```text
existing Spotter data found
       ↓
schema compatible?
  ├─ yes → reuse
  └─ no  → migrate or clearly refuse
```

Setup must remain idempotent and repairable.

## 20. Legacy plugin migration

The current plugin installation is a real migration path, not a hypothetical one.

Target transition:

```text
old
Codex plugin hooks
├─ SessionStart
├─ UserPromptSubmit
├─ PreToolUse
└─ PostToolUse

        ↓ spotter setup codex

new
External App Server ↔ spotterd
PreToolUse only (while atomic blocking still needs it)
```

Migration must detect and remove/replace the legacy integration without double-recording events. Existing `~/.spotter` journals, labels, and experiment data should remain usable whenever their schemas are supported.

## 21. Required components

The lifecycle implies a concrete component set:

| Component | Responsibility |
| --- | --- |
| `spotter` | user CLI / control plane |
| `spotterd` | long-lived supervision runtime |
| `spotter-hook` | minimal synchronous `PreToolUse` bridge |
| `ServiceManager` | login/user service lifecycle |
| `CodexIntegration` | setup/teardown/capability negotiation |
| `CodexAppServerManager` | discover/ensure/attach App Server strategy |
| `AppServerClient` | event stream + steer/interrupt control |
| `SessionManager` | thread/turn/attachment lifecycle |
| `GateEngine` | synchronous deterministic policy |
| `SignalEngine` | cheap event-driven candidates |
| `ReviewerScheduler` | asynchronous semantic review |
| `InterventionController` | freshness, steer, interrupt policy |
| `JournalStore` | durable event log |
| `MigrationManager` | config/schema/integration migration |
| `IntegrationManifest` | record what Spotter owns/changed |
| `Doctor` | diagnose and safely repair drift |
| `RepositoryRegistry` | track repos with Spotter resources |
| `RetentionManager` | journals, snapshots, forks, logs |

These are responsibility boundaries, not a requirement to create one module/class per row immediately.

## 22. Measurements required across the lifecycle

Architecture migration must be measured, not only described:

- App Server event coverage
- observable tool-result rate
- hook invocations per session
- hook latency p50/p95/p99
- daemon CPU/memory while idle and active
- journal write overhead
- reviewer dispatch latency
- intervention latency
- stale intervention rate
- reconnect/recovery latency
- upgrade/migration failure rate in fixtures
- storage growth and retention behavior

## 23. Implementation sequence

The lifecycle suggests this order:

### P0 — App Server lifecycle PoC

```text
external App Server
→ plain codex auto-attach
→ Spotter second client
→ event stream
→ active turn identity
→ turn/steer
```

If this fails, revisit the architecture before building more runtime semantics.

### P1 — Runtime foundation

- `spotterd`
- service/lifecycle abstraction
- App Server manager/client
- session/thread/turn model
- IPC

### P2 — Product lifecycle

- package/distribution path
- `setup` / integration manifest
- `doctor` / `status`
- `teardown`
- migration framework

### P3 — Move existing capabilities into the runtime

- journal
- audit state
- deterministic gates
- snapshots/forks
- reviewer
- metrics/labels/experiment

### P4 — Make App Server primary and minimize hooks

- migrate observation to App Server
- re-measure observability
- remove `SessionStart`
- remove `UserPromptSubmit`
- remove `PostToolUse`
- keep only bounded `PreToolUse` enforcement if required

### P5 — Complete asynchronous supervision

- event-driven signals
- asynchronous reviewer scheduling
- intervention freshness/staleness
- `turn/steer`
- later `turn/interrupt`

### P6 — Operational hardening

- crash/reconnect recovery
- upgrade/schema migration
- retention/purge
- reinstall
- multi-agent integration lifecycle

## 24. Non-goals for the first migration

- rewriting the whole runtime in Rust/Go
- requiring a native hook client before the daemon design is validated
- Windows support in the first App Server/daemon integration
- solving compensating rollback for arbitrary external effects
- implementing full automatic `RESTART` as part of the architecture migration
- claiming positive intervention value before the evaluation task set and experiments produce evidence
