# Contributing to Spotter

Thanks for contributing. Spotter is still a research-oriented runtime project, so small, well-scoped changes with explicit validation are especially valuable.

## Before you start

Use the repository docs to avoid solving the wrong layer of the problem:

- [`docs/status.md`](docs/status.md) — what exists now and what is blocked
- [`docs/architecture.md`](docs/architecture.md) — runtime boundaries and component contracts
- [`docs/lifecycle.md`](docs/lifecycle.md) — install/setup/update/remove behavior
- [`docs/roadmap.md`](docs/roadmap.md) — named roadmap stages and evidence gates
- [`docs/conventions.md`](docs/conventions.md) — code, issue metadata, branch, commit, test, and documentation conventions

For agent-assisted work, also read [`AGENTS.md`](AGENTS.md).

## Pick the right workflow

### Small bug / clear maintenance change

Open a PR directly if the scope and expected behavior are obvious. Link an existing issue when one exists.

### Feature or behavioral change

Prefer an issue first when the change introduces a new user-facing behavior, runtime capability, configuration surface, or non-trivial tradeoff.

### Architecture change

Open an architecture issue before implementation when the change alters process ownership, observation/control/enforcement boundaries, durable-state contracts, lifecycle semantics, or core adapter interfaces.

### Research / experiment

Open an experiment issue when the main output is evidence rather than product behavior. Define the hypothesis, comparison, metric, and success/failure interpretation before running the experiment.

### Documentation-only change

A dedicated issue is optional unless the documentation change represents a new project decision.

## Issue triage

Spotter uses GitHub's native issue metadata instead of label prefixes.

For maintained open issues, triage with:

- **Type** — `Bug`, `Feature`, `Architecture`, `Experiment`, or `Task`;
- **Priority** — `Urgent`, `High`, `Medium`, or `Low`;
- **Effort** — `XS`, `S`, `M`, `L`, or `XL`;
- **Area** — one primary product/problem domain;
- **Milestone** — the roadmap stage that owns the issue's completion gate, when applicable;
- **Dependencies** — GitHub `blocked by` / `blocking` relationships only for genuine blockers.

Documentation, maintenance, tooling, packaging, and community chores normally use `Task`. Issue forms shape the requested information but do not recreate type/priority/area labels.

The roadmap is intentionally named:

```text
Runtime → Observe → Detect → Intervene → Recover → Harden
```

Labels are exceptional rather than a second metadata system. Keep `good first issue` and `help wanted` for contributor discovery; add another label only when native fields, milestone, dependencies, or issue state cannot express a recurring useful distinction.

Detailed semantics are in [`docs/conventions.md`](docs/conventions.md#13-issue-metadata-and-triage).

## Local setup

Spotter requires Python 3.11+.

```bash
git clone https://github.com/spotter-agent/spotter.git
cd spotter
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

Run the full local checks with:

```bash
ruff format --check .
ruff check .
mypy src tests
pytest
```

Release artifacts are built only from an exact version tag. See
[`docs/releasing.md`](docs/releasing.md) for the artifact, checksum, and build-identity contract.

Use focused tests during iteration, but run the relevant full checks before requesting review whenever practical.

## Pull requests

Keep PRs narrow enough that a reviewer can understand the behavioral change without reconstructing unrelated work.

A good PR answers four questions quickly:

1. **Why is this change needed?**
2. **What behavior or contract changed?**
3. **How was it validated?**
4. **What remains intentionally out of scope?**

Purpose-specific templates live under [`.github/PULL_REQUEST_TEMPLATE/`](.github/PULL_REQUEST_TEMPLATE/). Use the closest match; delete sections that genuinely do not apply.

Available templates:

- feature
- bugfix
- documentation
- refactor
- experiment / research
- maintenance

Do not pad a PR description just to fill a template. Short, specific answers are better than boilerplate.

## Issues

The issue picker provides purpose-specific forms for:

- bug reports
- feature requests
- architecture / design changes
- research / experiments
- documentation
- maintenance / chores

Blank issues remain available when none of the forms fit. The forms are intentionally lightweight; add detail only where it helps someone make a decision or reproduce a result.

## Review expectations

Review should focus on contracts and evidence, not stylistic preference.

For code changes, reviewers should be able to determine:

- whether the change is in the right component/layer;
- whether failure/degraded behavior is explicit;
- whether durable state and Git resources remain safe;
- whether synchronous paths remain bounded;
- whether tests cover the behavior that changed;
- whether docs/status accurately describe the new implementation state.

Research claims need stronger evidence than implementation claims. “The mechanism works” and “the mechanism improves task outcomes” are different statements.

## Compatibility and migrations

Avoid silent breaking changes. If a change affects configuration, journal/schema formats, plugin/setup behavior, or persisted state:

- document the compatibility boundary;
- provide a migration or explicit refusal path where necessary;
- update lifecycle/convention docs as part of the same PR.

## Security and sensitive reports

Do not put credentials, private logs, tokens, or exploitable secrets into public issues or PRs. If a future private vulnerability-reporting channel is configured, use it for security-sensitive disclosures rather than a public issue.
