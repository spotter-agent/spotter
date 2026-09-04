# External natural-failure successor protocol v5

**Frozen before paid execution:** 2026-09-04

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Predecessor:** [external natural-failure v4 result](fork-natural-failure-external-v4-result.md)

**Decision posture:** remain **NO-GO** unless this fixed successor produces judgeable neutral
outcomes. No v3/v4 infrastructure row is a task outcome.

## Question

Does the metadata preservation fix in [#356](https://github.com/spotter-agent/spotter/pull/356)
keep the retained natural-failure forks valid through the frozen scorer, allowing the three neutral
pairs originally prescribed by v3 to measure mechanical outcome disagreement?

## Frozen inputs

v5 changes no task, selection, prefix, environment, scorer, prompt, model, effort, sandbox, timeout,
or arm count from v4:

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
| Pre-v5 raw SHA-256 | `5158c162d335b0987f6b00cfe0e03372d79064f0bb35ae783f45a3948aa7ff7a` |

The prior journal remains append-only. Only rows belonging to the new v5 experiment ID count.

## Sole implementation delta

Use Spotter merge commit `eb891034e986aa69bd98f00a2e070584b2a8f6eb` from #356. Relative to v4 it:

- backs up linked-worktree administration before Codex starts;
- gives the scorer a self-contained metadata copy after Codex returns; and
- restores the registered linked-worktree form for Git-aware cleanup.

The real retained-source diagnostic session `aa7941d5-fa71-4ce5-afc0-b225ccf21fe6` reached agent
exit 0 and post-teardown `git status` exit 0, then cleaned up through Git. It is implementation
validation only and is not a neutral outcome.

## Pre-run gates

Paid execution may begin only after this freeze merges:

1. install exact Spotter commit `eb891034e986aa69bd98f00a2e070584b2a8f6eb` in a fresh isolated venv;
2. verify the pre-v5 journal and scorer hashes above;
3. require the retained source, rollout, snapshot, and authentication;
4. require isolated coverage to report exactly one `FORKABLE_EXACT` and earliest step 2;
5. require Codex `0.153.1`, Docker `29.4.0`, matching source model/effort, prefix, and environment;
6. stop without replacement on any failed gate.

## Fixed execution and stop rule

Run exactly three neutral pairs / six continuations with prompt `Continue the task.`, model
`gpt-5.6-sol`, effort `low`, sandbox `workspace-write`, timeout 1,800 seconds, and the byte-identical
v3/v4 scorer. There is no result-conditioned retry or replacement.

A pair is judgeable only when both agents exit 0, both scorers reach pytest, and both finish as
`PASS` or test-derived `TASK_FAIL`. Git, Docker, setup, image, timeout, and scorer-launch failures
remain infrastructure failures regardless of the raw shell exit code.

Publish pre-run gates, the new experiment ID, all six outcomes, pytest-start coverage, raw post-run
hash, prefix/environment parity, infrastructure rate, mechanical disagreement, token/time range,
and the updated #42 decision.

This run covers one naturally occurring xarray failure region only. It cannot establish
benchmark-wide fidelity or intervention benefit.
