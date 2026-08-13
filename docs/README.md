# Spotter Documentation

Start here if you are reading the repository for the first time.

## Which document should I read?

| Question | Document | What you get |
| --- | --- | --- |
| What works today, what is blocked, what happens next? | **[Status](status.md)** | implementation dashboard + immediate blocker |
| What problem is Spotter trying to solve? | [Concept](concept.md) | mental model, principles, intervention semantics |
| What processes/components/state should exist? | [Architecture](architecture.md) | component, state, IPC, and failure contracts |
| What happens from install through uninstall/reinstall? | [Lifecycle](lifecycle.md) | command-by-command operational lifecycle |
| What did the App Server proof of concept establish? | [App Server PoC](app-server-poc.md) | exploratory protocol and transport findings |
| Was shared TUI control validated end to end? | [App Server validation](app-server-validation.md) | historical validation evidence and limitations |
| How is source/Trace IR/ThreadState coverage measured? | [Observability baseline](observability-baseline.md) | taxonomy, safe audit method, current sample limits |
| How are task sets validated and experiment results classified? | [Task corpus](../corpus/README.md) | frozen-set commands, scorer safety, and arm-result eligibility |
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

## Current architectural decision in one screen

```text
CURRENT
Codex hooks
   ↓
per-hook Spotter processes
   ↓
journal / gate / snapshot / periodic shadow reviewer

TARGET
Codex TUI
   ↓
External Codex App Server
   ↕ events / steer / interrupt
spotterd
   ↓
PreToolUse Hook only for deterministic blocking
```

The initial validation found that plain `codex` does not auto-discover a separately started App Server. [#78](https://github.com/spotter-agent/spotter/issues/78) subsequently proved the explicit remote path: the user's Codex TUI and Spotter can share a Spotter-managed external App Server, and `turn/steer` reaches the real active turn. A production App Server client, identity-rich normalized ingestion, and daemon-owned reconnect/reconciliation now exist for configured endpoints. Setup endpoint selection and the remaining ordinary-use lifecycle work are still pending. See [App Server connection validation](app-server-validation.md) and [Status](status.md).

## Roadmap vocabulary

The project intentionally does not use numeric phase codes as the primary vocabulary.

```text
Runtime → Observe → Detect → Intervene → Recover → Harden
```

These names describe the product/evidence outcome being made trustworthy. GitHub Milestone carries the stage assignment; Priority, Effort, Area, and native dependencies answer separate triage questions.

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
