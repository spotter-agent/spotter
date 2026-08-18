# Natural-failure capture protocol v4

**Frozen before execution:** 2026-08-18

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision posture:** remain **NO-GO** unless the predeclared evidence below justifies a narrower
qualification change.

## Question

Does one fresh, non-selected run of a new harder qualification split produce naturally occurring
mechanical control failures with complete replay-source capture, allowing failure-region neutral
repetitions without reusing or outcome-selecting the easy v2 cohorts?

## Frozen task split

`corpus/fidelity-validation-v1.toml` contains four previously unexecuted, runtime-inspired task
families:

- dependency ordering and transitive failure propagation;
- append-only durable-state recovery;
- security-sensitive endpoint validation and canonicalization;
- out-of-order lifecycle correlation and idempotency.

Every task uses only the Python standard library. Its checked-in negative fixture fails the required
scorer, while a manifest-owned known-good transform must pass the same scorer during preflight. The
split is intentionally harder and broader than the observed v2 fixtures, but remains a small
synthetic qualification corpus rather than a representative external benchmark.

No task may be removed, rewritten, or replaced after arm outcomes are observed. Corrections require
a new task and set version.

## Fixed execution

- task set: `corpus/fidelity-validation-v1.toml`;
- four frozen tasks / eight total task-batch arms;
- model: `gpt-5.6-sol`;
- reasoning effort: `low`;
- sandbox: `workspace-write`;
- replay-source capture: enabled;
- executions: exactly one batch, with no result-conditioned rerun.

The required guidance suffix is the non-directive sentence `Continue the task.`. Guidance arms have
a different full prompt and are therefore not neutral evidence or eligible natural-failure sources.
Only control outcomes answer the failure question; all arms answer the capture-coverage question.

## Pre-run gates

Before paid execution:

1. validate every task/set hash;
2. prove all four initial fixtures fail their required scorer;
3. prove every known-good transform passes the same scorer;
4. commit the corpus and this protocol;
5. install that committed worktree into an isolated experiment virtual environment;
6. use fresh, retained experiment `SPOTTER_HOME` and `CODEX_HOME` directories;
7. copy only the existing Codex authentication file into the isolated Codex home, without changing
   user configuration;
8. run `spotter setup codex --portable` and require capture readiness before the batch header or
   first model arm;
9. pin Codex/Spotter/model/config and the non-secret readiness receipt in the batch header.

The isolated portable daemon is stopped after capture. Raw batch, journal, snapshot, and source
artifacts remain local and are not committed.

## Selection and stop rule

After the one batch completes:

```text
eligible source = arm == control AND classification == TASK_FAIL
```

- If there are no eligible sources, publish the null result and stop. Do not rerun this protocol to
  search for a failure.
- For every eligible source, run `fork-coverage` and select its earliest `FORKABLE_EXACT`
  pre-mutation point.
- Exclude a source without a captured replay session, with an observation gap, with an external
  effect, or without exact context/state coverage.
- For each surviving source, run exactly three neutral pairs with its frozen required scorer,
  `gpt-5.6-sol`, and `low` reasoning effort.
- Do not replace an excluded or failed source with a passing source or another task.

## Report

Always report:

- all eight arm classifications and task families;
- control failure count and eligible-source identities;
- capture-readiness identity and replay-source coverage;
- setup, scorer, timeout, and infrastructure failures separately;
- raw batch SHA-256 and a bounded derived artifact;
- coverage/exclusion outcome for every eligible source;
- neutral-pair outcome disagreement, environment mismatch, and infrastructure failure separately;
- whether the stop rule prevented neutral forks;
- unchanged or revised qualification decision.

A zero-failure cohort is evidence about this fixed run, not evidence that natural failures do not
exist and not permission to close #42.
