Maintained by [@bogyie / Bogyoeng Kim](https://github.com/Bogyie) and [@zerone / Youngjin Jung](https://github.com/YoungJinJung)

# Spotter

> **A runtime spotter for coding agents.**
>
> Your coding agent drives. Spotter watches the trajectory, challenges bad assumptions, and steps in before wasted work compounds.

Spotter is an experimental runtime supervision system for coding agents, starting with Codex.

The project is moving from a hook/plugin-centered prototype toward a **standalone local supervision runtime**. The target architecture is tracked in [#66](https://github.com/Bogyie/spotter/issues/66): a long-lived `spotterd` owns live supervision state, Codex App Server becomes the primary observation/control plane, and hooks shrink to the minimum synchronous enforcement boundary.

> **Important:** that standalone architecture is the project direction, not the current shipping behavior. The current prototype still uses agent hooks/plugins for event collection and deterministic gates.

## Contents

- [The idea](#the-idea)
- [Why trajectory-level supervision](#why-trajectory-level-supervision)
- [Intervention ladder](#intervention-ladder)
- [Architecture direction](#architecture-direction)
- [Current status](#current-status)
- [Use the current prototype](#use-the-current-prototype)
- [Target installation experience](#target-installation-experience)
- [Inspect and evaluate sessions](#inspect-and-evaluate-sessions)
- [Documentation](#documentation)
- [Development](#development)

## The idea

Modern coding agents are good at moving forward. They are less reliable at noticing when they are confidently moving in the wrong direction: drifting from the request, locking onto a weak hypothesis, repeating low-information actions, expanding scope, skipping validation, or carrying a stale assumption deep into an implementation.

Spotter pairs the main coding agent with an **independent reviewer** that observes work while it unfolds and intervenes only when intervention is likely to help.

```text
                 User Goal
                    │
                    ▼
              Main Agent
            drives the work
                    │
       trajectory / tools / results / diffs
                    │
                    ▼
                 Spotter
          observes and maintains state
                    │
       ┌────────────┼────────────┐
       ▼            ▼            ▼
    CONTINUE      VERIFY      NUDGE
                                │
                     BLOCK / INTERRUPT / RESTART
```

A good gym spotter does not lift every rep for you. They watch, stay out of the way, and intervene when the lift is about to fail.

Spotter applies the same principle to coding agents:

- **Always observe. Rarely interrupt.**
- Review the **execution path**, not just the final diff.
- Prefer **evidence and executable verification** over agent-to-agent debate.
- Use deterministic checks for deterministic constraints; spend LLM reasoning only on ambiguous cases.
- Keep the reviewer judgment context independent from the main agent where practical.
- Escalate intervention gradually: `CONTINUE → VERIFY → NUDGE → BLOCK → INTERRUPT → RESTART`.

The goal is not to maximize reasoning. The goal is to keep reasoning and execution **grounded, economical, and recoverable**.

## Why trajectory-level supervision

Traditional review starts after code exists. Spotter starts earlier.

```text
Traditional review
intent → reasoning → exploration → edit → test → diff → REVIEW

Spotter
intent → reasoning → exploration → edit → test → diff
           ↑          ↑          ↑      ↑
             runtime supervision
```

Many agent failures are processes, not isolated bad outputs. A weak assumption can trigger unnecessary exploration, which triggers the wrong edit, which creates misleading failures, which causes further compensating changes. By the time a post-hoc reviewer sees the diff, most of the cost has already been paid.

Spotter is about intervening near the **first meaningful deviation**, before the mistake becomes the trajectory.

Typical targets include:

- specification or scope drift
- unsupported or stale assumptions
- premature implementation
- tunnel vision around one hypothesis
- repetitive exploration with little new evidence
- tool-error loops
- unnecessary refactors or dependency creep
- missed constraints
- edits without adequate validation
- continuing from a premise that later evidence invalidated

## Intervention ladder

| Decision | Meaning | Current state |
| --- | --- | --- |
| `CONTINUE` | Work looks healthy. Stay silent. | reviewer decision implemented in shadow mode |
| `VERIFY` | A consequential assumption needs evidence. | reviewer decision implemented in shadow mode; live delivery not yet implemented |
| `NUDGE` | The trajectory is drifting or wasting effort. | reviewer decision implemented in shadow mode; live delivery not yet implemented |
| `BLOCK` | A pending action clearly violates a deterministic constraint. | deterministic gates implemented; default posture remains observation/shadow unless explicitly enabled |
| `INTERRUPT` | Continuing the active turn is likely to compound failure. | target design, not implemented |
| `RESTART` | The current reasoning context is no longer trustworthy. | target design, not implemented |

A central design question is not only **“Is the agent wrong?”** but **“Is intervening now better than letting it continue?”**

## Architecture direction

The hook-centric prototype proved useful enough to expose its own limits. Long-lived audit state, event-driven signals, reviewer scheduling, asynchronous intervention, multi-session supervision, and recovery all want a persistent runtime rather than a fresh process per hook.

The target Codex architecture is:

```text
                     Codex TUI
                         │
                         ▼
                 Codex App Server
                  │             ▲
                  │ events      │ steer / interrupt
                  ▼             │
                    spotterd
                  │            │
             live state     reviewer/control
                  │
          deterministic gate only
                  │
             PreToolUse Hook
```

Responsibility becomes:

```text
Codex App Server = primary observation + live control plane
spotterd         = long-lived supervision runtime
PreToolUse Hook  = narrow synchronous enforcement plane
journal          = durable event log / recovery source
CLI              = user control plane
```

This also changes the product boundary: Spotter is intended to be an independent runtime that can integrate with multiple coding agents, not a Codex plugin that happens to contain supervision logic.

See [Architecture](docs/architecture.md) and [Lifecycle](docs/lifecycle.md) for the detailed target design.

## Current status

**Prototype / research runtime.** The repository has moved well beyond a scaffold, but the new standalone architecture is not implemented yet.

### Implemented today

- Codex hook ingestion and journaled trajectories
- deterministic pre-action gates with shadow/default-safe posture
- shell-aware command analysis for several destructive command classes
- crash-tolerant, cross-process step journals
- Git-backed snapshots and safe detached-worktree restore
- snapshot deduplication and pruning
- Codex session fork / continuation replay machinery
- shadow-mode model-backed reviewer
- reviewer cadence and detached review execution
- claim/evidence audit ledger with stale-premise propagation where observable outcomes exist
- human labels and coverage-aware metrics
- same-prefix counterfactual experiment harness
- Codex and Claude Code plugin packaging for the current prototype

### Not implemented yet

- standalone Homebrew-installed runtime as the primary distribution path
- `spotterd` as the owner of live in-memory supervision state
- Codex App Server as the primary observation stream
- event-driven two-stage signal → reviewer dispatch
- live `VERIFY` / `NUDGE` delivery to an active turn
- `INTERRUPT` / `RESTART`
- complete side-effect/reversibility handling
- a ground-truth task set with enough real intervention experiments to establish intervention advantage

The project deliberately keeps strong intervention behind measurement. A detector that sounds plausible is not sufficient evidence that interrupting a working agent helps.

## Use the current prototype

The current repository can be installed from source with Python 3.11+:

```bash
git clone https://github.com/bogyie/spotter.git
cd spotter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

For development:

```bash
python -m pip install -e '.[dev]'
```

Copy the example configuration and validate the runtime:

```bash
cp spotter.example.toml spotter.toml
spotter --config spotter.toml
```

The example defaults to passive observation:

```toml
observation_only = true

[main_agent]
adapter = "codex"

[reviewer]
model = "default"
```

### Current plugin integration

The current prototype can be installed as a Codex or Claude Code plugin. This is now considered a **compatibility/prototype path**, not the target product boundary.

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

The bundled integration currently uses lifecycle/tool hooks and writes journals under `~/.spotter/sessions`.

## Target installation experience

The target standalone experience from [#66](https://github.com/Bogyie/spotter/issues/66) is intentionally different:

```bash
brew install spotter
spotter setup codex
spotter doctor

# normal use after setup
codex
```

The user should not need to manually start `spotterd` or a Codex App Server before ordinary use. Manual daemon commands remain operational/debugging escape hatches.

Planned control-plane shape:

```text
spotter setup codex|claude|--all
spotter teardown codex|claude
spotter doctor
spotter status
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

The exact App Server lifecycle strategy is deliberately gated on an E2E PoC because current Codex can choose an embedded App Server when no reusable external daemon is present. See [Lifecycle](docs/lifecycle.md).

## Inspect and evaluate sessions

Recorded sessions can be summarized, labeled, and measured:

```bash
spotter analyze
spotter label --session <id> --step 7 --verdict fp --note "quoted, not executed"
spotter metrics
```

The shadow reviewer can be run explicitly:

```bash
spotter review --session <id>
```

Counterfactual continuations can be prepared or executed where a mechanically decidable `--check` exists:

```bash
spotter experiment --session <id> --step <k> --guidance "..." --check "..."
```

Labels live outside the session journal so the reviewer does not read its own report card. Metrics report coverage and avoid publishing rates from very small samples.

## Documentation

- [Concept](docs/concept.md) — the problem, principles, intervention semantics, and product boundary
- [Architecture](docs/architecture.md) — current/target runtime structure, observation, state, gates, review, and control paths
- [Lifecycle](docs/lifecycle.md) — install, setup, service/App Server lifecycle, sessions, recovery, upgrades, teardown, purge, and migration
- [Research](docs/research.md) — related work, borrowed ideas, open hypotheses, and evidence gaps
- [Roadmap](docs/roadmap.md) — migration sequence, implementation milestones, and evaluation plan

The umbrella direction is tracked in [#66](https://github.com/Bogyie/spotter/issues/66).

## Development

Run the local checks with:

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

The architectural migration should preserve the useful boundaries already present in the prototype: normalized trace data, deterministic gates, review policy, snapshots/replay, and evaluation tooling should not become coupled to one Codex transport.

## Trajectory Engineering

Spotter is also an experiment in a broader idea: **Trajectory Engineering**.

- Prompt engineering shapes the instruction.
- Context engineering shapes what the model can see.
- Harness engineering shapes tools and execution constraints around the model.
- **Trajectory engineering shapes what happens while the agent is already executing.**

The question is:

> **While the agent is working, how do we observe, verify, steer, stop, and recover its path without making supervision more expensive than the mistakes it prevents?**

## License

MIT
