# Repository Conventions

This document is the shared convention reference for contributors and coding agents.

The goal is consistency where consistency saves review time, not ceremony for its own sake.

## Quick reference

| Area | Convention |
| --- | --- |
| Python | 3.11+, typed, `ruff` + strict `mypy` |
| Runtime dependencies | Avoid by default; justify additions |
| Tests | Behavior-focused, deterministic, no hidden reasoning dependency |
| Branches | `<type>/<short-kebab-description>` |
| Commits | Short imperative summary; explain non-obvious why in body |
| PRs | One coherent purpose, explicit validation, link issue when relevant |
| Docs | English; current state and target design must be distinguishable |
| Architecture | Agent transport behind adapters; semantic review async; deterministic gates bounded |
| Persisted state | Version contracts; migrate or refuse explicitly |

---

## 1. Code style

The repository tooling is authoritative:

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

Python conventions:

- support Python 3.11+;
- keep type annotations complete enough for strict `mypy`;
- prefer small explicit data structures over loosely shaped dictionaries at core boundaries;
- use standard-library facilities unless an external dependency materially improves correctness or maintainability;
- keep side effects close to the boundary that owns them;
- prefer pure parsing/decision functions where practical so policy is easy to test.

Avoid abstractions whose only purpose is speculative future flexibility. Add a boundary when there are already distinct responsibilities or transports to isolate.

## 2. Naming

Use names that describe runtime meaning rather than implementation accidents.

Prefer:

```text
thread_id
turn_id
reviewer_job
intervention
observation
control
enforcement
```

over overloaded generic names such as `session`, `state`, or `event` when a more precise scope exists.

For booleans, prefer names that read as predicates (`is_stale`, `has_outcome`, `can_interrupt`).

For enums/state machines, use explicit states rather than multiple loosely related flags when the lifecycle matters.

## 3. Module boundaries

The target architecture separates:

```text
agent adapter / transport
        ↓
normalized runtime representation
        ↓
Spotter state + policy
        ↓
intervention / persistence
```

Keep these invariants:

- Codex-specific payload shapes should not leak throughout Spotter core.
- App Server, hooks, and future agent transports should normalize into shared internal contracts.
- Deterministic policy belongs outside reviewer prompts when it can be implemented directly.
- Semantic reviewer work must not be inserted into latency-sensitive synchronous gate paths.
- Durable logs are not a substitute for live runtime state in the target architecture.

If a change intentionally violates one of these boundaries, call it out in the PR and update `docs/architecture.md`.

## 4. Error and degraded behavior

Spotter supervises another tool; it should not accidentally become a new single point of failure.

General rules:

- distinguish **healthy**, **degraded**, and **unavailable** where capabilities can fail independently;
- do not report silence as health when observation/control is disconnected;
- existing deterministic gate ambiguity should fail open unless an explicit policy changes that contract;
- error messages should state the failed capability and the user-visible consequence;
- do not silently infer missing runtime facts.

Example:

```text
Observation: unavailable
Live intervention: unavailable
PreToolUse gate: active
```

is more useful than a single `Spotter: running` status.

## 5. Persisted state and migrations

Anything persisted beyond one process run is a contract.

Examples:

- journals
- labels / metrics metadata
- experiment metadata
- integration manifests
- configuration schemas
- snapshot refs / repository registry

When changing a persisted format:

1. identify the schema/version boundary;
2. keep compatible reads where reasonable;
3. migrate deliberately or reject unsupported newer data;
4. test upgrade/read behavior;
5. update lifecycle documentation.

Do not guess how to interpret unknown newer schemas.

## 6. Git resources

Spotter can create refs, snapshots, and detached worktrees. Treat them as managed resources, not disposable directories.

- use Spotter-owned ref namespaces;
- do not touch user HEAD/index as a side effect of snapshot/restore operations;
- use Git-aware worktree/ref cleanup;
- make destructive cleanup conservative and inspectable;
- retain lineage needed by replay/experiment results.

## 7. Tests

Tests should prove behavior or contracts.

Prefer tests that answer questions such as:

- Does this gate allow/deny the intended action?
- Does stale reviewer output stay out of a later turn?
- Does a torn journal preserve the valid prefix?
- Does setup remain idempotent?
- Does degraded App Server state appear in status?

Avoid tests that merely mirror internal function calls without protecting behavior.

For bug fixes, add a regression test when the failure can be reproduced deterministically.

For async/concurrent behavior, test ordering and identity explicitly rather than relying on sleeps when possible.

## 8. Research and experiment conventions

Experiments must separate hypothesis from result.

Before running a meaningful experiment, record:

```text
hypothesis
comparison / baseline
controlled prefix or task set
metric / mechanical check
budget
what result would change the decision
```

Report negative, tied, and inconclusive results. Do not promote a mechanism into default behavior merely because it was implemented successfully.

When claiming intervention benefit, prefer same-prefix or otherwise controlled comparisons over anecdotes.

## 9. Documentation conventions

Repository documentation is written in English.

Keep these categories distinct:

- **Current** — implemented behavior supported by code/tests/observations
- **Partial / shadow** — implemented but not fully active or validated
- **PoC required** — architectural premise still needing E2E proof
- **Target** — agreed direction, not implemented
- **Evidence gap** — mechanism exists but its value is unproven

Document ownership:

- `docs/status.md` — current state and next blocker
- `docs/concept.md` — problem, principles, intervention semantics
- `docs/architecture.md` — component and runtime contracts
- `docs/lifecycle.md` — install/setup/runtime/update/remove contracts
- `docs/roadmap.md` — dependency order and exit criteria
- `docs/research.md` — prior work, hypotheses, evidence

Avoid duplicating a detailed contract across documents. Link to the owning document.

## 10. Branch conventions

Use:

```text
<type>/<short-kebab-description>
```

Recommended types:

```text
feature/
fix/
docs/
refactor/
experiment/
chore/
```

Examples:

```text
feature/app-server-client
fix/torn-journal-recovery
docs/contribution-workflow
experiment/reviewer-trigger-thresholds
```

Branch names are descriptive aids, not release metadata.

## 11. Commit conventions

Use a short imperative subject. Conventional-commit-style prefixes are encouraged but not required.

Good examples:

```text
feat: add App Server capability probe
fix: keep stale review out of completed turns
docs: define setup and teardown contracts
test: cover concurrent journal recovery
```

A commit body is useful when the reason is not obvious from the diff. Explain **why the chosen behavior is correct**, not a line-by-line summary.

Do not spend time rewriting a useful commit history solely to satisfy a cosmetic rule.

## 12. Pull request conventions

Prefer one coherent purpose per PR.

A PR should make these easy to find:

- why the change exists;
- behavior/contract changed;
- validation performed;
- issue/roadmap relationship;
- intentionally out-of-scope work.

Use the closest purpose-specific template and remove irrelevant optional sections.

Architecture-changing PRs should identify changed ownership/boundaries. Research PRs should identify the evidence generated. Documentation PRs should state whether they describe current behavior or target direction.

## 13. Issue conventions

Issues should be decision-ready rather than exhaustive.

For most issues, enough information is:

```text
problem / desired outcome
context or reproduction
constraints / tradeoffs when relevant
acceptance or evidence criteria
```

Use sub-issues or follow-up issues rather than growing one implementation ticket into an unbounded backlog. Umbrella issues are appropriate for architecture/direction when they explicitly delegate concrete work.