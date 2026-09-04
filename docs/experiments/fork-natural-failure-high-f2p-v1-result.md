# Highest-F2P natural-failure v1 result — nonzero replay noise

**Measured:** 2026-09-04

**Protocol:** [`fork-natural-failure-high-f2p-v1`](fork-natural-failure-high-f2p-v1-protocol.md)

**Issue:** [#365](https://github.com/spotter-agent/spotter/issues/365)

**Decision:** **the second natural-failure region produced 1/3 mechanical disagreements; #42
remains NO-GO for representative causal use**

## Capture outcome

The fixed batch completed all six arms with no source, image, setup, scorer, or capture failure:

```text
task batch: 6 arm(s), 3 task(s)
control: PASS=2, TASK_FAIL=1
guidance: PASS=2, TASK_FAIL=1
pairs: n=3/3 mechanically judgeable; guidance better=0, control better=0, tied=3
replay sources: 6/6 captured
```

| Task | Scorer scope | Control | Guidance | Replay sources |
| --- | --- | --- | --- | ---: |
| `django__django-14011` | official module scope | `TASK_FAIL` | `TASK_FAIL` | 2/2 |
| `pylint-dev__pylint-4551` | official whole-file scope | `PASS` | `PASS` | 2/2 |
| `django__django-16560` | official module scope | `PASS` | `PASS` | 2/2 |

Every agent exited 0 and every setup completed. Both `django-14011` task-batch arms ran 20 tests
and failed only `servers.tests.LiveServerTestCloseConnectionTest.test_closes_connections`;
Pylint ran 18 passing tests in each arm; `django-16560` ran 128 passing tests with 54 skips in each
arm. The guidance arms are execution coverage only and are not intervention-effect evidence.

## Admission and branch point

The captured `django-14011` control `TASK_FAIL` was the only eligible source. Its coverage map had
12 candidate points: one `FORKABLE_EXACT`, eleven `UNSAFE_EXTERNAL_EFFECT`, and no state, context,
or observation-gap failure. The sole exact point was the earliest point and was before the first
mutation:

| Field | Frozen value |
| --- | --- |
| Source session | `01a06b83-34e5-79c3-a9c3-6af023a14c88` |
| Branch step | `2` |
| Prefix ID | `f71fc19d1c27dc5ec30309c4ea8d63a2fefebb78dd4ceab7c07052ae1d534d6b` |
| Snapshot | `af4e2f2443e7627f0ebb3b9ada6ade07bca3000e` |
| Snapshot tree | `797a7ad0b41b7312e7285bf45387a3e21186f434` |
| Rollout-prefix SHA-256 | `dca822b8f532d2817507e0607850fa9c8c37b8e994e5a593b61f32791e43c16b` |
| Environment fingerprint | `e0e29adeff86b52736b8b1631fd4ccb83c505eb300a1a02e317f73f04fb49f70` |

The selected point itself carried no external effect or observation gap. All six forks matched the
same prefix and environment fingerprint before continuation.

## Neutral outcome

All six neutral agents exited 0 and all six scorers reached the pinned Docker test suite. Five arms
passed; one arm reproduced the same single failing test as the source:

| Pair | Arm | Session | Agent | Scorer | Accepted result |
| ---: | --- | --- | ---: | ---: | --- |
| 0 | `neutral_b` | `90874c83-257f-4121-9aa0-0f7be9a4977d` | 0 | 0 | `PASS` |
| 0 | `neutral_a` | `11d5690e-26d1-47d1-b93a-53d36aa8b14e` | 0 | 0 | `PASS` |
| 1 | `neutral_a` | `d50c0b1d-585a-4452-b141-9dd1fcc2fe98` | 0 | 1 | `TASK_FAIL` |
| 1 | `neutral_b` | `5ecfed32-4ea2-4268-b411-46d7420dac5d` | 0 | 0 | `PASS` |
| 2 | `neutral_b` | `8875df25-4255-4ebe-9477-0ccdb4739624` | 0 | 0 | `PASS` |
| 2 | `neutral_a` | `143d3489-caba-4c4e-9d1d-ae5c07edd062` | 0 | 0 | `PASS` |

The accepted result is **3/3 judgeable pairs**, **1/3 mechanical disagreements (33.3%)**, **0/3
preflight failures**, **0/6 infrastructure failures**, and **0/6 environment mismatches**. Agent
elapsed time was 82.076–101.233 seconds and reported tokens were 29,553–42,546.

This is stochastic continuation noise under an equivalent replay prefix, not an environment or
snapshot failure. The three-pair sample is too small for a precise population rate, but downstream
intervention deltas comparable to this region's observed 33.3% disagreement cannot be treated as
causal evidence without additional repeats.

## Provenance and gates

- freeze PR [#366](https://github.com/spotter-agent/spotter/pull/366), merge commit
  `bec0d7cb6f22838243d9bec75939b92de2018580`;
- protocol SHA-256
  `f3d4deae81049d327a3256f3458111aff04993e51d0827f075116aebcdea7b5c`;
- task set `corpus/swebench-verified-high-f2p-v1.toml`, SHA-256
  `e261ac23e850e69c36223df70cc35ebd1e6c8557e2bb94788333e453c4439155`;
- task-batch run ID `010ed5dd-f48d-464a-9bc4-724fdd7f724a`, started
  `2026-09-04T08:21:33.525007+00:00`, finished `2026-09-04T08:43:10.876997+00:00`;
- task-batch raw SHA-256
  `6a0221cbedc09d40a8710bfec5e289dc85f62a3f1286826f2a41905dff2428b4` over 60,944 bytes;
- capture readiness pinned integration generation
  `e266a892b1f0d8eb77a7c6515eb30caf5aadc2f94b06a9fa5158963166c00c9c` and Hook command SHA-256
  `c99b8682a2503771794763870baabbf33fa51d8946fe4d6c0cc849834d8e046c`;
- neutral experiment ID `10ced71e-526b-4543-80bd-5a799ea80c91`, started
  `2026-09-04T08:44:09.466153+00:00`, finished `2026-09-04T08:54:34.755222+00:00`;
- neutral raw SHA-256
  `1aaf88b39fc35bf5cfddf39abc46690f5693d988ed24bf0847a46adcf1304479` over 37,040 bytes;
- scorer SHA-256
  `c996cceee8b07aa24b5bbcd84b777d1e0c7a6fef3b4b679234ca04917d094b6f` over 8,500 bytes;
- Codex `0.153.1`, Python `3.12.13`, Docker `29.4.0`, macOS 26.5.1 arm64;
- model `gpt-5.6-sol`, reasoning effort `low`, sandbox `workspace-write`;
- retained isolated root: `~/.spotter-experiments/issue-365-high-f2p-v1-20260904`.

Static validation and scorer preflight passed before freeze and again against the merged freeze
commit. The paid runner repeated preflight before writing its header. The task-batch journal and
neutral journal each contain one header, six arm rows, and one completion row. Git-aware cleanup
left no neutral worktree registered or on disk.

The immutable dataset, Git tree, patch, image, scorer, budget, task-manifest, and task-set identities
are listed in the frozen protocol. The machine-readable result is
[`fork-natural-failure-high-f2p-v1-result.json`](fork-natural-failure-high-f2p-v1-result.json).
Raw JSONL remains local because it contains bounded transcript-derived output and local paths.

## Qualification impact

This frame adds a second independent natural-failure region and shows that replay outcome variance
is not uniformly zero: xarray previously measured 0/3 disagreements, while this Django region
measured 1/3. These strata remain separate; they are not pooled into a benchmark-wide rate.

#42 remains open and **NO-GO** for representative causal use. Its labeled intervention-opportunity
coverage is still zero, and natural ignored-file, tool, process, and service drift coverage remains
incomplete. #23/#34 must cite the regional fidelity result they use, add repeats when expected
effects are comparable to observed replay noise, and avoid claiming a small single-pair delta as an
intervention effect.
