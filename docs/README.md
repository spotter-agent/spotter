# Spotter Documentation

Start here if you are reading the repository for the first time.

## Which document should I read?

| Question | Document | What you get |
| --- | --- | --- |
| What works today, what is blocked, what happens next? | **[Status](status.md)** | implementation dashboard + immediate blocker |
| What problem is Spotter trying to solve? | [Concept](concept.md) | mental model, principles, intervention semantics |
| What processes/components/state should exist? | [Architecture](architecture.md) | component, state, IPC, and failure contracts |
| What happens from install through uninstall/reinstall? | [Lifecycle](lifecycle.md) | command-by-command operational lifecycle |
| What should become trustworthy next? | [Roadmap](roadmap.md) | Runtime → Observe → Detect → Intervene → Recover → Harden, with evidence gates |
| Which papers/ideas inform the design, and what remains unproven? | [Research](research.md) | literature-to-mechanism map + evidence questions |
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
Roadmap → Runtime / Observe
```

### I want to evaluate the research hypothesis

```text
Concept
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

The target is **conditional on P0**: [validation](app-server-validation.md) found that
plain `codex` does not auto-discover a separately started App Server. Spotter must now
prove that an explicitly remote-connected TUI and Spotter can share the server and that
`turn/steer` reaches the real active user turn.
[#78](https://github.com/spotter-agent/spotter/issues/78) proved that the user's Codex TUI and
Spotter can share a Spotter-managed external App Server and that `turn/steer` reaches the real
active turn. The identity foundation is implemented; daemon ownership, event routing, and reconnect
remain explicit Runtime work.

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
- **Research** owns prior work, hypotheses, evidence, and evaluation questions.
- **Conventions** owns repository-wide code/test/git/issue-metadata/documentation conventions.
- **Contributing** owns the human contribution workflow.

When a detail belongs to another document, link to it rather than maintaining two competing definitions.
