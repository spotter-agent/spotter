# Spotter Documentation

Start here if you are reading the repository for the first time.

## Which document should I read?

| Question | Document | What you get |
| --- | --- | --- |
| What works today, what is blocked, what happens next? | **[Status](status.md)** | implementation dashboard + immediate blocker |
| What problem is Spotter trying to solve? | [Concept](concept.md) | mental model, principles, intervention semantics |
| What processes/components/state should exist? | [Architecture](architecture.md) | component, state, IPC, and failure contracts |
| What happens from install through uninstall/reinstall? | [Lifecycle](lifecycle.md) | command-by-command operational lifecycle |
| What should be implemented first, and what gates each phase? | [Roadmap](roadmap.md) | dependency graph, deliverables, exit criteria |
| Which papers/ideas inform the design, and what remains unproven? | [Research](research.md) | literature-to-mechanism map + evidence gates |
| How should code, branches, commits, tests, and docs be structured? | [Conventions](conventions.md) | repository-wide working conventions |
| How do I contribute? | [Contributing](../CONTRIBUTING.md) | issue/PR workflow, setup, and review expectations |
| What is the umbrella direction decision? | [Issue #66](https://github.com/spotter-agent/spotter/issues/66) | design rationale and ongoing decisions |

## Recommended reading paths

### I only want the current project picture

```text
Status
  ↓
Roadmap → Now / Next
```

### I want to implement the standalone runtime

```text
Status
  ↓
Architecture
  ↓
Lifecycle
  ↓
Roadmap
  ↓
#66
```

### I want to evaluate the research hypothesis

```text
Concept
  ↓
Research
  ↓
Roadmap → Evaluation track
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

The target is **conditional on P0**: Spotter must prove that the user's ordinary Codex TUI and Spotter can share the same external App Server and that `turn/steer` reaches the real active user turn.

If that does not work reliably, the target architecture is revisited before the daemon migration continues.

## Document responsibilities

To avoid maintaining duplicate specifications:

- **Status** owns “where are we now?”
- **Concept** owns “what problem and principles define Spotter?”
- **Architecture** owns runtime components, process/data flow, state ownership, IPC, and failure contracts.
- **Lifecycle** owns package/service/integration/session/update/removal behavior.
- **Roadmap** owns implementation dependencies, deliverables, and exit criteria.
- **Research** owns prior work, hypotheses, evidence, and evaluation questions.
- **Conventions** owns repository-wide code/test/git/documentation conventions.
- **Contributing** owns the human contribution workflow.

When a detail belongs to another document, link to it rather than maintaining two competing definitions.
