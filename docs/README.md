# Spotter Documentation

Start here if you are reading the repository for the first time.

## Which document should I read?

| Question | Document | What you get |
| --- | --- | --- |
| What works today, what is blocked, what happens next? | **[Status](status.md)** | implementation dashboard + immediate blocker |
| What problem is Spotter trying to solve? | [Concept](concept.md) | mental model, principles, intervention semantics |
| What processes/components/state should exist? | [Architecture](architecture.md) | component, state, IPC, and failure contracts |
| What happens from install through uninstall/reinstall? | [Lifecycle](lifecycle.md) | command-by-command operational lifecycle |
| How are release artifacts built and identified? | [Releasing](releasing.md) | tag, artifact, checksum, and build-identity contract |
| What does the packaged lifecycle smoke prove? | [Homebrew lifecycle smoke](homebrew-lifecycle-smoke.md) | recorded install, live-upgrade, uninstall, retention, and reinstall coverage |
| What did the App Server proof of concept establish? | [App Server PoC](app-server-poc.md) | exploratory protocol and transport findings |
| Was shared TUI control validated end to end? | [App Server validation](app-server-validation.md) | historical validation evidence and limitations |
| How is source/Trace IR/ThreadState coverage measured? | [Observability baseline](observability-baseline.md) | taxonomy, safe audit method, current sample limits |
| What should become trustworthy next? | [Roadmap](roadmap.md) | Runtime → Observe → Detect → Intervene → Recover → Harden, with evidence gates |
| What prior papers/systems should I study, and what does Spotter borrow from them? | [Reference](reference.md) | literature + implementation precedents, boundaries, and implementation hints |
| What hypotheses remain unproven, and how will Spotter evaluate them? | [Research](research.md) | evidence posture, research questions, experiments, and metrics |
| How should code, issue metadata, branches, commits, tests, and docs be structured? | [Conventions](conventions.md) | repository-wide working conventions |
| How do I contribute? | [Contributing](../CONTRIBUTING.md) | issue/PR workflow, setup, triage, and review expectations |

## Recommended reading paths

### I only want the current project picture

```text
Status
  ↓
Roadmap → current named stage
```

### I want to implement the standalone runtime

```text
Status
  ↓
Architecture
  ↓
Lifecycle
  ↓
Reference → implementation precedents as needed
  ↓
Roadmap → Runtime / Observe
```

### I want to evaluate the research hypothesis

```text
Concept
  ↓
Reference
  ↓
Research
  ↓
Roadmap → evidence gate for the relevant stage
  ↓
Status → Evidence status
```

### I want to contribute code or docs

```text
Contributing
  ↓
Conventions
  ↓
Status / Architecture as needed
```

## Current architecture and implementation

[Status](status.md) is the authoritative current-state dashboard. Read it with
[Architecture](architecture.md) for the current/target boundary and with the historical
[App Server validation](app-server-validation.md) for what was demonstrated in a recorded real
session. This index intentionally does not maintain a second capability summary.

## Roadmap vocabulary

The project intentionally does not use numeric phase codes as the primary vocabulary.

```text
Runtime → Observe → Detect → Intervene → Recover → Harden
```

These names describe the product/evidence outcome being made trustworthy. GitHub Milestone carries the stage assignment; Priority, Effort, Area, and native dependencies answer separate triage questions.

Legacy `P1`/`P3`/`P4` headings in `spotter metrics` name historical measurements retained for
output compatibility; they are not roadmap or issue-triage codes.

## Document responsibilities

To avoid maintaining duplicate specifications:

- **Status** owns “where are we now?”
- **Concept** owns “what problem and principles define Spotter?”
- **Architecture** owns runtime components, process/data flow, state ownership, IPC, and failure contracts.
- **Lifecycle** owns package/service/integration/session/update/removal behavior.
- **Roadmap** owns named stages, dependencies, and evidence gates.
- **Reference** owns prior papers/systems, what they demonstrate, implementation precedents, and what Spotter should or should not borrow.
- **Research** owns Spotter's hypotheses, evidence state, evaluation questions, experiment design, and metrics.
- **Conventions** owns repository-wide code/test/git/issue-metadata/documentation conventions.
- **Contributing** owns the human contribution workflow.

When a detail belongs to another document, link to it rather than maintaining two competing definitions.
