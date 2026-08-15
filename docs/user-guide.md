<div align="center">

<h1>Spotter Installation and Usage Guide</h1>

<p>
  <strong>English</strong> ·
  <a href="user-guide.ko.md">한국어</a> ·
  <a href="user-guide.zh-CN.md">简体中文</a>
</p>

<p>
  Installation, runtime operation, configuration, upgrades, safe removal,<br />
  troubleshooting, and useful issue reports for advanced users.
</p>

<p><a href="../README.md">← Back to README</a></p>

</div>

---

> [!IMPORTANT]
> Spotter is under active development. Deterministic Hook gates work today, but semantic `VERIFY`
> and `NUDGE` decisions are still recorded in shadow mode rather than delivered into live turns.
> App Server observation and control require explicit configuration. See [Status](status.md) for the
> authoritative current boundary.

<table>
  <tr>
    <td width="50%" valign="top">
      <a href="#3-install-with-homebrew"><strong>📦 Install with Homebrew</strong></a><br />
      <sub>Use the supported packaged installation.</sub>
    </td>
    <td width="50%" valign="top">
      <a href="#4-install-manually-from-source"><strong>🛠️ Install manually</strong></a><br />
      <sub>Run Spotter from a stable Python environment.</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="#5-connect-spotter-to-codex"><strong>🔌 Connect Codex</strong></a><br />
      <sub>Preview, apply, and verify the managed integration.</sub>
    </td>
    <td width="50%" valign="top">
      <a href="#9-disconnect-and-uninstall"><strong>🧹 Remove safely</strong></a><br />
      <sub>Separate integration teardown, package removal, and user data.</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="#10-troubleshooting"><strong>🩺 Troubleshoot</strong></a><br />
      <sub>Diagnose PATH, daemon, configuration, and observation problems.</sub>
    </td>
    <td width="50%" valign="top">
      <a href="#11-report-an-issue"><strong>📝 Report an issue</strong></a><br />
      <sub>Collect useful diagnostics without exposing sensitive data.</sub>
    </td>
  </tr>
</table>

## 1. Choose an installation method

| Method | Best for | Maintenance model |
| --- | --- | --- |
| Homebrew (recommended) | Normal macOS and Linux use | Homebrew owns the package; Spotter owns only its Codex integration |
| Dedicated Python environment | Source evaluation and advanced/manual installations | You own Python, the virtual environment, source updates, and executable stability |
| Editable development install | Contributors changing Spotter itself | Same as a manual install, plus development dependencies and repository checks |

Do not install both Homebrew and a manual copy on the same `PATH` unless you intentionally manage
which `spotter` and `spotterd` pair is active. Setup records stable executable paths, and mixing
installations is a common cause of CLI/daemon build mismatches.

## 2. Prerequisites

All installations need:

- the `codex` CLI installed and available on `PATH` before integration setup;
- a user account that can create files under its home directory;
- Git when snapshots, forks, replay, or a source installation are used.

Manual installations also need Python 3.11 or newer. Confirm the relevant tools before continuing:

```bash
codex --version
git --version
python3 --version
```

Do not run Spotter setup with `sudo`. The integration, background service, runtime socket, and data
belong to the current login account.

## 3. Install with Homebrew

Install from the official tap:

```bash
brew install spotter-agent/spotter/spotter
```

Verify that the CLI and daemon come from the same package boundary:

```bash
command -v spotter
command -v spotterd
spotter --version
spotterd --version
```

Package installation does not edit Codex configuration, register Hooks, or start a service. Those
changes happen only during explicit setup.

## 4. Install manually from source

Use a dedicated virtual environment whose path will remain stable. This example keeps source and
the installed entry points separate:

```bash
mkdir -p ~/.local/src ~/.local/share/spotter
git clone https://github.com/spotter-agent/spotter.git ~/.local/src/spotter
python3 -m venv ~/.local/share/spotter/venv
~/.local/share/spotter/venv/bin/python -m pip install --upgrade pip
~/.local/share/spotter/venv/bin/python -m pip install ~/.local/src/spotter
```

Add the environment's `bin` directory to your shell `PATH`, or invoke its entry points by absolute
path. Confirm that both commands resolve beside each other before setup:

```bash
~/.local/share/spotter/venv/bin/spotter --version
~/.local/share/spotter/venv/bin/spotterd --version
```

Activate the environment before the remaining examples if it is not already on `PATH`:

```bash
source ~/.local/share/spotter/venv/bin/activate
```

Spotter persists the discovered CLI and daemon paths in its integration. Do not move or delete the
virtual environment after setup. Teardown the integration first, or recreate it at the same stable
path and rerun setup.

For an editable contributor install, use the repository-local workflow instead:

```bash
cd ~/.local/src/spotter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

See [Contributing](../CONTRIBUTING.md#local-setup) before modifying the project.

## 5. Connect Spotter to Codex

Always inspect the mutation plan first:

```bash
spotter setup codex --dry-run
```

Then apply the managed integration and verify it end to end:

```bash
spotter setup codex
spotter doctor
```

Managed setup is transactional and idempotent. It updates only Spotter-owned Codex Hook/plugin
state, keeps fingerprinted backups, registers `spotterd` as a login-scoped user service, verifies a
synthetic Hook round trip, and commits an ownership manifest under
`~/.spotter/integrations/codex.json` by default.

If persistent user-service registration is unavailable or unwanted, use portable mode:

```bash
spotter setup codex --portable
spotter doctor
```

Portable mode starts `spotterd` without persistent login registration. You are responsible for
starting it again after logout, reboot, or process termination:

```bash
spotter daemon start
```

After setup, use Codex normally:

```bash
codex
```

## 6. Configuration

Configuration is optional. By default, Spotter looks for `~/.spotter/spotter.toml`; setting
`SPOTTER_HOME` moves the Spotter configuration, data, integration, runtime, and log root together.
You can also select a file explicitly with `--config` during setup and diagnostics.

A conservative starting configuration is:

```toml
observation_only = true
snapshot_on_patch = true

[main_agent]
adapter = "codex"

[reviewer]
model = "default"
# on_signals = true
# deliver_on_signals = true  # also requires on_signals and observation_only = false
# every_steps = 25
max_per_session = 20
max_per_day = 100

[gates]
forbidden_paths = []
block_dependency_changes = false

# Optional exact metadata for an MCP server/tool pair.
[mcp_semantics."inventory"."lookup_item"]
operation = "read"
reversibility = "A"
resource_fields = ["item_id"]
```

Configured MCP semantics are keyed by exact server and tool identity, so equally named tools on
different servers can have different effects. `read` must use Class A, `write`/`delete` may use
Class B or C, and `unknown` must use Class C. Only declared, non-secret scalar resource fields are
journaled. Missing metadata falls back to bounded known-name rules and then conservative Class C;
tool descriptions never authorize a safer class.

`deliver_on_signals` is intentionally separate from reviewer execution and requires active mode
(`observation_only = false`). When enabled, only fresh
signal-driven `VERIFY`/`NUDGE` decisions steer the exact active turn; `CONTINUE`, stale decisions,
and ambiguous outcomes are never retried. The advisory is recorded as current-turn Spotter
supervision and explicitly does not replace the user's task.

Validate and register an explicit configuration with:

```bash
spotter doctor --config /absolute/path/to/spotter.toml
spotter setup codex --config /absolute/path/to/spotter.toml
```

Signal-driven and periodic semantic reviews spend model tokens and are off by default. Enable them
deliberately, preserve per-session and per-day caps, and remember that their decisions are currently
recorded only.

## 7. Operate and inspect Spotter

### External effects and recovery evidence

List the external effects projected for a session:

```bash
spotter effects list --session SESSION
```

After an explicit probe or compensating action, append the observed resolution without rewriting
the original effect:

```bash
spotter effects resolve --session SESSION --effect-id EFFECT_ID \
  --resolution reconciled_absent --note "explicit probe did not find the resource"
```

Supported states are `reversed`, `compensated`, `reconciled_present`, `reconciled_absent`, and
`still_unresolved`. Compensation also requires `--related-effect-id` naming the separately
journaled compensating effect; Spotter refuses to call a missing relation resolved.

### Health and runtime

```bash
spotter status
spotter doctor
spotter daemon status
```

`spotter status` is a quick, non-invasive summary. `spotter doctor` performs deeper synthetic
checks. Their exit codes are:

| Exit | Meaning |
| --- | --- |
| `0` | healthy |
| `1` | supervision works with warnings or a configured surface is degraded |
| `2` | a required integration, daemon, storage, or ledger contract is broken |

An unconfigured App Server can produce an observation/live-control warning after setup. This is a
known current boundary: deterministic PreToolUse Hook enforcement remains independent, while full
App Server observation and live control are unavailable.

Manual daemon controls affect only `spotterd`; they never stop or reset a shared Codex App Server:

```bash
spotter daemon start
spotter daemon restart
spotter daemon stop
```

### Collected data and coverage

```bash
spotter metrics
spotter observability
spotter analyze
```

Use `--session <id>` with `analyze`, `metrics`, or `observability` to limit output to one recorded
session. `metrics` keeps Main, semantic-reviewer, deterministic-runtime, control-lifecycle, and
experiment costs separate and prints explicit coverage for unavailable observations. `analyze`
places a compact version of those costs beside each session's recorded interventions and shows
mechanical task/replay outcomes only when their durable arm or fork-prefix provenance names that
session. It never guesses an outcome link from a file name or timestamp. Reviewer token output
includes session and call coverage and says `partial` or `unavailable` when totals are incomplete.
Reviewer calls and labeled reviewer outcomes are also split by `signal`, `periodic`, and `manual`
launch provenance so A/B cohorts are not silently pooled.
Effect coverage separately reports exact and bounded classifications, unknown family/shell/MCP/
adapter buckets, conservative Class C fallbacks, and the concrete unknown reason counts.
Treat journals and diagnostics as potentially sensitive: they can contain repository paths,
prompts, tool payloads, and other work context.

To measure detector misses without treating every unflagged event as an equivalent negative, first
declare an event-kind stratum and deterministic inclusion probability, then label only the printed
sampled steps:

```bash
spotter sample-signals --session <id> --signal-type failure_streak \
  --event-kind command_result --sample-rate 0.1
spotter label --session <id> --step <sampled-step> \
  --signal-type failure_streak --verdict miss --note "written criterion"
spotter metrics --session <id>
```

Repeated sampling with the same detector, event kinds, and rate consumes only the new journal
suffix. Metrics preserve the frame probability and exclusions, report coverage separately per
detector/event-kind/rate stratum, and explicitly avoid generalizing the result to unsampled event
kinds.

For detection-delay studies, record both the retrospective semantic window and the separate window
where Spotter could actually observe the required evidence. The two intervals are not assumed to be
nested. Every anchor must name a journal step with a stable Trace IR event ID; the stored annotation
pins that identity rather than relying on a mutable line number:

```bash
spotter label-opportunity --session <id> --opportunity-id <failure-id> \
  --semantic-earliest <step> --semantic-latest <step> \
  --observable-earliest <step> --observable-latest <step> \
  --required-evidence <step> --note "why intervention was warranted"
```

Repeat `--required-evidence` as needed and use `--rater` when double-labeling a subset. Reusing one
opportunity ID and rater appends a correction while preserving annotation history.
`spotter metrics --session <id>` links only candidates that cite every required evidence event. It
reports early/within/late/never classifications, bounded delay, and actions, failed outcomes, and
files observed after the actionable window opened. Stale annotations and still-open windows remain
explicit instead of becoming misses. For signal-driven reviews, it also follows candidate event IDs
through the queued job, inference start, and decision, reporting step delays and explicit stale,
terminal-without-decision, or observation-gap coverage. Non-stale `VERIFY`/`NUDGE` decisions are
also followed through control dispatch and RPC acceptance/terminal outcome; missing, stale, failed,
unknown, and gap-crossed control stages stay visible rather than receiving fabricated delay.
Accepted steers count as adopted only when their client message ID is observed on the exact target
thread, turn, and connection epoch. Target completion without that evidence remains
`RPC_ACCEPTED_ONLY`; stale-after-accept and identity/gap-limited cases remain explicit. Spotter-tagged
advisory inputs stay in supervision state rather than replacing the user goal. If a known advisory
ID appears outside its target turn or connection epoch, Spotter records an expired-advisory
diagnostic for later safety evaluation.

A completed assistant message explicitly marked `final_answer` closes Spotter's soft-intervention
window even before `turn/completed`. Pending review results then become stale and a late steer is not
sent. Commentary or messages without a phase do not trigger this conservative fence.
If Codex rejects a steer because no active turn exists or the expected turn mismatches, telemetry
reports that request as stale with the stable reason `no_active_turn` or `turn_mismatch`. A known
non-steerable active turn remains a failed control; unrecognized rejection text stays generic.
When RPC acceptance has no matching advisory input before the target final-answer/turn boundary,
the journal records `rpc_accepted_only`. This is explicit non-observation coverage, not evidence that
Main consumed or acted on the steer.

### Storage maintenance

Snapshot pruning is dry-run by default. Run it from, or point it at, the relevant repository:

```bash
spotter prune --repo /path/to/repository --forks
spotter prune --repo /path/to/repository --forks --apply
```

Journal expiry requires an explicit age and can remove the snapshots needed to fork old sessions:

```bash
spotter prune --repo /path/to/repository --journals --max-age-days 30
spotter prune --repo /path/to/repository --journals --max-age-days 30 --apply
```

Read the dry-run output before adding `--apply`. Spotter uses Git-aware cleanup for owned refs and
worktrees; do not replace it with raw recursive deletion.

## 8. Upgrade and reinstall

For Homebrew:

```bash
brew upgrade spotter-agent/spotter/spotter
spotter setup codex
spotter doctor
```

Rerunning setup reconciles the installed CLI, the running daemon, generated Hook paths, build
identity, and integration generation.

For the dedicated source environment:

```bash
git -C ~/.local/src/spotter pull --ff-only
~/.local/share/spotter/venv/bin/python -m pip install --upgrade ~/.local/src/spotter
source ~/.local/share/spotter/venv/bin/activate
spotter setup codex
spotter doctor
```

If an upgrade was interrupted, do not hand-edit generated Hooks first. Run `spotter status`, then
rerun setup and doctor. Setup is designed to reconcile retained ownership state without duplicating
Hooks.

## 9. Disconnect and uninstall

To remove only the Codex integration while keeping Spotter and its data:

```bash
spotter teardown codex
```

For a clean Homebrew uninstall:

```bash
spotter teardown codex
brew uninstall spotter-agent/spotter/spotter
```

For a manual Python installation, run teardown while its commands still exist, then uninstall the
distribution from the same environment:

```bash
spotter teardown codex
spotter daemon stop
~/.local/share/spotter/venv/bin/python -m pip uninstall spotter-agent
```

After confirming those exact paths are dedicated to Spotter, you may remove the virtual environment
and source checkout with your normal file-management tools.

Normal uninstall does not delete `~/.spotter` (or `SPOTTER_HOME`) user data. Preview all registered
repository resources without deleting anything with `spotter purge --all --dry-run`; add `--json`
for machine-readable output. Journal snapshots and recovery checkpoints, fork manifests,
experiment results, and live worktrees appear as `REFERENCED`
instead of deletion candidates. The preview returns non-zero for inaccessible or ambiguous state.
Protect an exact registered snapshot explicitly with `spotter pins add --repo <path> --snapshot
<sha>`; inspect roots with `spotter pins list` and release one with `spotter pins remove --pin-id
<uuid>`. Manual pins also survive `prune --max-age-days` until removed.
Preview exact snapshot cleanup with `spotter purge --snapshots --dry-run`; remove only proven-owned,
unreferenced worktrees and snapshot refs with `spotter purge --snapshots`. Referenced or ambiguous
resources are skipped and reported. Data/log/full destructive scopes remain pending in
[#89](https://github.com/spotter-agent/spotter/issues/89).

If the package was removed before teardown, generated Hooks are designed to fail open. Reinstall the
same package method, run `spotter teardown codex`, and uninstall again to remove the recorded owned
integration safely.

## 10. Troubleshooting

<details>
<summary><strong><code>spotter</code> or <code>spotterd</code> is not found</strong></summary>

```bash
command -v spotter
command -v spotterd
```

For Homebrew, confirm the Formula is installed and that Homebrew's `bin` directory is on `PATH`. For
a manual installation, activate the intended environment. If the two commands resolve to different
installation roots, fix `PATH`, then rerun `spotter setup codex` and `spotter doctor`.

</details>

<details>
<summary><strong>Setup cannot find Codex</strong></summary>

```bash
command -v codex
codex --version
spotter setup codex --dry-run
```

Install or expose Codex on `PATH` in the same login environment that runs setup. If you use a custom
`CODEX_HOME`, keep it consistent for setup, doctor, and normal Codex sessions.

</details>

<details>
<summary><strong>The daemon is unavailable or has the wrong build</strong></summary>

```bash
spotter daemon status
spotter daemon restart
spotter setup codex
spotter doctor
```

After an upgrade, setup is the supported reconciliation step. Do not edit `launchd` or
`systemd --user` definitions until doctor output shows that the registered service itself is the
problem.

</details>

<details>
<summary><strong>Doctor reports unavailable observation or live control</strong></summary>

An App Server endpoint is not selected automatically today. If Hook enforcement is reported as
available, deterministic gating still works; App Server observation and live `VERIFY`/`NUDGE` do
not. Check [Status](status.md) before treating this known product boundary as an installation bug.

</details>

<details>
<summary><strong>Configuration fails to parse or appears ignored</strong></summary>

```bash
spotter doctor --config /absolute/path/to/spotter.toml
spotter setup codex --config /absolute/path/to/spotter.toml --dry-run
```

Confirm that `[main_agent]` exists and `adapter = "codex"` is a non-empty string. Setup stores the
chosen configuration path in the integration manifest. Hooks fail open and use safe defaults when
the recorded configuration becomes unusable, while printing the problem to stderr.

</details>

<details>
<summary><strong>No sessions or recent observations appear</strong></summary>

Run a normal Codex session after setup, then inspect:

```bash
spotter status
spotter doctor
spotter observability
```

Check the Hook ownership result, the last-observation age, and whether your Codex session uses the
same `CODEX_HOME` that setup inspected.

</details>

<details>
<summary><strong>Storage or permission checks fail</strong></summary>

By default, mutable state is under `~/.spotter`, including `logs/`, `runtime/`, `sessions/`, and
`integrations/`. Confirm that the current user owns and can write the configured root. Avoid
changing ownership with a broad recursive command; inspect the exact failing path shown by doctor.

</details>

<details>
<summary><strong>Spotter was uninstalled without teardown</strong></summary>

Codex should continue because generated Hooks fail open. To clean up the owned integration, reinstall
Spotter, run `spotter doctor`, then `spotter teardown codex` before uninstalling again.

</details>

<p align="right"><a href="#spotter-installation-and-usage-guide">Back to top ↑</a></p>

## 11. Report an issue

Use the [GitHub issue chooser](https://github.com/spotter-agent/spotter/issues/new/choose) and select
the bug, feature, documentation, architecture, experiment, or maintenance form that best matches the
request.

A useful bug report includes:

- what happened, what you expected, and the impact;
- the smallest reliable reproduction sequence;
- Homebrew or manual installation method;
- Spotter CLI and daemon versions, Codex version, OS, architecture, and Python version when relevant;
- the exact failing command and exit code;
- relevant `status`, `doctor`, and `daemon status` lines;
- whether the issue started after setup, an upgrade, configuration change, or reinstall.

Collect the basic diagnostics with:

```bash
spotter --version
spotterd --version
codex --version
spotter status
spotter doctor
spotter daemon status
```

Redact tokens, credentials, private repository names and paths, prompts, source code, and personal
information. Do not attach raw journals, complete configuration files, or full logs without first
reviewing them. For a security-sensitive or exploitable issue, do not publish details in a public
issue; check the repository Security page for a private reporting channel.

## 12. Related documentation

- [Status](status.md) — authoritative implemented vs. experimental boundary
- [Lifecycle](lifecycle.md) — complete package, service, integration, recovery, and removal contract
- [Configuration reference](../spotter.example.toml) — all currently documented configuration keys
- [Homebrew lifecycle smoke](homebrew-lifecycle-smoke.md) — packaged lifecycle evidence
- [Contributing](../CONTRIBUTING.md) — source setup and contribution workflow
