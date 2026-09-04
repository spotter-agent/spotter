# External natural-failure v6 result — judgeable failure-region baseline

**Measured:** 2026-09-04

**Protocol:** [`fork-natural-failure-external-v6`](fork-natural-failure-external-v6-protocol.md)

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision:** **3/3 neutral pairs were judgeable with 0/3 mechanical disagreements; #42 remains
NO-GO for representative causal use**

## Outcome

All pre-run gates passed against the retained xarray source, exact step 2 prefix, environment
fingerprint, and byte-identical scorer. Exact Spotter commit
`703f3e6725c32e54f0b76a8066368c3b26b248cf` was installed in a fresh isolated Python 3.14.7
virtual environment.

All six agents exited 0. All six scorers completed host Git setup, entered the pinned Docker image,
installed the fork, and reached pytest. Every arm produced the same test-derived result: 12 failed,
946 passed, 2 skipped, 7 xfailed, and 2 xpassed. The accepted result is **3/3 judgeable pairs**,
**0/3 mechanical disagreements**, **0/6 infrastructure failures**, and **0/6 environment
mismatches**.

| Pair | Arm | Session | Agent | Scorer | Accepted result |
| ---: | --- | --- | ---: | ---: | --- |
| 0 | neutral_a | `8a365750-eb09-4c02-99dc-7ca3406434e2` | 0 | 1 | `TASK_FAIL` |
| 0 | neutral_b | `719aeccb-5bb6-40b1-b3c1-8a6ca41b69dc` | 0 | 1 | `TASK_FAIL` |
| 1 | neutral_b | `c234433b-8abb-4bef-bf3a-42696011390d` | 0 | 1 | `TASK_FAIL` |
| 1 | neutral_a | `849af00d-d03b-4dee-831b-c11e07f7a7ce` | 0 | 1 | `TASK_FAIL` |
| 2 | neutral_a | `e02a2a2d-bce0-43f0-9959-c11d349f713b` | 0 | 1 | `TASK_FAIL` |
| 2 | neutral_b | `4c9297c9-59de-4a07-b3b0-be15426096bc` | 0 | 1 | `TASK_FAIL` |

Agent elapsed time was 45.054–93.459 seconds and reported tokens were 17,276–26,638. Pytest time
was 15.45–18.14 seconds.

## Provenance and gates

- freeze PR [#360](https://github.com/spotter-agent/spotter/pull/360), merge commit
  `e2fc9d779f28a8fb2b18359171f2d2a2543c9adb`;
- experiment ID `0906b996-a3eb-4231-bcf5-ce261a4c70ba`;
- started `2026-09-04T05:20:52.306166+00:00`;
- finished `2026-09-04T05:33:04.788941+00:00`;
- pre-run raw SHA-256
  `a3fd56189ba50bdef627cf84f3ae81f906c9b8b6c46d3cabf4c57293fae04c2a`;
- post-run raw SHA-256
  `07796e5b7bfbdd9800f54e12dd6ed249045e77bab66e00ada655edfb379fc43a` over
  113,087 bytes;
- scorer SHA-256
  `fb3f0bfc21aa5e2e81a65f3a24f7dc93835abfc1e90921d9607d5d6c8f5f50b9` over
  9,778 bytes;
- prefix ID `2bbdb26e5dfaff2933c1a36b648be2e297f89ff7968648fb39f64f1a84db35af`;
- environment fingerprint
  `9e7b6c1caa72c01c69286891ba3c3eb48bd5af2f187b7198df3da6714c8ddaf4`;
- `FORKABLE_EXACT`: 1, earliest step: 2;
- Codex `0.153.1`, Docker `29.4.0`, model `gpt-5.6-sol`, effort `low`, sandbox
  `workspace-write`;
- all prefix/environment preflights matched, with zero observation gaps and external effects.

The append-only journal contains one v6 header, six arm rows, and one completion row. Git-aware
cleanup left no experiment worktree behind.

## Qualification impact

v6 establishes a zero-disagreement mechanical outcome baseline for one naturally occurring xarray
failure region at an exact pre-mutation prefix. It also demonstrates that the #351 metadata repair
survives the real paid host-to-container scoring path.

This does not establish a representative global noise bound. The sample contains one repository,
one failure region, and no labeled intervention opportunity. Broader independent failure-region
coverage, labeled-opportunity branch coverage, and natural drift evidence remain incomplete.
Therefore #42 stays open and remains **NO-GO** for representative causal use. The next evidence
slice should not rerun this xarray prefix.
