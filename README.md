Maintained by [@bogyie / Bogyoeng Kim](https://github.com/Bogyie) and [@zerone / Youngjin Jung](https://github.com/YoungJinJung)

# Spotter

> **A runtime spotter for coding agents.**
>
> Your coding agent drives. Spotter watches the trajectory, challenges bad assumptions, and steps in before wasted work compounds.

Spotter is an experimental runtime supervision layer for coding agents, starting with Codex.

## Contents

- [How it works](#the-idea)
- [Installation](#installation)
- [First run](#first-run)
- [Install the agent plugins](#install-the-agent-plugins)
- [Manual hook setup](#manual-hook-setup)
- [Documentation](#documentation)
- [Development](#development)

Modern coding agents are good at moving forward. They are less reliable at noticing when they are confidently moving in the wrong direction: drifting from the request, locking onto a weak hypothesis, repeating low-information actions, expanding scope, skipping validation, or carrying a stale assumption deep into an implementation.

Spotter pairs the main coding agent with an **independent reviewer model** that observes the work as it unfolds and intervenes only when intervention is likely to help.

```text
                 User Goal
                    │
                    ▼
              Main Agent
            drives the work
                    │
       action / tool / result / diff
                    │
                    ▼
                 Spotter
          observes the trajectory
                    │
       ┌────────────┼────────────┐
       ▼            ▼            ▼
    CONTINUE      VERIFY      NUDGE
                                │
                     BLOCK / INTERRUPT / RESTART
```

## The idea

A good gym spotter does not lift every rep for you. They watch, stay out of the way, and intervene when the lift is about to fail.

Spotter applies the same principle to coding agents:

- **Always observe. Rarely interrupt.**
- Review the **execution path**, not just the final diff.
- Prefer **evidence and executable verification** over agent-to-agent debate.
- Use deterministic checks for deterministic constraints; spend LLM reasoning only on ambiguous cases.
- Let the main agent and Spotter use **different models and isolated judgment contexts**.
- Escalate intervention gradually: `CONTINUE → VERIFY → NUDGE → BLOCK → INTERRUPT → RESTART`.

The goal is not to add more reasoning. The goal is to keep reasoning and execution **grounded, economical, and recoverable**.

## Why trajectory-level supervision?

Traditional code review starts after code exists. Spotter starts earlier.

```text
Traditional review
intent → reasoning → exploration → edit → test → diff → REVIEW

Spotter
intent → reasoning → exploration → edit → test → diff
           ↑          ↑          ↑      ↑
             runtime supervision
```

Many agent failures are processes, not isolated bad outputs. A weak assumption can trigger unnecessary exploration, which triggers the wrong edit, which creates misleading test failures, which causes further compensating changes. By the time a post-hoc reviewer sees the diff, most of the cost has already been paid.

Spotter is about intervening near the **first meaningful deviation**, before the mistake becomes the trajectory.

## What Spotter watches for

Spotter focuses on execution failures such as:

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

It is intentionally not just another static code reviewer.

## Intervention ladder

| Decision | Meaning |
| --- | --- |
| `CONTINUE` | Work looks healthy. Stay silent. |
| `VERIFY` | A consequential assumption needs evidence before more work depends on it. |
| `NUDGE` | The trajectory is starting to drift or waste effort. Add a small course correction. |
| `BLOCK` | A pending action clearly violates a known constraint or policy. Stop it before execution. |
| `INTERRUPT` | Continuing the current turn is likely to compound a bad trajectory. |
| `RESTART` | The current reasoning context itself is no longer trustworthy; restart from verified state. |

A central design question is not only **“Is the agent wrong?”** but **“Is intervening now better than letting it continue?”**

## Codex is a good first target

Current Codex runtime primitives make this design practical:

- lifecycle hooks including `PreToolUse`, `PostToolUse`, `SessionStart`, and others
- `PreToolUse` can inspect, block, or rewrite many local tool calls before execution
- App Server streams plan, reasoning-summary, tool, message, and diff-related events
- `turn/steer` can inject a course correction into an active turn
- `turn/interrupt` can stop an in-flight turn

See [Architecture](docs/architecture.md) for the proposed integration.

## Main + Spotter

Spotter is not meant to become a second user giving orders.

> **The user defines the goal. Spotter reviews the path.**

The main agent remains the driver. Spotter acts as an independent falsifier and runtime controller. When they disagree, the preferred resolution is a cheap empirical probe — a test, compiler result, repository search, log, or other external evidence — rather than a long debate between models.

## Trajectory Engineering

Spotter is also an experiment in a broader idea: **Trajectory Engineering**.

Prompt engineering shapes an instruction. Context engineering shapes what the model can see. Harness engineering shapes the environment and tools around the agent.

Trajectory engineering asks a different question:

> **While the agent is already working, how do we observe, verify, steer, stop, and recover the path it is taking?**

Spotter is intended as a reference implementation for exploring that layer.

## Installation

Spotter requires Python 3.11 or newer and has no third-party runtime dependencies. Clone
the repository and install it in a virtual environment:

```bash
git clone https://github.com/bogyie/spotter.git
cd spotter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

For a development installation, include the formatter, linter, type checker, and test
runner with `python -m pip install -e '.[dev]'`.

## First run

Copy the example rather than editing the tracked file, then start Spotter:

```bash
cp spotter.example.toml spotter.toml
spotter --config spotter.toml
```

The command validates the configuration and reports the selected adapter, reviewer, and
operating mode. The example selects Codex and defaults to safe, passive observation:

```toml
observation_only = true

[main_agent]
adapter = "codex"

[reviewer]
model = "default"
```

`"default"` delegates reviewer model choice to the codex account, so the `[reviewer]`
table may be omitted. Pin a specific id only if your auth supports it: ChatGPT-account
auth rejects unknown or unavailable ids with a slow retry loop rather than a fast error —
and the message ("not supported when using Codex with a ChatGPT account") does not
distinguish a wrong slug from a restricted one. Check `codex` for the slugs your account
actually has. Which reviewer model works best is an open experimental question (see
docs/research.md RQ3), not a constant.

## Install the agent plugins

The repository is a native plugin for both Codex and Claude Code. The plugin bundles the
hook bridge, so installing the Python package separately is not required; Python 3.11 or
newer must still be available as `python3`.

### Codex

Add the Spotter GitHub repository as a marketplace, then add the qualified plugin:

```bash
codex plugin marketplace add bogyie/spotter
codex plugin add spotter@spotter
```

Restart Codex after installation. Codex discovers `.codex-plugin/plugin.json` and the
bundled `hooks/hooks.json` configuration.

### Claude Code

Add the Spotter GitHub repository as a marketplace and install its qualified plugin:

```bash
claude plugin marketplace add bogyie/spotter
claude plugin install spotter@spotter
```

Restart Claude Code after installation. Claude Code discovers `.claude-plugin/plugin.json`
and the same bundled hooks.

By default, plugin hooks observe only and write journals under `~/.spotter/sessions`. To
customize gates, copy `spotter.example.toml` to `spotter.toml` in the project being
supervised. For Claude Code, change `main_agent.adapter` to `"claude-code"`. A config path
in `SPOTTER_CONFIG` takes precedence over the project-local file:

```bash
export SPOTTER_CONFIG=/absolute/path/to/spotter.toml
```

The wrapper resolves Spotter from the plugin checkout and therefore keeps working when the
marketplace caches the plugin in another directory.

## Manual hook setup

If the plugin manager is unavailable, install the Python package as described above and add
the bridge to the agent's hook configuration manually. For Codex, add this to
`.codex/hooks.json`; for Claude Code, use `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [{"hooks": [{"type": "command", "command": "spotter hook --config /absolute/path/to/spotter.toml"}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "spotter hook --config /absolute/path/to/spotter.toml"}]}]
  }
}
```

Without `--config`, the hook only records observations. Set `observation_only = false`
explicitly to enforce configured gates.

Recorded sessions can then be summarized with `spotter analyze`, judged one flag at a
time with `spotter label`, and scored with `spotter metrics`:

```bash
spotter analyze                                              # what was flagged, with context
spotter label --session <id> --step 7 --verdict fp --note "quoted, not executed"
spotter metrics                                              # gate FP rate, reviewer precision
```

Labels are stored outside the session journal, so a reviewer never reads its own report
card. `spotter metrics` reports every rate with its coverage and withholds rates that
rest on too few labels. Run `spotter --help` for the complete command and option list.

## Documentation

- [Concept](docs/concept.md) — the problem definition, design principles, terminology,
  non-goals, and evaluation questions
- [Architecture](docs/architecture.md) — components, event flow, intervention policy,
  and the proposed Codex integration
- [Research](docs/research.md) — related work and the ideas Spotter borrows
- [Roadmap](docs/roadmap.md) — implementation stages, milestones, and evaluation plan

## Development

Install the development extras as described above, then run every local check with:

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

The package deliberately keeps normalized trace events and orchestration separate from
the Codex adapter. The hook bridge records tool events and applies deterministic gates;
model-backed review is the next implementation milestone.

## Status

**Prototype.** Codex hook ingestion and deterministic gates are implemented. Reviewer
decisions and replay are not.

The first milestone is deliberately small: observe a Codex trajectory with a separate reviewer model, identify high-confidence misbehavior, and measure whether intervention helps more than it harms.

## License

MIT
