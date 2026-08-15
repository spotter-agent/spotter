# Lifecycle and Operations

> **Status:** primarily a target contract with an implemented transactional Codex setup slice. For
> commands that work today, see [Connect Spotter to Codex](../README.md#connect-spotter-to-codex);
> for current implementation state, see [Status](status.md). `spotter setup|teardown codex`,
> versioned ownership
> manifests, managed `launchd`/`systemd --user` registration, portable startup, legacy Hook/plugin
> migration, runtime-aware diagnostics, tag-derived release artifacts, stable packaged runtime
> layout, official Homebrew tap, packaged `--version` identity, and a two-generation Homebrew
> lifecycle gate exist. App Server event ingestion also exists, but broader configuration/protocol
> migration policy, `spotter purge`, and transparent plain-`codex` launch remain target behavior.

---

## 30-second target lifecycle

The packaged lifecycle is four operations. The qualified install command selects the official
`spotter-agent/homebrew-spotter` tap without trusting unrelated third-party Formulae:

```bash
# 1. Install the product
brew install spotter-agent/spotter/spotter

# 2. Integrate an agent once
spotter setup codex
spotter doctor

# 3. Use the agent normally
codex

# 4. Remove the integration/product
spotter teardown codex
brew uninstall spotter
```

The target contract reserves persistent-data removal for an explicit purge. This command is not
implemented yet:

```bash
spotter purge --all
```

Ownership is intentionally split:

```text
brew install / uninstall
  owns Spotter package files only

spotter setup / teardown
  owns agent integration changes only

spotterd
  owns live supervision state

Codex App Server
  may be shared Codex infrastructure, not Spotter-private state

journals / snapshots / labels / experiments
  are user data and survive a normal uninstall unless explicitly purged
```

The most important lifecycle constraint is this:

> **If full Codex mode requires an external App Server, it must be available and selected
> explicitly when the TUI starts (currently with `--remote`).**

That conflicts with a completely lazy “wake Spotter on the first Hook” design. #78 proved a
Spotter-managed external path, and #79 provides the daemon/control and `ServiceManager` foundation.
#83 must register the selected managed startup strategy before the lifecycle is finalized.

---

## Quick navigation

| Task | Command / section |
| --- | --- |
| Package installation | `brew install spotter-agent/spotter/spotter` → [3. Install](#3-install-package-only) |
| Connect Codex | `spotter setup codex` → [4. Setup](#4-spotter-setup-codex) |
| Understand App Server ownership | [5. App Server lifecycle](#5-app-server-lifecycle) |
| Understand background service startup | [6. Managed runtime startup](#6-managed-runtime-startup) |
| Normal day-to-day execution | `codex` → [7. Normal runtime](#7-normal-runtime) |
| Resume/fork an existing thread | [8. Resume, fork, experiment](#8-resume-fork-and-experiment) |
| Check health | `spotter status`, `spotter doctor` → [9. Status / doctor / repair](#9-status-doctor-and-repair) |
| Recover from crashes | [10. Failure recovery](#10-failure-recovery) |
| Upgrade Codex | [11. Codex upgrade](#11-codex-upgrade) |
| Upgrade Spotter | `brew upgrade spotter` → [12. Spotter upgrade](#12-spotter-upgrade) |
| Change config | [13. Configuration lifecycle](#13-configuration-lifecycle) |
| Disconnect Codex | `spotter teardown codex` → [14. Teardown](#14-spotter-teardown-codex) |
| Uninstall Spotter | `brew uninstall spotter` → [15. Uninstall](#15-brew-uninstall-spotter) |
| Target data removal | `spotter purge --all` → [16. Purge](#16-purge) |
| Reinstall safely | [17. Reinstall](#17-reinstall) |
| Migrate current plugin users | [18. Legacy plugin migration](#18-legacy-plugin-migration) |

---

# 1. Lifecycle state model

## 1.1 User-visible states

```text
UNINSTALLED
    │
    │ brew install spotter-agent/spotter/spotter
    ▼
INSTALLED
    │ package exists
    │ no agent integration
    │
    │ spotter setup codex
    ▼
SETTING_UP
    │
    ├─ inspect environment
    ├─ plan mutations
    ├─ back up owned config fragments
    ├─ prepare runtime/service
    ├─ prepare App Server strategy
    ├─ install minimal Hook surface
    └─ verify end-to-end
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
    ├─ detect/review
    ├─ steer/block
    └─ persist history
    │
    ├──────────────┐
    │              │
TUI exits      update/failure
    │              │
    ▼              ▼
READY       MIGRATING / RECOVERING
                   │
                   └────► READY

READY
  │ spotter teardown codex
  ▼
INSTALLED
  │ brew uninstall spotter
  ▼
UNINSTALLED

DATA may remain until explicit purge
```

## 1.2 Internal lifecycles

The product composes several independent state machines:

| Resource | Example states |
| --- | --- |
| Spotter package | absent / installed / upgrading |
| Agent integration | absent / setting-up / ready / drifted / tearing-down |
| `spotterd` | stopped / starting / ready / degraded / recovering |
| Codex App Server | absent / embedded / external-ready / disconnected |
| Agent Thread | new / active / dormant / resumed / archived |
| Turn | started / active / completed / interrupted |
| Reviewer Job | queued / running / decided / delivered / stale / failed |
| Snapshot | created / referenced / expired / pruned |
| Fork Worktree | created / running / complete / orphaned / cleaned |

Never assume that stopping one resource automatically destroys another:

```text
spotterd stop
  ≠ Codex App Server stop
  ≠ journal delete
  ≠ snapshot delete
  ≠ integration teardown
```

---

# 2. Resource ownership

Safe upgrades and removal depend on Spotter knowing what it owns.

## 2.1 Package-owned resources

Installed by the package manager:

```text
spotter
spotterd
spotter hook  # minimal Hook bridge within the packaged CLI
package metadata
```

Homebrew owns these files.

The package manager exposes `spotter` and `spotterd` through stable entry points. For Homebrew these
are Formula-provided `opt_bin`/prefix-linked paths; versioned Cellar paths are implementation files,
not durable references. The packaged `spotter hook` subcommand is the minimal bridge. The legacy
repository plugin script remains source/plugin compatibility material and is not copied into the
Python release artifact or generated integration state.

## 2.2 Integration-owned resources

Implemented compatibility layout:

```text
~/.spotter/
  integrations/
    codex.json
    codex.lock
```

An `IntegrationManifest` records exactly which agent config fragments Spotter added, removed, or
migrated. Schema 4 also records the package build ID, an immutable integration-generation fence, and
stable runtime-layout references. It never records the versioned package-assets directory.

## 2.3 Runtime-owned resources

Implemented:

```text
~/.spotter/runtime/spotterd.sock
adjacent ownership lock
service registration metadata
runtime protocol/version metadata
connection state
```

A PID file may be useful for bookkeeping but should not be the liveness source of truth. A real IPC/socket handshake should be.

## 2.4 User data

Logical data categories:

```text
session journals
labels
signal-sampling frames
experiments
reviewer spend ledger
logs
repository registry
```

The compatibility root is `~/.spotter`, or `SPOTTER_HOME` when explicitly configured.
`RuntimeLayout` exposes config, data, integration, runtime, and log paths separately even where they
share that root. A future move to platform-native roots must preserve or explicitly migrate it.

## 2.5 Stable runtime layout contract

The package/runtime boundary supplies one central layout:

```text
RuntimeLayout
  cli_executable       stable current-package entry point
  daemon_executable    stable current-package entry point beside the CLI
  bridge_command       <cli_executable> hook
  package_assets_dir   immutable, version-specific package content
  user_config_dir      mutable user configuration
  user_data_dir        journals/evidence/experiments
  integration_dir      generated host ownership state
  runtime_dir          recreatable socket/lock state
  log_dir              mutable operational logs
```

Discovery preserves the invoked executable's path spelling instead of resolving symlinks. An
explicit package adapter path beats `PATH`, preventing an older installation on `PATH` from being
selected for a service. `SPOTTER_CLI_EXECUTABLE` and `SPOTTER_DAEMON_EXECUTABLE` are explicit
packaging-boundary overrides for launchers that cannot preserve invocation identity. Persistent
setup refuses missing entry points and paths containing a versioned `Cellar` component.

This contract is package-manager-neutral. Homebrew, pipx/uv, and source/editable installs may supply
different stable executables while using the same core layout interface.

## 2.6 Repository-owned Spotter resources

Spotter can leave resources outside its home directory:

```text
refs/spotter/...
Spotter-created detached worktrees
snapshot/fork lineage
```

That is why a true `purge --all` needs a repository registry and Git-aware cleanup.

---

# 3. Install: package only

Qualified command:

```bash
brew install spotter-agent/spotter/spotter
```

The `homebrew-spotter` repository name surfaces as the Homebrew tap `spotter-agent/spotter`; the
final `/spotter` names its Formula. Installation consumes the immutable sdist and SHA-256 from a
published Spotter tag, installs the declared Python runtime plus pinned Python resources, and exposes
both `spotter` and `spotterd`. Its `brew services` metadata runs `opt_bin/spotterd`, so the service
reference remains stable when Homebrew moves the active keg during an upgrade.

### Preconditions

- supported OS/architecture;
- supported package manager;
- no requirement that Codex is already installed.

### Actions

1. Install `spotter`.
2. Install `spotterd`.
3. Install `spotter-hook` if the target build still requires it.
4. Install package/version metadata.

### Must not happen during package install

Do **not** silently:

- mutate Codex configuration;
- register Hooks;
- register a background service;
- start a Codex App Server;
- delete or migrate user data.

Package installation and agent integration are separate transactions.

### Success check

```bash
spotter --version
spotterd --version
spotter status
```

Expected state:

```text
Spotter:      installed 0.x.y
Daemon:       not configured
Codex:        detected or not detected
Integration:  not configured
```

### Remaining implementation

- package provenance detection (`homebrew`, source, standalone, etc.);

Release artifacts, the dedicated tap and Formula, shared `spotter`/`spotterd` build identity, stable
runtime path discovery, and the package-vs-running-daemon build comparison are implemented. The
[Homebrew lifecycle smoke](homebrew-lifecycle-smoke.md) covers real macOS install, live upgrade,
uninstall, and reinstall while fast fixtures cover Intel/macOS and Linuxbrew path logic.

---

# 4. `spotter setup codex`

`setup` is the **integration installer**.

```bash
spotter setup codex
```

It must be **idempotent** and **transactional**.

The implemented command supports `--dry-run` and `--portable`. Managed mode registers `spotterd`
as a login-scoped user service; portable mode starts it without persistent registration. Setup
mutates only Codex Hook/plugin configuration, keeps fingerprinted backups, verifies daemon health
and a synthetic packaged-bridge round-trip, then atomically commits
`~/.spotter/integrations/codex.json`. Generated Hooks invoke the stable packaged CLI, not a copied
module tree or a persisted Python interpreter path.

## 4.1 Transaction stages

```text
INSPECT
   ↓
PLAN
   ↓
BACKUP
   ↓
APPLY
   ↓
START / CONNECT
   ↓
VERIFY
   ↓
COMMIT MANIFEST
```

## 4.2 INSPECT

Collect enough information to make a deterministic plan:

```text
OS / architecture
Spotter package version and install method
Codex binary path
Codex version
Codex install method
CODEX_HOME
existing App Server state
existing Codex Hook configuration
legacy Spotter plugin/integration
existing Spotter integration manifest
existing Spotter data/schema versions
```

Probe capabilities rather than relying on one version check:

```text
thread lifecycle events
user-message events
tool start/completion
tool result/exit status
diff/file-change events
token usage
turn/steer
turn/interrupt
PreToolUse veto
```

## 4.3 PLAN

Build an internal mutation plan before writing anything.

Example:

```text
Plan
  App Server strategy: codex-managed-external
  Runtime mode: managed/login-scoped
  Hooks:
    add SessionStart
    add UserPromptSubmit
    add PreToolUse
    add PostToolUse
  Legacy plugin: migrate
  Existing data: preserve
  Agent config backup/fingerprint: required
```

The mutation plan is available without writes:

```bash
spotter setup codex --dry-run
```

## 4.4 BACKUP

Before modifying an agent-owned file:

- record a content fingerprint;
- preserve the specific original fragment Spotter will replace;
- create a recoverable backup when the mutation API cannot be safely inverted.

Do **not** plan teardown as “restore the entire old file”. The user may legitimately edit the file after Spotter setup.

## 4.5 APPLY

The exact changes depend on the App Server strategy selected by the Runtime gate. Conceptually:

1. register/prepare the Spotter runtime service if managed mode requires it;
2. ensure the external App Server path or attach strategy;
3. prepare Spotter's App Server client connection;
4. register only the minimum required Hook surface;
5. remove legacy duplicate Spotter Hooks/plugin wiring;
6. create/update config/state directories with correct permissions.

## 4.6 START / CONNECT

Bring required runtime components to a testable state:

```text
spotterd ready
App Server reachable
Spotter ↔ App Server initialized
Hook IPC reachable
```

The current setup verifies Codex's `app-server --listen` and `--remote` capability but records
endpoint selection as pending because it does not create or own a shared App Server. When a verified
endpoint is present in the manifest, `spotterd` owns connection, epoch, backoff, and reconciliation;
`doctor` continues to probe the same explicit endpoint.

## 4.7 VERIFY

A synthetic E2E verification should check at least:

```text
spotterd handshake succeeds
App Server initialize succeeds
Spotter can subscribe/read required event surface
Hook round-trip succeeds
journal path is writable and private
protocol versions are compatible
schemas are readable
```

Once live steering is implemented, also verify the control capability without mutating user work unexpectedly.

## 4.8 COMMIT MANIFEST

Only after verification succeeds should the integration become `READY`.

Example manifest:

```json
{
  "schema": 4,
  "agent": "codex",
  "setup_by": "0.6.0",
  "setup_build_id": "v0.6.0@<commit>",
  "integration_generation": "<sha256>",
  "agent_path": "/opt/homebrew/bin/codex",
  "agent_version": "...",
  "app_server_strategy": "pending-external",
  "app_server_endpoint": null,
  "runtime_mode": "managed",
  "runtime_layout": {
    "cli_executable": "/opt/homebrew/opt/spotter/bin/spotter",
    "daemon_executable": "/opt/homebrew/opt/spotter/bin/spotterd",
    "bridge_command": ["/opt/homebrew/opt/spotter/bin/spotter", "hook"],
    "user_data_dir": "~/.spotter",
    "runtime_dir": "~/.spotter/runtime"
  },
  "service_owned": true,
  "owned_hooks": [
    {"event": "PreToolUse", "matcher": ".*", "hook": {"type": "command"}},
    {"event": "SessionStart", "matcher": null, "hook": {"type": "command"}},
    {"event": "UserPromptSubmit", "matcher": null, "hook": {"type": "command"}},
    {"event": "PostToolUse", "matcher": ".*", "hook": {"type": "command"}}
  ],
  "legacy_hooks_removed": [{"event": "PostToolUse"}],
  "config_fingerprint_before": "...",
  "created_at": "..."
}
```

## 4.9 Failure / rollback

| Failure stage | Expected behavior |
| --- | --- |
| INSPECT | no mutation |
| PLAN | no mutation |
| BACKUP | no mutation |
| APPLY | roll back Spotter-owned mutations already applied |
| START/CONNECT | either roll back or leave an explicit incomplete/degraded manifest |
| VERIFY | do not report READY; preserve actionable diagnostics |

A second `spotter setup codex` heals an interruption after inspectable changes were written. A
process crash before the manifest commit can leave the owned Hook without an ownership record;
re-running setup heals forward, while teardown cannot remove an unrecorded mutation automatically.

---

# 5. App Server lifecycle

Connection recovery is implemented; selecting and verifying the shared endpoint without claiming
ownership of another client's App Server remains the lifecycle dependency.

## 5.1 Why startup order matters

Plain `codex` does not auto-discover a separately started App Server in the current
documented/source interface. An external endpoint is selected explicitly with
`codex --remote <endpoint>`; without that option the external Spotter control plane is
not selected. See [App Server connection validation](app-server-validation.md).

```text
codex starts
   │
   ├─ reusable external/default daemon?
   │      ├─ yes → attach
   │      └─ no
   │
   └─ Embedded App Server
```

If Codex has already selected an embedded server, waking Spotter later at the first `PreToolUse` is too late to create an external sidecar observation/control plane for that turn.

## 5.2 Candidate canonical strategies

### Strategy A — explicit remote TUI

```text
ensure external Codex App Server
      ↓
`codex --remote <endpoint>` selects it
      ↓
Spotter attaches as client B
```

Advantages:

- uses Codex's documented external-TUI interface;
- makes the selected endpoint explicit and diagnosable.

Risks:

- App Server/WebSocket lifecycle remains experimental;
- preserving the plain-command UX requires a separately validated launcher/alias/wrapper;
- Spotter must not assume exclusive ownership.

### Strategy B — Spotter-managed App Server process

```text
Spotter starts external `codex app-server`
      ↓
TUI attaches to explicit/shared endpoint
      ↓
Spotter attaches to same endpoint
```

Advantages:

- lifecycle can be isolated behind `CodexAppServerManager`.

Risks:

- preserving plain `codex` UX may require wrapper/config/service work;
- version mismatch and process ownership become Spotter concerns.

### Strategy C — Embedded/degraded mode

If no external attachable server can be guaranteed, Spotter must explicitly report reduced capability:

```text
Observation:       limited/unavailable
Live NUDGE:        unavailable
INTERRUPT:         unavailable
PreToolUse gate:   independently available/unavailable
```

Silence must never mean both “nothing to report” and “Spotter is disconnected”.

## 5.3 Ownership rule

App Server may be shared with non-Spotter clients.

```text
pre-existing App Server
  → attach
  → never stop merely because Spotter exits

App Server started while Spotter ensures availability
  → record provenance
  → do not automatically couple lifetime to spotterd exit
  → stop only through an explicit safe cleanup policy
```

Therefore:

```bash
spotter daemon stop
```

means “stop Spotter”, **not** “kill shared Codex infrastructure”.

---

# 6. Managed runtime startup

The original daemon idea was fully lazy: start on the first Hook, exit when idle. That is attractive operationally but may be incompatible with full Codex App Server observation.

The requirement is mechanism-independent:

> **In managed mode, whatever runtime is required for full observation/control must be
> ready and explicitly selected before the Codex TUI starts.**

If the Runtime gate confirms that an external App Server must already exist, a likely default is a login-scoped user service:

```text
user login
   ↓
spotterd starts / becomes available
   ↓
ensure external App Server path
   ↓
user runs `codex` at any later time
```

Possible implementations:

- macOS: `launchd`;
- Linux: `systemd --user`.

Do not hard-code these mechanisms into Spotter core. Hide them behind a `ServiceManager` abstraction.

The implemented `ManagedServiceManager` receives its executable and mutable paths from
`RuntimeLayout`. Launchd and systemd definitions use the stable `spotterd` entry point, set an
explicit user-data working directory, carry only `SPOTTER_HOME`, write stdout/stderr to the layout's
log path, and contain no App Server attachment/session state. When the registered service is alive
but reports an older build ID, `start` restarts it even though the stable executable path and service
definition did not change.

The current manual escape hatch exercises that boundary without claiming managed setup:

```bash
spotter daemon start
spotter daemon status
spotter daemon restart
spotter daemon stop
```

These commands control only `spotterd`. They never stop or reset a shared Codex App Server.

A portable mode may trade capability for zero persistent service registration:

```bash
spotter setup codex --portable
```

Portable mode must display exactly which guarantees are lost.

---

# 7. Normal runtime

## 7.1 Codex launch

Target managed flow:

```text
external App Server already reachable
        │
user runs `codex`
        │
        ▼
TUI attaches to server
        │
Spotter sees/attaches same thread
        │
        ▼
RuntimeAttachment becomes ACTIVE
```

## 7.2 Thread initialization

For a newly observed thread, Spotter should create live state and a durable provenance header.

Suggested provenance:

```text
Spotter version
agent binary/version
App Server version/capabilities
repository/worktree
config fingerprint
reviewer model/config
runtime attachment id
start timestamp
```

## 7.3 Event processing

```text
App Server event
      ↓
normalize to Trace IR
      ↓
update ThreadState
      ├─ append durable journal
      └─ evaluate cheap signals
                 │
            candidate?
            ├─ no
            └─ yes → ReviewerJob
```

## 7.4 Deterministic gate

Only the bounded synchronous path uses Hook IPC:

```text
PreToolUse
  ↓
spotter-hook
  ↓
spotterd GateEngine
  ↓
ALLOW / DENY
```

No LLM call, broad repository scan, or journal replay belongs here.

## 7.5 Async reviewer

```text
Candidate at turn U7
  ↓
ReviewerJob QUEUED → RUNNING
  ↓
Main continues
  ↓
DECIDED
  ↓
U7 still active?
  ├─ yes → deliver VERIFY/NUDGE
  └─ no  → stale/defer/discard policy
```

Every delivery decision is journaled.

With `reviewer.on_signals = true`, `spotterd` executes the bounded immutable job input
asynchronously, records `review_inference_started` and a `reviewer_decision` (or visible cap/error),
and marks a result stale when its turn/epoch ended while the model was running. Reviews remain
shadow-only unless active mode and the separate `reviewer.deliver_on_signals = true` opt-in are set. In that mode,
fresh signal-driven `VERIFY`/`NUDGE` decisions issue one current-turn advisory through the exact
App Server attachment/turn/epoch. RPC acceptance, stale/failed/unknown outcomes, and later observed
input remain distinct durable states; ambiguous acceptance is never blindly retried. Both options
are off by default, and configured session/day ceilings are enforced before inference. A correlated
advisory input is tagged as Spotter supervision before live-state reduction, so it cannot replace the
user goal. Exact-target observation becomes `control_observed_in_turn`; the same intervention ID
appearing outside its target becomes an `expired_advisory_visible` diagnostic instead of a new goal.
If a completed App Server `agentMessage` is explicitly phased as `final_answer`, Spotter treats the
soft-intervention window as settled before `turn/completed`: queued/running reviews become stale and
new steers are rejected with `terminal_answer_settled`. Started messages, commentary, and messages
without a phase remain non-terminal rather than guessing across provider compatibility gaps.
An accepted steer with no correlated input stays pending. If its target then completes or settles a
final answer, Spotter durably closes it as `rpc_accepted_only`; acceptance and the terminal boundary
are reconciled in either arrival order. A later appearance is expired/outside-target evidence, not
proof that Main observed the advisory in time.
When Codex rejects `turn/steer`, its adapter also maps structured non-steerable data and the current
version-specific no-active-turn/expected-turn-mismatch messages into stable Spotter reason codes.
The supervision core never parses English RPC text; unknown rejections remain generic failures.

At journal append, each lifecycle record receives local receipt wall time plus a monotonic timestamp
and process clock-domain ID. Metrics correlate evidence, candidate, queue, start, and terminal events
by their stable IDs and derive sub-second latency only when both endpoints share that domain. A
daemon restart leaves the affected duration unknown with incomplete coverage rather than crossing
clocks.

## 7.6 Turn completion

On `turn/completed`:

- clear/update active-turn state;
- finalize per-turn counters;
- finalize validation state;
- re-evaluate jobs targeting that turn;
- mark late jobs stale if policy requires.

The Thread remains durable.

## 7.7 TUI exit

TUI exit closes a runtime attachment, not the underlying durable thread:

```text
attachment closed
      ↓
thread becomes dormant
      ↓
Spotter journal/audit state remains
```

---

# 8. Resume, fork, and experiment

## 8.1 Resume

```text
Codex resumes agent thread T1
      ↓
Spotter identifies durable T1 history
      ↓
hydrate missing live state from journal
      ↓
reconcile with App Server current state
      ↓
create new RuntimeAttachment
```

Journal replay is correct here because this is a recovery/reconstruction boundary.

## 8.2 Fork

A fork creates explicit lineage:

```text
parent_thread
branch point (turn/step/tool)
snapshot
forked rollout/thread
worktree
```

The implementation persists this lineage in a secure, atomically replaced fork manifest. It records
the source event and tool call, repository and snapshot identity, the exact rollout-prefix digest,
model/runtime metadata available in the rollout, external-effect warnings, observation gaps, and a
captured environment fingerprint. The fingerprint covers tracked/untracked Git status, submodule
status, Git/Python versions, and platform identity. A fork or experiment may additionally declare
relative non-secret files or directories with `--environment-resource`, and non-secret environment
variables with `--environment-variable`. Virtualenv or cache directories whose absence has distinct
meaning can be declared with `--environment-venv-or-cache`; Spotter does not infer their purpose
from path names. Spotter records only resource paths/purpose/types/Git state and content/tree hashes,
plus variable names, presence, and value hashes; raw variable values are never persisted.
Source-to-fork loss or value drift is classified before either arm runs and rechecked
immediately before continuation. A boolean records whether a declared value contains the current
worktree's absolute path, without persisting that value; a copied source path is classified as
`ABSOLUTE_PATH_MISMATCH`. Directory trees containing symbolic links are rejected instead of being
followed. Undeclared ignored resources, environment variables, and agent configuration remain
explicitly uncaptured limitations. Fork manifest schema v6 persists resource purpose so a missing
declared virtualenv/cache becomes `MISSING_VENV_OR_CACHE`; schema v1 through v5 manifests remain
readable, with v1 providing no declared-resource coverage.

With snapshotting enabled, Codex App Server `thread/started` pins a baseline snapshot for the
reported Git working directory. Completed local mutation items pin the resulting worktree state,
while `PreToolUse` continues to capture the before-state required by synchronous enforcement during
the Hook migration. Read-only proposals can therefore reuse an existing anchor with their
reconstructed rollout context; Spotter does not create a filesystem snapshot for every observation.
Snapshot refs retain the existing tree deduplication and prune serialization guarantees.

Forked worktree lifecycle:

```text
CREATED
  ↓
RUNNING / PREPARED
  ↓
COMPLETE / FAILED
  ↓
CLEANED
```

Cleanup must use Git-aware operations.

## 8.3 Counterfactual experiment

Each experiment pair should record:

```text
experiment_id
shared prefix
control prompt/guidance
intervention prompt/guidance
check command
model/config provenance
result
cost/timing
```

Both forks in a pair are created before either arm runs. Execution is refused when their prefix IDs
or captured environment fingerprints differ, or when a declared source resource does not survive
restore. Captured drift is classified as repository-state, missing ignored/untracked files, copied
absolute worktree paths, tool-version, or otherwise unknown environment drift. This preflight
protects pair parity but does not yet establish the replay instrument's empirical noise floor.

Neutral-noise mode resumes both arms with the exact same control prompt. Across repeated pairs it
reports mechanical outcome disagreement separately from environment-preflight mismatches and
infrastructure failures. This avoids treating stochastic continuation variance as snapshot failure;
the resulting rate becomes evidence only after representative real prefixes are executed.

`spotter fork-coverage --session <id>` derives a point-by-point coverage map without creating a
fork. It verifies rollout correlation and current Git object availability, then classifies exact
forks, missing state/context, external-effect contamination, and observation gaps. The report also
names the earliest exact point and coverage before the first known mutation. Each active signal
trigger is linked to the first following proposal unless the signal resolves or becomes stale first;
the report counts trigger follow-ups and the subset that are exactly forkable. Controlled fixture
bootstrap is not inferred when no bootstrap contract exists.

Do not treat experiment machinery as evidence of positive intervention advantage until enough mechanically scored runs exist.

Frozen task-set batches run control/guidance arms from separate clean fixture copies. Each arm row
records the task/set identity, declared budget, setup/check diagnostics, and an explicit task versus
infrastructure/timeout classification. Rows are flushed durably after scoring; resume skips existing
task/arm keys only when the task-set hash, environment, guidance, model, and sandbox still match.
The current Codex backend enforces wall time and records the declared max-turn budget; the Codex CLI
does not yet expose a hard max-turn limit for the batch runner to enforce.

---

# 9. Status, doctor, and repair

## 9.1 `spotter status`

Designed for a quick operational answer.

The implemented command reports manifest/Hook ownership, daemon RPC, observation, live control,
enforcement consequences, storage, reviewer errors, and spend-ledger health. Exit `0` is healthy,
`1` is degraded/warn, and `2` is broken. Expected unavailable surfaces are informational and do not
affect the verdict; once a configured surface becomes unavailable it warns or fails according to
its consequence. App Server ingestion now supplies daemon-owned live identity state, but the status
command does not yet project active/dormant counts; it reports them as unknown rather than inferring
them from Hook sessions.

Target output shape (not current CLI output):

```text
Spotter
  package:        0.6.0
  daemon:         running
  IPC:            healthy

Codex
  integration:    ready
  App Server:     external/shared, connected
  observation:    healthy
  live steer:     available
  PreToolUse:     active

Threads
  active:         2
  dormant:        7
```

Today the command prints the Spotter home and storage size, runtime diagnostic checks, journaled
session count and last-observation age, fork count when present, reviewer/ledger warnings, and
review-token totals when available. It does not report package version or active/dormant thread
counts. The target shape depends on the remaining runtime identity and packaging work.

## 9.2 `spotter doctor`

Doctor should be diagnostic and synthetic, not just configuration inspection.

The implemented doctor preserves the synthetic Hook round-trip and storage/ledger checks, validates
the integration manifest against the exact owned Hook and legacy plugin state, checks daemon IPC,
and probes a configured App Server endpoint. Expected unimplemented capabilities are informational,
configured-but-unavailable capabilities warn, and broken owned integration, daemon, storage, or
ledger contracts fail.

Checks:

```text
Spotter
  binary/package provenance
  config parse/schema
  state directory permissions
  daemon/service registration
  IPC handshake

Codex
  binary/version
  integration manifest consistency
  App Server reachability
  capability negotiation
  same-thread observation path
  Hook registration
  synthetic PreToolUse round-trip

Data
  journal writable
  supported schema versions
  repository registry consistency
  snapshot/worktree sanity
```

## 9.3 Repair

A future:

```bash
spotter doctor --repair
```

may repair safe drift, such as:

- missing Spotter-owned Hook fragment;
- stale service path after a known package-manager migration;
- missing owner-only permissions;
- stale runtime socket after confirmed dead daemon.

Destructive repair requires explicit confirmation or a separate command.

---

# 10. Failure recovery

## 10.1 `spotterd` crash

In managed mode:

```text
spotterd crashes
  ↓
service manager restarts it
  ↓
App Server reconnect
  ↓
list loaded/active threads
  ↓
reconcile current runtime state
  ↓
hydrate durable state where needed
  ↓
READY
```

During daemon downtime, the Hook preserves existing deterministic enforcement with its local Gate
while reporting degraded IPC telemetry. Ambiguous or unsupported proposals still fail open.

## 10.2 App Server disconnect/crash

Connection state:

```text
CONNECTED
  ↓
DEGRADED
  ↓
RECONNECTING
  ├─ CONNECTED
  └─ UNAVAILABLE
```

A running daemon without its observation/control plane is not fully healthy.

## 10.3 Journal write failure

Policy must distinguish:

- inability to persist telemetry/history;
- inability to answer an atomic safety rule.

A journal failure should surface loudly in status/logs, but should not imply a deny if the gate policy itself can still safely return.

## 10.4 Reviewer/provider failure

```text
reviewer timeout / model error
  → ReviewerJob FAILED
  → no live intervention
  → Main continues
  → record error and spend if known
```

---

# 11. Codex upgrade

Codex and Spotter release independently.

After an upgrade, do capability negotiation rather than assuming compatibility from version strings alone.

Probe at least:

```text
thread/turn events
tool start/result events
diff visibility
token usage
turn/steer
turn/interrupt
PreToolUse veto
```

Possible outcome:

```text
Observation:        healthy
Tool outcomes:      healthy
Live steer:         healthy
Interrupt:          unsupported
PreToolUse gate:    healthy
```

This is a valid degraded state; it is better than incorrectly declaring the whole integration either compatible or broken.

`doctor` should detect drift after Codex upgrade and recommend `spotter setup codex` reconciliation if integration files changed.

---

# 12. Spotter upgrade

Target package operation:

```bash
brew upgrade spotter
```

Potential transient state:

```text
CLI binary:      0.7.0
running spotterd: 0.6.0
hook helper:      package path now points to 0.7.0
```

Required behavior:

1. CLI performs daemon protocol/version handshake.
2. If the running daemon is outside the supported compatibility range, request/recommend a graceful restart.
3. Stop accepting new long operations if a migration requires exclusivity.
4. Flush durable state.
5. Restart daemon using stable package-manager path.
6. Run schema migrations if needed.
7. Reconnect App Server.
8. Reconcile live threads.
9. Return to READY.

Never write versioned Homebrew Cellar paths into agent Hooks/service definitions. Use stable `bin`/`opt` paths.

Schema-4 integration Hooks also carry an integration-generation fence. Re-running setup after a
package-build or stable-prefix change rotates the fence and reconciles the exact owned Hook entries.
A Codex process holding an older generated command invokes the current stable binary, but that binary
rejects the retired generation or package build and fails open rather than silently adopting the
replacement build.

The running daemon also watches the stable daemon entry point. A transient unlink/relink during an
upgrade is tolerated, so G1 remains visible until setup explicitly reconciles it. If the entry point
stays absent for ten seconds after uninstall, the daemon exits cleanly without touching durable
state or a shared App Server. launchd keep-alive is conditional on that executable path; systemd
uses restart-on-failure, while package removal is a clean exit. A loaded but unavailable launchd job
left by teardown-less uninstall is re-bootstrapped after reinstall instead of hanging in kickstart.

`spotter update` should not compete with Homebrew for file ownership. It may check for updates or delegate to the package-manager-supported path.

---

# 13. Configuration lifecycle

Recommended precedence:

```text
runtime CLI override
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

Each configuration field should declare reload semantics.

| Setting class | Likely behavior |
| --- | --- |
| reviewer model/budget/cadence | hot reload where safe |
| signal thresholds | hot reload |
| deterministic gate rules | hot reload with atomic config swap |
| socket/runtime path | daemon restart |
| service strategy | setup/migration required |
| App Server integration strategy | setup/migration required |

Config reload must not leave half-old/half-new gate policy during a synchronous request. Parse/validate new config first, then atomically replace the active snapshot.

---

# 14. `spotter teardown codex`

Teardown removes the integration while preserving the Spotter installation and user data.

```bash
spotter teardown codex
```

### Preconditions

- package may be installed;
- integration manifest may be present, missing, or partially drifted.

### Actions

1. Read/reconcile the integration manifest.
2. Detach Spotter from active Codex observation/control if necessary.
3. Remove only Spotter-owned Hook/config fragments.
4. Remove legacy Spotter plugin wiring if it was part of the migrated integration.
5. Disable/remove Spotter's service integration if no remaining agent needs it.
6. Preserve session journals, labels, experiments, snapshots unless explicitly requested otherwise.
7. Remove/mark integration manifest as torn down.

### Critical rule

> **Do not delete configuration Spotter did not create.**

Do not blindly restore an entire old Codex config file.

### App Server rule

```text
teardown codex
  ≠ codex app-server stop
```

A shared App Server may still be used by Codex or other clients.

---

# 15. `brew uninstall spotter`

Users may uninstall without running teardown first. Codex must not become unusable because a Spotter Hook points at a missing binary.

Desired safety property:

```text
Spotter helper exists
  → normal Hook IPC

Spotter helper missing/unavailable
  → Hook path fails open / no-op
  → Codex continues
```

Package uninstall removes package files only.

Generated commands first test the stable bridge executable and always terminate through an explicit
fail-open fallback. If the package link is absent, Codex continues without launching retries or a
removed interpreter. After reinstall, `doctor` reports the missing/old recorded package path or build
and directs the user to rerun `spotter setup codex`; setup then reconciles the new prefix while
preserving durable user data.

If setup previously registered the managed runtime, the integration manifest, Hook, and inactive
service registration deliberately remain repairable integration state. The daemon exits after the
stable executable disappears, and the conditional service definition does not retry the removed
binary. Reinstall plus `doctor`/`setup` reuses the exact ownership manifest, rotates stale build state
when needed, and deduplicates the generated Hooks. An unrecorded or user-modified Spotter Hook is
reported as ambiguous instead of being overwritten.

It should not remove:

- journals;
- labels;
- experiments;
- Git snapshot refs;
- detached worktree metadata;
- user configuration unless explicitly owned by package manager.

If package-manager uninstall cannot run lifecycle cleanup reliably, `spotter teardown --all` remains the recommended clean path but not a correctness requirement for Codex availability.

---

# 16. Purge

Purge is the target destructive data-cleanup operation. No `spotter purge` command exists today;
repository-aware implementation and retention behavior are tracked by
[#89](https://github.com/spotter-agent/spotter/issues/89).

Examples:

```bash
spotter purge --data
spotter purge --snapshots
spotter purge --logs
spotter purge --all
```

`--all` may need to clean resources in repositories, not only `~/.spotter`/state directories.

Safe order:

```text
1. identify registered repositories/resources
2. clean Spotter-created detached worktrees through Git
3. clean Spotter-owned refs according to explicit policy
4. remove journals/labels/opportunity annotations/experiments/logs
5. remove integration/runtime metadata if requested
6. remove repository registry last
```

Purge must support dry-run for repository-affecting cleanup:

```bash
spotter purge --all --dry-run
```

Never remove non-Spotter refs/worktrees based on path guessing.

---

# 17. Reinstall

A normal reinstall may encounter durable data from an earlier Spotter version.

```text
brew install spotter-agent/spotter/spotter
      ↓
existing Spotter data found
      ↓
schema compatible?
  ├─ yes → reuse
  └─ no  → migrate or refuse with actionable error
```

Then:

```bash
spotter setup codex
```

must be safe and idempotent even if old integration fragments remain.

Desired behavior:

- detect prior integration manifest;
- reconcile rather than duplicate Hooks;
- preserve journal/label/experiment history;
- migrate schema explicitly;
- re-run synthetic doctor checks.

---

# 18. Legacy plugin migration

Current users may already have the hook/plugin integration:

```text
Legacy
Codex plugin
├─ SessionStart
├─ UserPromptSubmit
├─ PreToolUse
└─ PostToolUse
```

Target migration:

```text
spotter setup codex
      ↓
detect legacy plugin/hooks
      ↓
preserve current data/config
      ↓
install standalone runtime integration
      ↓
retain observation hooks until App Server responsibility parity is measured
      ↓
remove SessionStart, UserPromptSubmit, and PostToolUse under #86; retain PreToolUse
```

Migration rules:

- never register duplicate Spotter Hooks;
- migrate schema 1/2 manifests to the complete owned-Hook list and reconcile all observation Hooks;
- preserve existing `~/.spotter` data;
- record which legacy mutations were removed;
- keep rollback information until verification succeeds;
- show the migration plan before destructive config changes when ambiguity exists.

---

# 19. Versioned contracts and schema migration

Do not overload the package version as the only compatibility identifier.

Version independently:

```text
spotter_version
ipc_protocol_version
config_schema_version
journal_schema_version
journal_state_schema_version
label_schema_version
signal_sampling_schema_version
experiment_schema_version
task_schema_version
task_set_schema_version
task_batch_schema_version
fork_manifest_schema_version
integration_manifest_version
review_spend_schema_version
source_audit_schema_version
intervention_feedback_schema_version
```

Prefer:

```text
read old
write current
```

where practical.

For destructive migration:

```text
backup
  ↓
migrate to temporary/new representation
  ↓
validate
  ↓
commit/replace
```

When a newer incompatible schema is encountered, refuse rather than guessing.

The review-spend ledger uses the independent `spotter.review_spend` schema. Legacy unversioned
ledgers remain readable and are upgraded lazily by the next successful spend mutation. Current
writes fsync a temporary file, atomically replace the ledger, and fsync its parent directory;
unknown schema names and versions are read-only failures so an older binary cannot reset a budget.

The bounded source-shape audit uses the independent `spotter.source_audit` container schema.
Legacy JSONL samples are prefixed atomically with current metadata before the next append. Unknown
container versions refuse audit writes without interrupting primary App Server journaling.

Human intervention feedback uses per-record `spotter.intervention_feedback` schema metadata.
Released schema-name-less v1 records remain readable and immutable; new records use the current
schema, while unsupported, future, or corrupt history refuses append without changing the file.

Evaluation labels use per-record `spotter.label` schema metadata. Schema-name-less historical
records remain readable, mixed history writes only the current schema, and every append validates
the existing file under its resource lock so future, foreign, or corrupt evidence is not extended.

Intervention opportunity annotations use per-record `spotter.intervention_opportunity` schema
metadata. Released schema-name-less records remain readable, while locked appends validate all
existing timing evidence and refuse unsupported or corrupt history without modifying it.

User configuration uses the independent `spotter.config` schema. Existing schema-less files remain
readable as legacy configuration, the reference file declares the current version, and explicit
future or foreign schemas are rejected before settings can become active. Syntax compatibility here
remains separate from the reload/restart/reconfigure activation boundaries owned by #90.

The Codex ownership manifest uses the independent `spotter.integration_manifest` schema while
retaining its released numeric `schema` field for compatibility. Schema 1-3 manifests upgrade in
memory to current ownership evidence before atomic persistence; foreign, mismatched, and future
formats refuse use. Manifest and owned host-state replacements fsync both temporary content and the
parent directory so successful setup/teardown boundaries survive crashes.

Trace journal records use the independent `spotter.trace_event` schema while retaining the released
`v` field for mixed-history compatibility. The `spotter.journal_state` sidecar is only a versioned,
rebuildable index: unsupported or corrupt cache state is discarded and reconstructed from the
journal, whose full schema validation remains authoritative before append. Both journal records and
atomic sidecar replacements are fsynced.

Frozen evaluation artifacts use separate `spotter.task`, `spotter.task_set`, and
`spotter.task_batch` schemas. Released schema-name-less v1 task and set manifests remain readable;
new bundled manifests declare their identity explicitly. Batch resume validates the complete
history under its resource lock before preflight or mutation, writes only current records, and
repairs a validated torn tail through fsync plus atomic replacement. Future, foreign, or corrupt
history is never truncated or extended.

Fork lineage metadata uses the independent `spotter.fork_manifest` schema while retaining bounded
v1-v6 reads. New CREATING, READY, and FAILED states write v6 identity through a locked, fsynced
atomic replacement. Before replacing an existing manifest, the writer validates its schema under
the same resource lock; future, foreign, or corrupt lineage is never overwritten.

---

# 20. Release lifecycle

The user lifecycle starts with a release pipeline:

```text
source
  ↓
tests
  ↓
version/tag
  ↓
build artifacts
  ↓
checksums/signing as applicable
  ↓
GitHub Release
  ↓
Homebrew formula update
```

The repository now implements the package/build and GitHub Release portions of this pipeline. Pushing
an exact `vMAJOR.MINOR.PATCH` tag runs the repository validation gates, then produces a source
distribution, universal wheel, machine-readable release manifest, and versioned SHA256 file. The
workflow verifies the complete remote artifact set while the release is still a draft and publishes
only after every downloaded asset matches the validated build. Both installed entry points expose
the embedded tag+commit build identity, while the Hook bridge identifies itself independently on
daemon requests. See
[Release artifacts and build identity](releasing.md) for the authoritative artifact and identity
contract. Homebrew-specific layout remains outside the core artifacts.

The [Homebrew lifecycle smoke](homebrew-lifecycle-smoke.md) now verifies:

- clean install;
- setup;
- idempotent setup;
- upgrade with running daemon;
- current Integration Manifest schema repair;
- teardown;
- uninstall without teardown;
- reinstall with retained data.

Broader schema/configuration migrations remain separate #47/#90 gates rather than being inferred
from the packaging fixture.

---

# 21. Required implementation components

The lifecycle implies concrete components rather than one monolithic CLI.

| Component | Lifecycle responsibility |
| --- | --- |
| `PackageInfo` | detect Spotter install/version/provenance |
| `IntegrationManager` | setup/teardown/reconcile agent integrations |
| `IntegrationManifest` | record Spotter-owned mutations |
| `ServiceManager` | launchd/systemd-user or selected runtime startup mechanism |
| `CodexAppServerManager` | ensure/discover/attach App Server without assuming ownership |
| `CapabilityProbe` | determine supported observation/control/enforcement surfaces |
| `DaemonClient` | CLI ↔ spotterd control protocol |
| `Doctor` | synthetic health diagnostics |
| `MigrationManager` | config/journal/label/etc. schema migration |
| `RepositoryRegistry` | track repositories containing Spotter refs/worktrees |
| `RetentionManager` | journal/snapshot/log lifecycle |
| `PurgeManager` | safe destructive cleanup with dry-run |

---

# 22. Lifecycle acceptance checklist

The target lifecycle is not complete until all of these work end-to-end:

- [x] clean package install does not modify Codex;
- [x] `spotter setup codex` is idempotent;
- [x] interrupted setup can heal forward when setup is re-run (pre-manifest teardown cannot infer ownership);
- [ ] ordinary `codex` requires no manual Spotter/App Server startup in managed mode;
- [x] `status` distinguishes daemon, observation, control, enforcement, storage health;
- [x] `doctor` performs a real synthetic round-trip;
- [ ] multiple concurrent threads/sessions remain isolated;
- [ ] daemon crash recovers without corrupting journal/live state;
- [ ] Codex upgrade degrades by capability rather than silently breaking;
- [ ] Spotter upgrade handles a running old daemon and schema migration;
- [x] `teardown codex` removes only Spotter-owned integration changes;
- [x] uninstall without teardown does not break Codex;
- [x] user data survives normal uninstall;
- [ ] purge can enumerate and safely clean repository resources;
- [x] reinstall can reuse compatible retained data (migration-required state remains #47/#90);
- [x] legacy plugin users migrate without duplicate events.

For runtime component boundaries, see [Architecture](architecture.md). For implementation sequencing, see [Roadmap](roadmap.md). For current implementation state, see [Status](status.md).
