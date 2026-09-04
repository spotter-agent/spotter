# External natural-failure v4 result — locked metadata still removed at teardown

**Measured:** 2026-09-04

**Protocol:** [`fork-natural-failure-external-v4`](fork-natural-failure-external-v4-protocol.md)

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision:** **the infrastructure-only successor remained 0/3 judgeable; #351 is reopened and
#42 remains NO-GO**

## Outcome

Every pre-run gate passed. The retained source, exact step 2 prefix, snapshot, environment
fingerprint, and byte-identical scorer were used with Spotter commit
`7abbc36d14d2da0c0f803cfca86b80b9df97cbaa` from #353.

All six continuations exited normally. All six scorers then failed on their first `git checkout`
with exit 128 because the fork's `.git` file pointed at a missing
`<source>/.git/worktrees/<session-id>` directory. Pytest never started.

The raw runner again classified those rows as `TASK_FAIL` and summarized 3/3 judgeable tied pairs.
That summary is rejected: v4 produced **0/3 judgeable pairs**, no disagreement estimate, and
**6/6 infrastructure failures**.

## Pre-run gates

| Gate | Result |
| --- | --- |
| Freeze PR | [#354](https://github.com/spotter-agent/spotter/pull/354), merged as `c2b088b9b0044f226eb70e5891f52c8fc8c1d531` |
| Spotter implementation | `7abbc36d14d2da0c0f803cfca86b80b9df97cbaa` installed from Git in a fresh isolated venv |
| Pre-run raw SHA-256 | `cc48342e0be66f93f24a475808440c91ec9bf6fb17f1f90986b08a7779f2f0e1` |
| Scorer SHA-256 | `fb3f0bfc21aa5e2e81a65f3a24f7dc93835abfc1e90921d9607d5d6c8f5f50b9` over 9,778 bytes |
| Source / rollout / snapshot | present and resolvable |
| Isolated branch coverage | one `FORKABLE_EXACT`; earliest step 2 |
| Codex / Docker | `0.153.1` / `29.4.0` |

## Neutral execution

- experiment ID: `2ccf231f-9625-4509-a500-9c4f12c79467`;
- started: `2026-09-04T04:32:38.153588+00:00`;
- finished: `2026-09-04T04:39:28.807683+00:00`;
- prefix ID: `2bbdb26e5dfaff2933c1a36b648be2e297f89ff7968648fb39f64f1a84db35af`;
- environment fingerprint:
  `9e7b6c1caa72c01c69286891ba3c3eb48bd5af2f187b7198df3da6714c8ddaf4`;
- post-run raw SHA-256:
  `5158c162d335b0987f6b00cfe0e03372d79064f0bb35ae783f45a3948aa7ff7a`.

| Pair | Arm | Session | Agent | Scorer | Accepted result |
| ---: | --- | --- | ---: | ---: | --- |
| 0 | neutral_b | `10951ce1-6cf4-4a56-b8a4-c9c173c1c5f1` | 0 | 128 | infrastructure failure |
| 0 | neutral_a | `e5f7e613-14f2-4a56-a384-a282dd1969bc` | 0 | 128 | infrastructure failure |
| 1 | neutral_a | `f44647cf-49b6-4874-9676-9894c63769e8` | 0 | 128 | infrastructure failure |
| 1 | neutral_b | `c3989af1-1499-4e64-a9c3-e901bd4f387e` | 0 | 128 | infrastructure failure |
| 2 | neutral_b | `e4367ff2-ac24-4d92-9aca-8b231d062dcc` | 0 | 128 | infrastructure failure |
| 2 | neutral_a | `9ff037f5-0f02-4ebf-8067-dfd5996ba8fc` | 0 | 128 | infrastructure failure |

All six arms had matching prefix and environment fingerprints. Continuations took
48.663–82.980 seconds and reported 16,401–33,010 tokens.

## New lifecycle evidence

The #353 Git lock was present during execution. For pair 1 neutral_a,
`git worktree list --porcelain` reported `locked spotter-experiment` while Codex was active. The
rollout recorded task completion at `2026-09-04T04:36:28.369Z`; a 100 ms filesystem probe then
observed the locked admin directory disappear by `04:36:30Z`.

The agent's last Git command had succeeded, and no agent command removed the worktree. The native
Git lock therefore does not cover this asynchronous/raw teardown. The pre-scorer `rev-parse` guard
also raced: it passed immediately before the metadata disappeared.

[#351](https://github.com/spotter-agent/spotter/issues/351) was reopened with this evidence. The
next fix must preserve or reconstruct the linked-worktree admin metadata across Codex process
teardown and treat unsuccessful reconstruction as infrastructure before starting the scorer.

## Qualification impact

v4 changes no replay-noise estimate. The only accepted evidence remains that v3 captured a natural
xarray control failure with one exact pre-mutation prefix. No neutral scorer has reached pytest in
that failure region.

Therefore #42 remains **NO-GO** for representative causal use. No v4 row may be pooled as a
mechanical task outcome.
