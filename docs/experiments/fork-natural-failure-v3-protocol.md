# Natural-failure capture protocol v3

**Frozen before execution:** 2026-08-18

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision posture:** remain **NO-GO** unless the predeclared evidence below justifies a narrower
qualification change.

## Question

After the capture-readiness guard added for #307, does one fresh, non-selected run of the frozen
development-v2 controls produce usable replay sources and a naturally occurring mechanical failure
that can seed failure-region neutral replay measurement?

## Fixed cohort

- task set: `corpus/dev-v2.toml`;
- three frozen tasks / six total task-batch arms;
- model: `gpt-5.6-sol`;
- reasoning effort: `low`;
- sandbox: `workspace-write`;
- replay-source capture: enabled;
- executions: exactly one batch, with no result-conditioned rerun.

This cohort adds three task families not present in the v2 capture attempt: localized bug fix,
missing validation, and regression avoidance. The batch runner requires a non-empty guidance arm.
Its guidance suffix is the non-directive sentence `Continue the task.`. Because that produces a
different full prompt from the control arm, **guidance arms are not neutral evidence and are not
eligible natural-failure sources**. Only control-arm outcomes answer this protocol's failure
question; all arms answer the capture-coverage question.

## Pre-run gates

Before paid execution:

1. validate the frozen task-set hashes;
2. execute every setup/precheck/known-good scorer preflight;
3. commit this protocol;
4. install the committed worktree into an isolated experiment virtual environment;
5. use fresh, retained experiment `SPOTTER_HOME` and `CODEX_HOME` directories;
6. copy only the existing Codex authentication file into the isolated Codex home, without changing
   the user's Codex configuration;
7. run `spotter setup codex --portable` in those isolated homes and require capture readiness to
   pass before the task-batch header or first paid arm;
8. record Codex/Spotter/model/config and non-secret capture-readiness provenance in the batch
   header.

The isolated portable daemon is stopped after capture. Raw batch, journal, snapshot, and source
artifacts remain local under the retained experiment home and are not committed.

## Selection and stop rule

After the one batch completes:

```text
eligible source = arm == control AND classification == TASK_FAIL
```

- If there are no eligible sources, publish the null result and stop. Do not rerun this protocol to
  search for a failure.
- If one or more eligible sources exist, run `fork-coverage` for each source and select its earliest
  `FORKABLE_EXACT` pre-mutation point.
- Exclude any source without a captured replay session, with an observation gap, with an external
  effect, or without exact context/state coverage.
- For each surviving source, run exactly three neutral pairs with the task's frozen required scorer,
  pinned to the same model and reasoning effort.

## Report

Always report:

- all six arm classifications;
- control failure count and task families;
- replay-source capture coverage and readiness receipt identity;
- setup/preflight/infrastructure failures separately;
- raw batch SHA-256 and bounded derived artifact;
- whether the stop rule prevented neutral forks;
- unchanged or revised qualification decision.

A zero-failure cohort is evidence about this fixed run, not evidence that natural failures do not
exist and not permission to close #42.
