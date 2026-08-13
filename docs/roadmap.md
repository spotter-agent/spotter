# Roadmap

> Spotter's roadmap is organized by **named outcomes**, not phase numbers.  
> Each stage combines implementation with the evidence required to justify moving forward.

GitHub Milestones are the source of truth for **which stage owns an issue's completion**. This document owns the meaning, dependency shape, and evidence gate of each stage.

---

## 30-second summary

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

The stages are not release versions and they are not strict single-threaded sprints. Work can overlap when dependencies allow.

[#78](https://github.com/spotter-agent/spotter/issues/78) proved the shared-server control premise for
the Spotter-managed external path. Runtime work now productizes that path through the App Server
client, `spotterd`, explicit identity, lifecycle, and recovery boundaries.

| Stage | Product outcome | Evidence gate |
| --- | --- | --- |
| **Runtime** | Spotter has a viable standalone runtime/control boundary | same real Codex thread/turn is observable and controllable; normal `codex` UX remains viable |
| **Observe** | App Server events and live state replace broad Hook-era observation | consequential failures are observable early enough; missing evidence is explicit |
| **Detect** | cheap signals target semantic review | precision, miss rate, and detection delay are defensible |
| **Intervene** | `VERIFY` / `NUDGE` reach the correct active turn | intervention helps more than it harms and Main can reject bad supervision |
| **Recover** | Spotter can interrupt or restart a contaminated trajectory | strong actions meet stricter harm/precision budgets and external effects remain visible |
| **Harden** | installation, upgrades, retention, cleanup, and long-term operation are predictable | lifecycle failures are diagnosable, migrations are explicit, stale resources are bounded |

Evaluation is deliberately **inside** the roadmap rather than a separate evaluation track. A mechanism is not considered ready merely because its code exists.

---

# Runtime

## Outcome

Establish the smallest standalone boundary that can own live supervision state and control a real Codex session.

```text
Codex TUI
    ↓
External Codex App Server
    ↕ events / steer / interrupt
spotterd
    ↓
PreToolUse Hook only where synchronous deterministic enforcement is required
```

## Gate result

[#78](https://github.com/spotter-agent/spotter/issues/78) demonstrated that a TUI and Spotter can
attach to one Spotter-managed external App Server, observe the same thread/turn, and steer the real
user-visible turn. The Codex-managed daemon path was unavailable to the tested Homebrew Cask install.
A provisional concurrent identity registry is now explicit but unconsumed. The standalone daemon
and local control foundation exist; managed registration, event routing, and reconnect remain
Runtime work rather than assumed properties.

## Implementation after the gate

The Runtime milestone is now decomposed into concrete boundaries rather than one daemon umbrella issue:

| Issue | Boundary |
| --- | --- |
| [#79](https://github.com/spotter-agent/spotter/issues/79) | `spotterd`, versioned local control, manual lifecycle, and platform-neutral service boundary (implemented) |
| [#80](https://github.com/spotter-agent/spotter/issues/80) | production App Server client and capability negotiation |
| [#81](https://github.com/spotter-agent/spotter/issues/81) | Thread / Turn / Runtime Attachment identity lifecycle (provisional foundation; integration in #85) |
| [#82](https://github.com/spotter-agent/spotter/issues/82) | bounded Hook ↔ daemon IPC for deterministic `PreToolUse` enforcement (implemented) |
| [#31](https://github.com/spotter-agent/spotter/issues/31) | independent live supervision state owned by `spotterd` |
| [#83](https://github.com/spotter-agent/spotter/issues/83) | transactional `setup|teardown codex`, Integration Manifest, legacy migration (implemented) |
| [#84](https://github.com/spotter-agent/spotter/issues/84) | runtime-aware `status` / `doctor` and degraded capability reporting (implemented) |
| [#87](https://github.com/spotter-agent/spotter/issues/87) | daemon/App Server reconnect and thread reconciliation |

Native GitHub issue dependencies encode the actual order between these issues. The table describes responsibility, not a second dependency system.

## Exit condition

Runtime is credible when:

- normal use does not require manually starting Spotter before every Codex session;
- one long-lived runtime owns isolated state for multiple threads;
- the real active Codex turn can be observed and addressed safely;
- deterministic enforcement crosses bounded IPC with a local compatibility fallback;
- setup/teardown ownership is explicit and repeatable;
- a dead or disconnected subsystem is reported as degraded instead of healthy;
- reconnect/restart can recover identity and durable state without inventing missing observations.

---

# Observe

## Outcome

Make the App Server event stream the primary observation source and maintain trustworthy live state without reconstructing the world from journals on every Hook.

```text
App Server event
      ↓
Trace IR normalization
      ↓
live ThreadState
      ↓
durable journal
```

The journal remains durable history and recovery input; it is not the normal hot-state database.

## Work

- normalize App Server events into identity-rich Trace IR and durable history ([#85](https://github.com/spotter-agent/spotter/issues/85));
- maintain live state from that stream ([#31](https://github.com/spotter-agent/spotter/issues/31));
- record runtime cost/timing/provenance ([#33](https://github.com/spotter-agent/spotter/issues/33)); durable records now project Main actions/token coverage, reviewer cost, gate latency, source/receipt timing coverage, and storage without treating unavailable values as zero, while resource sampling and delivery/outcome joins remain;
- measure the actual observability ceiling after migration ([#37](https://github.com/spotter-agent/spotter/issues/37)); the [measurement instrument and current zero-sample baseline](observability-baseline.md) are documented, but labeled App Server failures are still required;
- remove `SessionStart`, `UserPromptSubmit`, and `PostToolUse` only after parity is demonstrated, leaving the minimal enforcement bridge ([#86](https://github.com/spotter-agent/spotter/issues/86)).

## Evidence gate

The important number is not raw event count. It is:

> **What fraction of consequential failures become visible early enough that Spotter could still change the trajectory?**

The existing Hook-era outcome visibility figure is only a baseline. Re-measure after the observation migration.

## Exit condition

- App Server is the primary trajectory source;
- thread/turn/tool-result/diff/provenance coverage is measured;
- missing information is represented as unknown rather than inferred;
- duplicate App Server/Hook observation cannot double-count activity;
- the observability ceiling is high enough to justify semantic detection work on this surface.

---

# Detect

## Outcome

Replace periodic “look every N steps” review with cheap candidate detection followed by semantic judgment only when useful.

```text
runtime event
    ↓
cheap signal
    ↓
possible issue?
    ├─ no → done
    └─ yes → reviewer
```

A signal is a hypothesis, not a verdict.

## Work

- event-driven candidate signals ([#28](https://github.com/spotter-agent/spotter/issues/28));
- precision and miss-rate measurement ([#38](https://github.com/spotter-agent/spotter/issues/38));
- detection delay and wasted-action measurement ([#24](https://github.com/spotter-agent/spotter/issues/24));
- keep reviewer cost visible alongside detection benefit ([#33](https://github.com/spotter-agent/spotter/issues/33)).

## Evidence gate

Report together:

- precision / false-positive rate;
- miss rate or recall estimate;
- abstention / blind-spot rate;
- detection delay / wasted actions;
- reviewer call and token cost.

A detector that almost never fires does not earn trust through precision alone.

## Exit condition

- reviewer calls are primarily evidence-triggered rather than cadence-triggered;
- precision and misses are both measured;
- detections arrive early enough to plausibly save work;
- model cost is visible alongside benefit.

---

# Intervene

## Outcome

Deliver soft supervision to the correct live turn without confusing Spotter guidance with user intent.

## Work

- live `VERIFY` / `NUDGE` via `turn/steer` ([#22](https://github.com/spotter-agent/spotter/issues/22));
- target-turn freshness, stale/discard policy, and exactly-once delivery;
- human-visible supervision provenance and feedback ([#45](https://github.com/spotter-agent/spotter/issues/45));
- wrong-nudge susceptibility ([#23](https://github.com/spotter-agent/spotter/issues/23));
- intervention advantage, harm, recovery, and ignored rate ([#34](https://github.com/spotter-agent/spotter/issues/34)).

## Evidence gate

Prefer same-prefix comparisons when possible:

```text
control: continue
vs
intervention: VERIFY / NUDGE
```

Classify intervention better, control better/harm, tied, or inconclusive/infrastructure failure. Also test the opposite failure direction: Main must be able to reject plausible but wrong Spotter guidance when evidence contradicts it.

## Exit condition

- guidance reaches the intended active turn exactly once;
- stale guidance does not leak into unrelated turns;
- intervention benefit and harm are measured against defensible outcomes;
- wrong-nudge compliance is low enough for the intended policy;
- live intervention remains conservative when evidence is insufficient.

---

# Recover

## Outcome

Add stronger control only after soft intervention is understood.

## Work

- action reversibility and external side-effect tracking ([#30](https://github.com/spotter-agent/spotter/issues/30));
- `turn/interrupt` with shadow-first policy;
- restart from a deliberately small verified-state payload ([#26](https://github.com/spotter-agent/spotter/issues/26));
- checkpoint/lineage tracking and explicit disclosure of effects recovery cannot undo.

A restart payload should remain intentionally small:

```text
user goal
explicit constraints
verified evidence
current repository/checkpoint state
explicitly retained artifacts
```

## Evidence gate

Stronger actions require stronger evidence than `VERIFY` or `NUDGE`. Measure would-interrupt precision before activation, harm/recovery by action type, useful work retained after restart, and unreverted external effects.

## Exit condition

- interruption and restart have explicit evidence budgets;
- strong actions are auditable and attributable to a specific turn/state;
- restart never implies external rollback that did not happen.

---

# Harden

## Outcome

Make months of use boring: install, upgrade, recover, clean up, and reinstall without hidden state or mystery failures.

## Work

- persisted schema/version contracts and migrations ([#47](https://github.com/spotter-agent/spotter/issues/47));
- repeatable standalone/Homebrew packaging and release artifacts ([#88](https://github.com/spotter-agent/spotter/issues/88));
- repository-aware purge, retention, uninstall, and reinstall lifecycle ([#89](https://github.com/spotter-agent/spotter/issues/89));
- runtime configuration, protocol/version handshake, and upgrade compatibility ([#90](https://github.com/spotter-agent/spotter/issues/90));
- run the real-session configuration comparison once the relevant product states exist ([#36](https://github.com/spotter-agent/spotter/issues/36)).

Multi-agent lifecycle work remains deferred until another adapter is real enough to justify a concrete integration issue.

## Exit condition

- packaged installs/upgrades are repeatable;
- supported migrations and mixed-version windows are tested;
- stale resources are discoverable and bounded;
- uninstall and integration teardown have predictable ownership;
- purge is repository-aware and conservative;
- capability loss after Codex updates degrades features explicitly;
- operational A/B results can be reported with uncertainty rather than anecdotes.

---

# Cross-cutting evidence infrastructure

Some work supports several stages. Its Milestone indicates the stage by which its current completion gate is needed, not the only area it benefits.

- [#21](https://github.com/spotter-agent/spotter/issues/21) — reproducible mechanically scored task set.
- [#42](https://github.com/spotter-agent/spotter/issues/42) — replay/fork noise floor and environmental fidelity.
- [#33](https://github.com/spotter-agent/spotter/issues/33) — Main cost, Spotter cost, timing, and outcome telemetry.

Do not make causal intervention claims from fork deltas before #42 establishes an instrument noise bound, and do not report benefit without its cost.

---

# Issue selection

Roadmap stage, urgency, size, and problem domain are deliberately separate dimensions. Use GitHub's native metadata:

- **Milestone** — the roadmap stage that owns completion/evidence;
- **Priority** — `Urgent`, `High`, `Medium`, or `Low` for current sequencing pressure;
- **Effort** — `XS` through `XL` for change surface, validation difficulty, and uncertainty, not elapsed-time prediction;
- **Area** — the stable primary product/problem domain;
- **Dependencies** — actual `blocked by` / `blocking` relationships.

Keep `Urgent` rare. A dependency should mean the downstream issue cannot meaningfully complete without the blocker, not merely that the issues are related.

---

# Revised MVP

A useful Spotter does not require a graph database, learned policy, or automatic external rollback engine. It must prove this loop:

```text
1. Observe the real active coding-agent trajectory.
2. Maintain independent live state for goal, constraints, hypotheses, evidence, and progress.
3. Generate cheap candidate failure signals.
4. Use semantic review only where deterministic evidence is insufficient.
5. Deliver VERIFY/NUDGE to the correct live turn or deterministic BLOCK before execution.
6. Record timing, cost, freshness, delivery, and outcome.
7. Demonstrate that intervention helps more than it harms.
```

Everything after that should make the loop safer, stronger, or easier to operate—not obscure whether it works.
