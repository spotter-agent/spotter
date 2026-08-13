Maintained by [@bogyie / Bogyoeng Kim](https://github.com/Bogyie) and [@zerone / Youngjin Jung](https://github.com/YoungJinJung)

<div align="center">

# Spotter

<picture>
  <img alt="Spotter" src="docs/assets/main-ts.png" width="40%" style="max-width: 250;"/>
</picture>

> **A runtime spotter for coding agents.**  
> Your coding agent drives. Spotter watches the trajectory, challenges bad assumptions, and steps in before wasted work compounds.

Spotter is an experimental **runtime supervision system for coding agents**, starting with Codex.

</div>

---

## 30-second summary

Spotter asks a more specific question than “is the agent wrong?”

> **Can we detect a bad assumption, loop, scope drift, or missing validation while the agent is still working, and intervene before the mistake becomes expensive?**

The repository is already beyond a scaffold. The current prototype implements Hook-based trajectory collection, daemon-backed deterministic gates, crash-safe journals, Git snapshots, fork/replay, a shadow reviewer, claim/evidence state, evaluation labels/metrics, counterfactual experiment machinery, and a standalone daemon/control foundation.

The current prototype and target product architecture are different:

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

[#78](https://github.com/spotter-agent/spotter/issues/78) proved shared observation and steering for a
Spotter-managed external App Server. The daemon now owns connection epochs, reconnect/reconciliation,
and conservative control freshness; endpoint selection in setup remains the last ordinary-use gap.

For the fastest project snapshot, read [Status](docs/status.md). For sequence and evidence gates, read [Roadmap](docs/roadmap.md).

---

## Current status

Legend: ✅ implemented · 🟡 partial/shadow · 🧪 proof required · 🎯 target · ❌ not implemented

| Capability | Status | Notes |
| --- | --- | --- |
| Hook trajectory ingestion | ✅ | Current primary observation path |
| Deterministic pre-action gates | ✅ | Bounded daemon IPC; local fallback preserves enforcement if the daemon is unavailable |
| Crash-tolerant journal | ✅ | Locking, fsync, torn-tail recovery |
| Git snapshot / detached restore | ✅ | User HEAD/index remain untouched |
| Fork / continuation replay | ✅ | Same-prefix continuation machinery |
| Shadow reviewer | ✅ | Produces `CONTINUE / VERIFY / NUDGE`; no live delivery |
| Claim/evidence audit ledger | 🟡 | Works where observable outcomes exist |
| Evaluation labels / metrics | ✅ | Coverage-aware evaluation and precision/FP metrics |
| Counterfactual experiment harness | ✅ | Control/guidance same-prefix pairs |
| Standalone `spotterd` runtime | 🟡 | Process, gate IPC, per-thread immutable state, and App Server recovery ownership implemented; setup endpoint selection remains |
| Versioned release artifacts | 🟡 | Tag-only sdist/wheel builds, SHA256 sums, and CLI/daemon/bridge build identity exist; GitHub Release/Homebrew publication remains |
| App Server primary observation | 🟡 | Configured endpoints route through daemon-owned epoch/reconciliation into durable Trace IR and ThreadState; setup does not select an endpoint yet |
| Event-driven signal engine | ❌ | Current reviewer trigger is periodic |
| Live `VERIFY / NUDGE` | ❌ | Target: `turn/steer` |
| `INTERRUPT` | ❌ | Target: `turn/interrupt` |
| `RESTART` | ❌ | Requires verified-state + side-effect-aware recovery |
| Codex setup lifecycle | 🟡 | Transactional setup/teardown, ownership diagnostics, and degraded capability reporting implemented; packaging and full App Server integration remain |

### Current runtime work

[#78](https://github.com/spotter-agent/spotter/issues/78) proved that a Spotter-managed external
App Server can share the TUI's real thread and steer its active turn. The production client in
[#80](https://github.com/spotter-agent/spotter/issues/80) now exposes events, thread queries,
control methods, and per-capability degraded state. [#81](https://github.com/spotter-agent/spotter/issues/81)
adds a thread/turn/attachment registry. [#85](https://github.com/spotter-agent/spotter/issues/85)
uses it to normalize authoritative lifecycle, message, plan, reasoning-summary, command, file-change,
MCP, search, diff, and token events into durable Trace IR. Exact replays are deduplicated across
restarts, operation outcomes correlate by item ID, and unknown notification families remain explicit.
[#31](https://github.com/spotter-agent/spotter/issues/31) reduces that Trace IR into isolated,
immutable, versioned ThreadState snapshots owned by `spotterd`; restart hydration deliberately
requires live turn reconciliation before control becomes ready again. [#87](https://github.com/spotter-agent/spotter/issues/87)
adds the single-owner connection loop, bounded reconnect backoff, durable observation gaps,
connection/attachment epochs, multi-thread reconciliation, and exact epoch+turn control fencing.
[#79](https://github.com/spotter-agent/spotter/issues/79)
adds the `spotterd` process, versioned local control handshake, manual lifecycle commands, and a
platform-neutral service boundary. [#83](https://github.com/spotter-agent/spotter/issues/83) adds
transactional Codex setup/teardown, managed user-service registration, and integration ownership.
[#84](https://github.com/spotter-agent/spotter/issues/84) makes `status` and `doctor` distinguish
daemon, observation, control, enforcement, integration drift, storage, and reviewer health.

---

## Roadmap

The roadmap uses named outcomes instead of `P0–P9` / `E0–E5` codes:

```text
Runtime
  ↓
Observe
  ↓
Detect
  ↓
Intervene
  ↓
Recover
  ↓
Harden
```

| Stage | What becomes trustworthy |
| --- | --- |
| **Runtime** | standalone App Server/`spotterd` boundary and lifecycle |
| **Observe** | primary event ingestion, live state, and observability |
| **Detect** | cheap candidate signals + semantic reviewer quality |
| **Intervene** | live `VERIFY/NUDGE`, provenance, benefit vs harm |
| **Recover** | interrupt/restart with reversibility and side-effect awareness |
| **Harden** | upgrades, migrations, retention, cleanup, diagnostics, long-term operation |

Experiments are not a separate parallel roadmap. Each stage has an evidence gate. Implementing a mechanism does not prove the mechanism helps.

GitHub Milestones carry stage assignment for issues. See [Roadmap](docs/roadmap.md) for stage meaning, linked issues, dependencies, and exit criteria.

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

The goal is not to maximize alarms. It is to reduce **wasted progress after the first meaningful deviation**.

---

## Intervention ladder

Spotter prefers the weakest intervention likely to help.

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

Spotter separates four responsibilities:

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

Target data flow:

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

State ownership is explicit:

```text
memory   = live supervision state for active/dormant threads
journal  = durable event history + recovery/analysis source
snapshot = filesystem/Git state at a branch/recovery point
```

---

## Use the current prototype

Python 3.11+:

```bash
git clone https://github.com/spotter-agent/spotter.git
cd spotter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

The package has one runtime dependency, `websockets`, which pip installs automatically. The `dev`
extra adds the repository's test, type-checking, formatting, and release-build tools; use
`pip install -e .` when those tools are not needed. Tagged artifact production is documented in
[Release artifacts and build identity](docs/releasing.md).

Configuration:

```bash
cp spotter.example.toml spotter.toml
spotter --config spotter.toml
```

Current plugin compatibility path:

```bash
# Codex
codex plugin marketplace add spotter-agent/spotter
codex plugin add spotter@spotter

# Claude Code
claude plugin marketplace add spotter-agent/spotter
claude plugin install spotter@spotter
```

Plugin installation remains a compatibility path. New standalone integration can use:

```bash
spotter setup codex --dry-run
spotter setup codex
spotter teardown codex
```

App Server endpoint selection remains pending; when a verified endpoint is present in the integration
manifest, `spotterd` owns its connection and recovery loop. Setup does not print or claim an endpoint
that it has not verified.

Useful commands:

```bash
spotter observe
spotter hook  # hook bridge; JSON payload on stdin
spotter setup codex [--dry-run|--portable]
spotter teardown codex
spotter daemon start|stop|restart|status
spotter analyze
spotter review --session <id>
spotter label --session <id> --step <n> --verdict fp
spotter metrics
spotter observability [--session <id>]
spotter fork --session <id> --step <n>
spotter fork-coverage --session <id>
spotter prune --repo /path/to/repo  # dry-run unless --apply is supplied
spotter tasks validate path/to/task-set.toml
spotter tasks preflight path/to/task-set.toml
spotter tasks run path/to/task-set.toml --guidance "..." --run
# after an interrupted batch, repeat the same conditions and add:
# --resume ~/.spotter/experiments/task-batches/<batch>.jsonl
spotter experiment --session <id> --step <n> --guidance "..." --check "..."
# repeat the same neutral continuation to measure replay/model outcome noise:
spotter experiment --session <id> --step <n> --neutral --pairs 3 --check "..." --run
```

When snapshotting is enabled, `SessionStart` pins one baseline Git snapshot so read-only exploration
before the first mutation has a fork anchor; mutation boundaries continue to add deduplicated
snapshots. `spotter fork` writes a durable manifest under `~/.spotter/fork-manifests/`. The manifest links the
source event, snapshot, rollout-prefix digest, external-effect warnings, and captured environment
fingerprint. Counterfactual pairs are fully prepared and must pass shared-prefix and captured-
environment parity checks before either agent arm runs. Ignored files, environment variables, and
agent configuration are explicitly marked as not captured rather than assumed equal.
Neutral mode gives both arms the exact control prompt and reports mechanically judgeable outcome
disagreement, environment-preflight mismatch, and infrastructure-failure rates separately. The
command provides the measurement path; a representative executed corpus is still required before
claiming a replay noise bound.

`fork-coverage` is read-only. It classifies every journaled proposal against the currently available
rollout and Git snapshot objects, reports the earliest exact fork, and separates pre-mutation
coverage from missing state/context, external-effect contamination, and observation gaps. Because
the command checks object availability now, pruned or moved historical repositories remain visible
as `NOT_FORKABLE_STATE` rather than being counted as covered.

Task-set validation freezes task and fixture hashes and checks the versioned scorer/budget contract without executing commands. Preflight runs setup, the broken-state scorer, a declared known-good transform, and the positive scorer in a temporary fixture copy; it never runs agent arms. Because preflight and batch execution run corpus-declared shell commands, use `validate` only for untrusted task sets. `tasks run` requires an explicit paid-run opt-in, starts each control/guidance arm from a clean fixture copy, journals bounded mechanical results with `fsync`, and resumes only when the frozen set, environment, and run conditions still match. The Codex backend enforces the wall-time budget; it records the declared max-turn budget for parity/provenance because `codex exec` does not expose a hard turn-limit flag. The existence of the experiment harness does **not** mean positive intervention advantage has been established. Enough executed multi-task runs remain an evidence gap.

---

## Target installation and operations UX

The target first-run experience is intentionally small:

```bash
brew install spotter
spotter setup codex
spotter doctor

# normal use afterwards
codex
```

The user should not need to manually run `spotter daemon start` or `codex app-server daemon start` before each session.

Detailed ownership, rollback, upgrades, uninstall, purge, and reinstall behavior are specified in [Lifecycle](docs/lifecycle.md).

---

## Issues and metadata

Repository issues use GitHub's native structured metadata rather than prefixed label taxonomies:

- **Type** — `Task`, `Bug`, `Feature`, `Architecture`, or `Experiment`;
- **Priority** — `Urgent`, `High`, `Medium`, or `Low`;
- **Effort** — `XS` through `XL`, representing change surface, validation burden, and uncertainty;
- **Area** — the primary product/problem domain;
- **Milestone** — `Runtime`, `Observe`, `Detect`, `Intervene`, `Recover`, or `Harden` when the issue belongs to the product roadmap;
- **Dependencies** — native `blocked by` / `blocking` relationships for actual blockers.

Labels are exceptional contributor signals only; currently `good first issue` and `help wanted` remain. Detailed semantics live in [Repository Conventions](docs/conventions.md#13-issue-metadata-and-triage).

---

## Documentation

- **[Docs index](docs/README.md)** — choose a document by question/reading path
- **[Status](docs/status.md)** — current implementation, blocker, and next steps
- **[Concept](docs/concept.md)** — problem definition, principles, and intervention semantics
- **[Architecture](docs/architecture.md)** — process, state, event, control, and failure contracts
- **[Lifecycle](docs/lifecycle.md)** — install → setup → run → recover → upgrade → teardown → purge
- **[Roadmap](docs/roadmap.md)** — named stages, dependencies, and evidence gates
- **[Research](docs/research.md)** — prior work, borrowed ideas, open hypotheses, and evidence gaps
- **[Conventions](docs/conventions.md)** — code, issue metadata, branch, PR, and documentation conventions

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
