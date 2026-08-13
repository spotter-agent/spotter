# AGENTS.md

This file is the operating guide for coding agents working in this repository.

## Start here

Before changing code, read only what the task needs:

- Project state and immediate priorities: [`docs/status.md`](docs/status.md)
- Runtime boundaries and invariants: [`docs/architecture.md`](docs/architecture.md)
- Install/setup/update/remove behavior: [`docs/lifecycle.md`](docs/lifecycle.md)
- Named roadmap stages and evidence gates: [`docs/roadmap.md`](docs/roadmap.md)
- Contribution and repository conventions: [`CONTRIBUTING.md`](CONTRIBUTING.md), [`docs/conventions.md`](docs/conventions.md)

Do not treat target architecture as current behavior. `docs/status.md` is the source of truth for what is implemented today.

The roadmap uses named outcomes rather than numbered phases:

```text
Runtime → Observe → Detect → Intervene → Recover → Harden
```

Issue selection should follow native **Priority**, **Milestone**, and dependency state rather than a guessed numeric phase.

## Working rules

1. **Keep the change scoped.** Do not opportunistically redesign adjacent systems.
2. **Preserve runtime boundaries.** Agent-specific transport belongs behind adapters; supervision policy should not depend directly on Codex event shapes.
3. **Keep semantic review off synchronous hot paths.** `PreToolUse`-style enforcement must remain bounded and deterministic.
4. **Fail visibly, not silently.** Degraded observation/control must be diagnosable. Existing fail-open gate behavior should remain explicit.
5. **Preserve durable-state safety.** Do not weaken journal, snapshot, ref, worktree, or migration guarantees without a deliberate design change.
6. **Separate implementation from evidence.** A mechanism being implemented does not prove it improves outcomes.
7. **Update docs when contracts change.** Architecture/lifecycle/roadmap/status changes should land with the code that changes them.
8. **Respect issue triage.** Use GitHub native Type, Priority, Effort, Area, Milestone, and dependencies. Labels are only exceptional contributor/cross-cutting signals.

## Where changes usually belong

| Change | Primary area |
| --- | --- |
| CLI commands / UX | `src/spotter/cli.py` |
| Configuration | `src/spotter/config.py`, `spotter.example.toml` |
| Hook ingestion / hook responses | `src/spotter/hook.py`, `hooks/`, `scripts/` |
| Agent integration | `src/spotter/adapters.py`, agent-specific modules/plugins |
| Deterministic policy | `src/spotter/gates.py` |
| Reviewer behavior | `src/spotter/reviewer.py`, reviewer scheduling/budget modules |
| Claim/evidence state | `src/spotter/audit.py` |
| Snapshot / fork / replay | snapshot/replay/experiment modules and Git helpers |
| Metrics / labels | `src/spotter/metrics.py`, `src/spotter/labels.py` |
| Runtime architecture direction | `docs/architecture.md`, `docs/lifecycle.md`, `docs/roadmap.md` |
| Repository issue metadata | GitHub native metadata; `docs/conventions.md` |

If the right boundary is unclear, inspect nearby tests before creating a new abstraction.

## Validation

Run the checks relevant to the change. For code changes, the default full suite is:

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

Prefer focused tests while iterating, then run the full suite before finishing when practical.

For behavior changes, add or update tests that demonstrate the contract rather than only exercising the implementation.

## Change discipline

- Avoid new dependencies unless they materially simplify the design; Spotter intentionally has no runtime dependencies today.
- Keep Python 3.11 compatibility.
- Keep public/user-facing behavior backward compatible unless the issue/PR explicitly introduces a migration.
- Never write tests that depend on hidden reasoning or private chain-of-thought.
- Do not invent observed outcomes when the runtime did not provide them.
- Do not delete Spotter-owned Git refs/worktrees with raw filesystem deletion when Git-aware cleanup is required.
- Do not couple `spotterd` lifetime to shared Codex App Server lifetime without an explicit ownership rule.

## Before opening a PR

Confirm:

- the change matches an issue/roadmap direction or explains why it intentionally diverges;
- tests/checks were run and failures are disclosed;
- docs/status are updated if implementation state changed;
- architecture/lifecycle docs are updated if a contract changed;
- the PR describes **why**, **what changed**, and **how it was validated**.

Keep PRs reviewable. Prefer a small complete change over a broad partial rewrite.
