# Independent natural-failure v1 result — predeclared null

**Measured:** 2026-09-04

**Protocol:** [`fork-natural-failure-independent-v1`](fork-natural-failure-independent-v1-protocol.md)

**Issue:** [#362](https://github.com/spotter-agent/spotter/issues/362)

**Decision:** **all six control arms passed; the null stop rule applies; #42 remains NO-GO**

## Outcome

The fixed batch completed all twelve arms with no source, image, setup, scorer, or capture failure:

```text
task batch: 12 arm(s), 6 task(s)
control: PASS=6
guidance: PASS=6
pairs: n=6/6 mechanically judgeable; guidance better=0, control better=0, tied=6
replay sources: 12/12 captured
```

| Task | Scorer scope | Control | Guidance | Replay sources |
| --- | --- | --- | --- | ---: |
| `pallets__flask-5014` | official whole-file command | `PASS` | `PASS` | 2/2 |
| `psf__requests-2931` | all 85 graded node IDs | `PASS` | `PASS` | 2/2 |
| `pytest-dev__pytest-10356` | official whole-file command | `PASS` | `PASS` | 2/2 |
| `pylint-dev__pylint-8898` | official whole-file command | `PASS` | `PASS` | 2/2 |
| `sphinx-doc__sphinx-7590` | official whole-file command | `PASS` | `PASS` | 2/2 |
| `sympy__sympy-13878` | all 20 graded test names | `PASS` | `PASS` | 2/2 |

Every agent exited 0, every setup and scorer command returned 0, and every requested replay source
was captured without error. The execution took about 29 minutes 36 seconds; individual arms took
about 61–271 seconds.

## Admission and stop rule

The predeclared admission rule required a captured control `TASK_FAIL`. None of the six controls
failed, so there was no eligible source, no exclusion decision, and no `fork-coverage` or neutral
pair to run. No task or arm was rerun, replaced, or reweighted.

This is the required fixed null result. The six guidance passes are reported for execution coverage
only; they are not neutral evidence and do not estimate intervention effect.

## Provenance and gates

- freeze PR [#363](https://github.com/spotter-agent/spotter/pull/363), merge commit
  `acc1b71a83942caa2af86b167e329dd5c42888a5`;
- protocol SHA-256
  `7010e3095c53817e5fd068ab8d88c1e0bf3e6a6b48536923a8c9e3fbf07be1e2`;
- task set `corpus/swebench-verified-independent-v1.toml`, SHA-256
  `d928ac5c4c1e1ca8d970644091799919fbf25d07bbc095127d50503df8abef9b`;
- run ID `506bad35-ea0b-4a49-b555-0d1cf987c8e9`;
- started `2026-09-04T05:58:38.251859+00:00`, finished
  `2026-09-04T06:28:14.386988+00:00`;
- raw batch SHA-256
  `e56e490190b92edb57761a58de7e425474212219eabf6ec7b5145583a60035c9` over 99,170 bytes;
- capture readiness pinned integration generation
  `67a2a48536a488248a87ecc659a8e7c424d6c1216c7174eb5fcde4bb73752b4c` and Hook command SHA-256
  `86e5320dfa87d1d04d20dc4cce5115efce9f7432c22dc7da77f069ab8b85c4df`;
- Codex `0.153.1`, Python `3.12.13`, Docker `29.4.0`, macOS 26.5.1 arm64;
- model `gpt-5.6-sol`, reasoning effort `low`, sandbox `workspace-write`;
- retained isolated root: `~/.spotter-experiments/issue-362-independent-v1-20260904`.

Static validation and scorer preflight passed before freeze and again against the merged freeze
commit. The paid runner repeated preflight before writing its header. The append-only raw journal
contains one header, twelve arm rows, and one completion row.

The reused task-manifest hashes and immutable dataset, Git tree, patch, image, scorer, and budget
identities are listed in the frozen protocol. The machine-readable result is
[`fork-natural-failure-independent-v1-result.json`](fork-natural-failure-independent-v1-result.json).
Raw JSONL remains local because it contains bounded transcript-derived output and local paths.

## Qualification impact

This time-separated, outcome-independent frame ran cleanly, but it sampled no natural failure
outside xarray. It therefore adds execution and capture coverage, not a second failure-region noise
estimate. The existing xarray 0/3 disagreement result remains valid only for that one region.

#42 remains open and **NO-GO** for representative causal use. The next evidence slice must either
freeze another outcome-independent frame likely to expose a different natural failure region, add
labeled-opportunity coverage through #24, or measure natural drift; it must not rerun the retained
xarray prefix or reinterpret these passing guidance arms.
