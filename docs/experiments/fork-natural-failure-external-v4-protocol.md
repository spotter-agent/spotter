# External natural-failure successor protocol v4

**Frozen before paid execution:** 2026-09-04

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Predecessor:** [external natural-failure v3 result](fork-natural-failure-external-v3-result.md)

**Decision posture:** this is an infrastructure-only continuation of the v3 stop rule. It may
measure the retained failure-region noise floor, but it may not select a new task, prefix, scorer,
or arm after observing an outcome.

## Question

After the worktree lifetime and isolated-home fix in
[#353](https://github.com/spotter-agent/spotter/pull/353), do the three neutral pairs already
prescribed by v3 remain valid through their frozen scorer and produce judgeable outcomes?

## Frozen source and scorer

No task-batch arm is rerun. v4 reuses the only source admitted by the predeclared v3 rule:

| Field | Frozen value |
| --- | --- |
| Task | `swebench/verified/pydata__xarray-6992` |
| Source session | `01a06a49-d733-7b43-9d07-e60540da3138` |
| Branch step | `2` |
| Prefix ID | `2bbdb26e5dfaff2933c1a36b648be2e297f89ff7968648fb39f64f1a84db35af` |
| Snapshot | `328539437225b6153d1ad9ae3dec374abf949262` |
| Snapshot tree | `c325c914bfff5bd2face1a7e641cfcece6b0a3e1` |
| Rollout-prefix SHA-256 | `4a7b1cb2735f9c5a75af43be6ca848bd4190c42d94dd43fbde9d4fa8ba104960` |
| Environment fingerprint | `9e7b6c1caa72c01c69286891ba3c3eb48bd5af2f187b7198df3da6714c8ddaf4` |
| Scorer command SHA-256 | `fb3f0bfc21aa5e2e81a65f3a24f7dc93835abfc1e90921d9607d5d6c8f5f50b9` |
| Scorer command bytes | `9,778` |

The scorer command is copied byte-for-byte from the v3 neutral result header. It restores the
three official xarray test files, reapplies the pinned test patch, and runs the same immutable
SWE-bench image and pytest scope. Its text, timeout, Docker image, and test scope may not change.

The retained pre-v4 neutral journal has SHA-256
`cc48342e0be66f93f24a475808440c91ec9bf6fb17f1f90986b08a7779f2f0e1`. Its invalid v3 rows
remain append-only history and are not counted as v4 outcomes.

## Allowed implementation delta

The sole intentional implementation change is Spotter merge commit
`7abbc36d14d2da0c0f803cfca86b80b9df97cbaa` from #353:

- rollout discovery honors the isolated `CODEX_HOME`;
- every executed fork is held under a native Git worktree lock until scoring finishes;
- a missing Git worktree immediately before scoring is recorded as `INFRA_FAIL`.

Model, prompt, source state, prefix reconstruction, environment fingerprinting, scorer, and arm
count remain unchanged from v3.

## Pre-run gates

Paid execution may begin only after this freeze merges. In the retained isolated root:

1. require the source repository, rollout, journal, snapshot, and authentication to remain present;
2. verify the existing neutral journal SHA-256 shown above before appending;
3. run isolated `spotter fork-coverage` and require earliest step `2` with exactly one
   `FORKABLE_EXACT` point;
4. install merged Spotter commit `7abbc36d14d2da0c0f803cfca86b80b9df97cbaa` into a fresh isolated
   virtual environment;
5. verify Codex `0.153.1`, Docker `29.4.0`, the frozen scorer hash, and the source environment
   fingerprint;
6. require no observation gap, external effect, environment mismatch, or source-config mismatch.

Failure of any gate stops the run without replacement or repair and is published as
infrastructure coverage.

## Fixed paid execution

- exactly three neutral pairs / six continuations;
- prompt `Continue the task.` for both arms;
- model `gpt-5.6-sol`;
- reasoning effort `low`;
- sandbox `workspace-write`;
- timeout 1,800 seconds per agent and scorer;
- the frozen scorer command above;
- no result-conditioned retry.

The run appends one new experiment header, six arm rows, and one completion row to the retained
journal. Only rows belonging to that new experiment ID are v4 evidence.

## Admission and stop rule

A neutral pair is judgeable only when both arms:

- pass prefix and environment preflight;
- exit the agent with code 0;
- retain a valid Git worktree through scorer start;
- reach the frozen pytest process; and
- finish as `PASS` or a test-derived `TASK_FAIL`.

Git, Docker, image, setup, timeout, or scorer-launch failures remain infrastructure failures even
if a shell exit code is available. Do not reinterpret the invalid v3 rows or add replacement pairs.

Report mechanical disagreement, environment mismatch, and infrastructure failure separately. If
all three pairs are judgeable, combine them only with the existing pinned-low natural-failure
stratum; do not pool them into passing-prefix or unpinned-effort cohorts.

## Required report

Publish the freeze PR and merge commit, pre-run gate evidence, new experiment ID, exact six arm
classifications, whether pytest started, raw journal post-run SHA-256, environment and prefix
parity, infrastructure rate, mechanical outcome disagreement, token/time range, and the updated
#42 qualification decision.

This successor can measure one naturally occurring xarray failure region. It cannot establish
benchmark-wide replay fidelity, intervention benefit, or production readiness.
