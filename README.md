<div align="center">

<p>
  <strong>English</strong> ·
  <a href="README.ko.md">한국어</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

<h1>Spotter</h1>

<picture>
  <img alt="Spotter" src="docs/assets/main-ts.png" width="250" />
</picture>

<h3>Catch bad coding-agent trajectories before they become expensive.</h3>

<p>
  Spotter is a local runtime supervisor for coding agents.<br />
  It watches how work unfolds—not only the final diff—so costly deviations can be caught early.
</p>

<p>
  <code>local-first</code> · <code>bounded gates</code> · <code>trajectory-aware</code>
</p>

<p>
  <a href="#install"><strong>Install</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="#connect-spotter-to-codex"><strong>Quick start</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="https://spotter-agent.github.io/spotter/"><strong>Project site</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="docs/user-guide.md"><strong>Detailed guide</strong></a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="docs/status.md">Current status</a>&nbsp;&nbsp;·&nbsp;&nbsp;
  <a href="docs/README.md">Documentation</a>
</p>

</div>

---

## Why Spotter

Coding agents rarely fail in one obvious step. A weak assumption can shape the next search, edit,
and test; repeated local decisions then turn a recoverable mistake into wasted time, tokens, and
repository churn.

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>🔭 Trajectory-aware</strong><br />
      <sub>Observe decisions, evidence, edits, and validation—not only the final diff.</sub>
    </td>
    <td width="33%" valign="top">
      <strong>⚡ Bounded safety</strong><br />
      <sub>Keep deterministic checks local and fast before risky tool use.</sub>
    </td>
    <td width="33%" valign="top">
      <strong>🩺 Visible degradation</strong><br />
      <sub>Make unavailable observation or control diagnosable without blocking Codex.</sub>
    </td>
  </tr>
</table>

Spotter maintains an independent view of the running trajectory and helps answer:

- Is the agent repeating failures or equivalent actions without learning anything new?
- Is the change growing beyond the requested scope?
- Did meaningful edits happen without relevant validation?
- Is the agent still acting on a hypothesis that newer evidence has weakened?
- Is a deterministic safety rule about to be violated?

The goal is not more alerts. It is less wasted work after the first meaningful deviation.

## What Spotter does today

The current runtime can:

- collect Codex Hook and configured App Server events into durable trajectory journals;
- maintain daemon-owned live state for threads, turns, evidence, progress, and detected signals;
- enforce bounded deterministic gates before risky tool use;
- detect candidate loops, stalled exploration, scope growth, missing validation, and stale
  hypotheses;
- run optional semantic reviews in shadow mode, with a separate live-advisory opt-in;
- expose health and integration diagnostics through `spotter status` and `spotter doctor`;
- preserve Git-backed snapshots and replay material for recovery and analysis.

> [!IMPORTANT]
> Spotter is under active development. Deterministic gates are active; semantic `VERIFY` and
> `NUDGE` decisions remain shadow-only by default and can steer only with explicit active-mode
> configuration. Live advisory benefit and task-ownership safety are not yet established. Check
> [Status](docs/status.md) for the exact current boundary.

Codex is the primary standalone integration.

## How it works

```text
Codex
  ├─ Hooks ────────────────► bounded deterministic gates
  └─ App Server events ────► observation and control when configured
                                   │
                                   ▼
                                spotterd
                                   │
                 journal · live state · signals · review · opt-in advisory
```

Deterministic gates stay bounded, while slower semantic review remains off synchronous
tool-execution paths. If observation or control degrades, diagnostics remain explicit and
generated Hooks fail open rather than blocking normal Codex use.

## Install

The supported packaged installation uses the official Homebrew tap:

```bash
brew install spotter-agent/spotter/spotter
```

Verify both installed entry points:

```bash
spotter --version
spotterd --version
```

Package installation only installs the CLI, daemon, and Hook bridge. It does **not** edit Codex
configuration or register an integration.

For a source/development checkout, follow [CONTRIBUTING.md](CONTRIBUTING.md#local-setup).

## Connect Spotter to Codex

Make sure the `codex` CLI is installed and available on `PATH`. For setup's endpoint preflight,
start one user-managed App Server in a separate terminal:

```bash
codex app-server --listen ws://127.0.0.1:4500
```

Then inspect and apply the managed integration, verify it, and start the TUI against that same
endpoint:

```bash
spotter setup codex --endpoint ws://127.0.0.1:4500 --dry-run
spotter setup codex --endpoint ws://127.0.0.1:4500
spotter doctor
spotter codex
```

Setup is transactional and idempotent. It records the exact Spotter-owned Hooks and service state so
later repair or teardown does not guess at user-owned configuration. It verifies the server identity
and observation capabilities before committing the endpoint. On later `spotter codex` launches,
Spotter reuses a listener already at that endpoint or starts a detached App Server with the recorded
Codex binary when the endpoint is unreachable. It never stops that server or claims exclusive
ownership. Setup without `--endpoint` remains available as an explicit degraded Hook-only mode; App
Server observation and live control are then unavailable.

## Everyday commands

| Command | Purpose |
| --- | --- |
| `spotter status` | Show integration, daemon, capability, and storage health |
| `spotter doctor` | Run synthetic health checks and print actionable diagnostics |
| `spotter codex [args...]` | Start `spotterd`, verify the configured endpoint, and launch the remote TUI |
| `spotter daemon status` | Inspect the packaged `spotterd` process and build identity |
| `spotter daemon reload` | Atomically apply safe config changes or stage the next generation |
| `spotter update` | Detect the package owner and print non-mutating update guidance |
| `spotter metrics` | Summarize collected runtime and evaluation metrics |
| `spotter sample-signals` | Persist a deterministic detector-silence sampling frame |
| `spotter label-opportunity` | Record semantic and observable intervention windows |
| `spotter analyze` | Review per-session interventions, costs, and provenance-linked outcomes |
| `spotter observability` | Inspect which trajectory sources and normalized events are available |
| `spotter --help` | Show the complete command surface |

Configuration is optional. Use [spotter.example.toml](spotter.example.toml) as the reference when
you need to customize gates, storage, snapshots, or reviewer budgets. Signal-driven semantic
reviews spend model tokens and are disabled by default. Live delivery is a second opt-in that also
requires active mode; enable either deliberately and keep the provided per-session and per-day
limits.

## Upgrade

Upgrade the Formula, then rerun setup so Spotter can reconcile the installed build, running daemon,
and integration generation:

```bash
spotter update
brew upgrade spotter-agent/spotter/spotter
spotter setup codex
spotter doctor
```

`spotter update` is advisory: it reports the current build and the appropriate Homebrew, pipx, uv
tool, or pip command. It never overwrites package-managed files, and source/editable installs are
directed back to their owning development workflow.

Persistent Hook and service references use stable package entry points rather than versioned
Homebrew Cellar paths. Spotter detects a still-running older daemon instead of assuming it matches
the newly installed CLI. A rerun without `--endpoint` retains and re-verifies an endpoint already
recorded by a successful setup.

## Disconnect or uninstall

To remove the Codex integration but keep Spotter installed:

```bash
spotter teardown codex
```

For a clean uninstall:

```bash
spotter teardown codex
brew uninstall spotter-agent/spotter/spotter
```

Homebrew uninstall removes package-owned executables and stops the packaged runtime. It
intentionally does not purge separately managed user data under `~/.spotter`. An integration left
behind by an uninstall without teardown is designed to fail open and can be repaired after
reinstalling.

See [Lifecycle](docs/lifecycle.md) before upgrades, recovery, migration, teardown, or data removal
that needs more than the common path above.

## Operational guarantees

<details>
<summary><strong>Show the safety and ownership guarantees</strong></summary>

- `brew install` and `brew upgrade` do not silently modify Codex configuration.
- `spotter setup codex` and `spotter teardown codex` change only exact owned integration state.
- Generated Hooks use stable executable paths and fail open when Spotter is unavailable.
- Spotter does not stop a shared Codex App Server that it cannot prove it owns.
- Uninstall and user-data purge are separate operations.
- `status` and `doctor` make degraded observation or control visible.

These contracts are covered by fast fixtures and a real macOS Homebrew install → live upgrade →
uninstall → reinstall lifecycle smoke. See
[Homebrew lifecycle smoke](docs/homebrew-lifecycle-smoke.md) for the evidence and reproduction path.

</details>

## Documentation

| If you want to… | Read |
| --- | --- |
| See what works today and what is still experimental | [Status](docs/status.md) |
| Install, configure, operate, troubleshoot, or uninstall | [Detailed user guide](docs/user-guide.md) |
| Understand the complete package and integration contract | [Lifecycle](docs/lifecycle.md) |
| Understand the product idea and intervention model | [Concept](docs/concept.md) |
| Understand runtime boundaries and durable state | [Architecture](docs/architecture.md) |
| Follow upcoming work and evidence gates | [Roadmap](docs/roadmap.md) |
| Review experiments, hypotheses, and evidence | [Research](docs/research.md) |
| Build or contribute to Spotter | [Contributing](CONTRIBUTING.md) |
| Browse every project document | [Documentation index](docs/README.md) |

---

<p align="center">
  Maintained by <a href="https://github.com/Bogyie">@bogyie / Bogyoeng Kim</a> and
  <a href="https://github.com/YoungJinJung">@zerone / Youngjin Jung</a>.<br />
  Released under the <a href="LICENSE">MIT License</a>.
</p>
