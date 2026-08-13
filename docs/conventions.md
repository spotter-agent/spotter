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
| Issues | Native Type + Priority + Effort + Area + Milestone + dependencies; labels exceptional |
| Roadmap | Named outcomes: Runtime → Observe → Detect → Intervene → Recover → Harden |

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
- evaluation labels / metrics metadata
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
- `docs/roadmap.md` — named stages, dependency order, and evidence gates
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

## 13. Issue metadata and triage

Prefer GitHub's structured issue metadata over label namespaces. Each dimension should answer a different recurring question; do not duplicate the same fact in labels or issue-body boilerplate.

### Issue Type — what kind of work is this?

Every maintained open issue should have one native Issue Type after triage.

| Type | Use |
| --- | --- |
| `Bug` | incorrect behavior, regression, or broken contract |
| `Feature` | new user- or developer-facing capability |
| `Architecture` | ownership, lifecycle, interface, state, or runtime-boundary decision |
| `Experiment` | research/evaluation whose primary output is evidence |
| `Task` | maintenance, documentation, tooling, packaging, community work, or other bounded work |

Do not create extra types merely to mirror every PR/branch category.

### Priority — what deserves attention next?

Every maintained open issue should normally have one Priority after triage.

| Priority | Meaning |
| --- | --- |
| `Urgent` | the current critical path or roadmap stage cannot meaningfully advance until this is resolved |
| `High` | current/immediately-next work or parallel evidence needed soon |
| `Medium` | planned work with no immediate sequencing pressure |
| `Low` | useful but distant, opportunistic, external, or community work |

Priority is intentionally **not** a roadmap stage. It may change as dependencies and evidence change.

Keep `Urgent` rare. If most open issues are `High`, priority has stopped helping choose work.

### Effort — how large/uncertain is the change?

Effort is a rough measure of **change surface, validation difficulty, and uncertainty**, not a promise about elapsed time.

| Effort | Meaning |
| --- | --- |
| `XS` | trivial/local change with obvious validation |
| `S` | one bounded component or simple external task |
| `M` | several modules/tests/docs or moderate uncertainty |
| `L` | crosses a runtime/architecture/E2E boundary |
| `XL` | broad or uncertain enough that splitting should be considered |

AI-assisted implementation can change wall-clock time dramatically; the scope and proof burden remain useful planning signals.

### Area — where does the problem primarily live?

Use one primary Area in most cases.

| Area | Scope |
| --- | --- |
| `Runtime` | `spotterd`, App Server integration, adapters, IPC, live runtime state |
| `Observation` | event ingestion, Trace IR, audit evidence, observability |
| `Detection` | signals, reviewer judgment, detection policy/quality |
| `Intervention` | VERIFY/NUDGE/BLOCK delivery, supervision UX/provenance |
| `Recovery` | interrupt, restart, snapshots, reversibility, side effects |
| `Evaluation` | task sets, experiments, replay measurement, metrics, statistics, A/B |
| `Operations` | install/setup/update, schemas, retention, cleanup, diagnostics |
| `Community` | OSS programs, showcases, ecosystem listings, outreach |

Area should stay relatively stable as roadmap priority changes.

### Milestone — which roadmap outcome owns completion?

Use the native GitHub Milestones:

```text
Runtime → Observe → Detect → Intervene → Recover → Harden
```

The Milestone is the stage whose completion/evidence gate needs the issue done. It does not mean the issue benefits only that stage. Cross-cutting infrastructure can therefore have an earlier Milestone than some of its consumers.

Community/outreach work does not need a roadmap Milestone when it is independent of product maturity.

The detailed stage meaning and evidence gates live in `docs/roadmap.md`; GitHub Milestones are the source of truth for issue-to-stage assignment.

### Dependencies — what is actually blocked?

Use GitHub's native `blocked by` / `blocking` relationships when an issue cannot meaningfully complete until another issue resolves.

Do not encode a dependency merely because:

- two issues are related;
- one would be convenient to do first;
- they share a Milestone or Area.

Native dependency state replaces a custom `status:blocked` label.

### Labels — exceptional signals only

Labels are no longer the primary issue taxonomy. Keep the standard contributor-discovery labels:

- `good first issue` — small and well-bounded for a new contributor;
- `help wanted` — maintainers explicitly welcome outside help.

Add a future label only when it expresses a recurring cross-cutting distinction that Type, Priority, Effort, Area, Milestone, dependencies, assignees, or issue state cannot represent cleanly.

Use GitHub's native open/closed state and close reasons for duplicate/not-planned outcomes instead of recreating status labels.

## 14. Issue conventions

Issues should be decision-ready rather than exhaustive.

For most issues, enough information is:

```text
problem / desired outcome
context or reproduction
constraints / tradeoffs when relevant
acceptance or evidence criteria
```

Use sub-issues or follow-up issues rather than growing one implementation ticket into an unbounded backlog. Umbrella issues are appropriate for architecture/direction when they explicitly delegate concrete work.

Use issue titles to describe the work itself. Do not prefix titles with roadmap codes such as `[P6]` or `[E4]`; sequencing belongs in the roadmap Milestone, Priority field, and native dependencies.
