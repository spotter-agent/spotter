# Natural-failure capture protocol v2

**Frozen before execution:** 2026-08-17

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision posture:** remain **NO-GO** unless the predeclared evidence below justifies a narrower
qualification change.

## Question

Does one fresh, non-selected run of the frozen validation-v2 controls produce a naturally occurring
mechanical failure that can seed failure-region neutral replay measurement?

## Fixed cohort

- task set: `corpus/validation-v2.toml`;
- three frozen tasks / six total task-batch arms;
- model: `gpt-5.6-sol`;
- reasoning effort: `low`;
- sandbox: `workspace-write`;
- replay-source capture: enabled;
- executions: exactly one batch, with no result-conditioned rerun.

The batch runner requires a non-empty guidance arm. Its guidance suffix for this capture is the
non-directive sentence `Continue the task.`. Because that produces a different full prompt from the
control arm, **guidance arms are not neutral evidence and are not eligible natural-failure sources**.
Only control-arm outcomes answer this protocol's question.

## Pre-run gates

Before paid execution:

1. validate the frozen task-set hashes;
2. execute every setup/precheck/known-good scorer preflight;
3. commit this protocol;
4. use a fresh isolated `SPOTTER_HOME` and retain its raw batch/source artifacts locally;
5. record Codex/Spotter/model/config provenance in the batch header.

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
- replay-source capture coverage;
- setup/preflight/infrastructure failures separately;
- raw batch SHA-256 and bounded derived artifact;
- whether the stop rule prevented neutral forks;
- unchanged or revised qualification decision.

A zero-failure cohort is evidence about this fixed run, not evidence that natural failures do not
exist and not permission to close #42.
