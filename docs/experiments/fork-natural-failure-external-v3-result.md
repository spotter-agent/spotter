# External natural-failure v3 result — failure sampled, scorer infrastructure blocked

**Measured:** 2026-09-04

**Protocol:** [`fork-natural-failure-external-v3`](fork-natural-failure-external-v3-protocol.md)

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision:** **a natural control failure was captured and admitted, but no neutral pair was
judgeable; #42 remains NO-GO**

## Outcome

The fixed six-arm batch completed with no source, image, setup, scorer, or replay-capture failure:

```text
task batch: 6 arm(s), 3 task(s)
control:  PASS=2, TASK_FAIL=1
guidance: PASS=2, TASK_FAIL=1
pairs: n=3/3 mechanically judgeable; guidance better=0, control better=0, tied=3
replay sources: 6/6 captured
```

Unlike v2, this cohort sampled the natural failure region. The xarray control failed its frozen
whole-file scorer and supplied a captured source. Against the isolated Codex rollout, its earliest
pre-mutation proposal at step 2 was `FORKABLE_EXACT`, so the source passed admission.

The three prescribed neutral pairs did not yield outcome evidence. All six continuations exited
normally, but every restored fork ceased to be a valid Git worktree before scorer setup. The
scorer's first `git checkout` returned 128 because the fork's `.git` file pointed to a missing
`<source>/.git/worktrees/<fork-id>` directory. No pytest process started.

The raw runner classified those six rows as `TASK_FAIL` and summarized three judgeable tied pairs.
That summary is not accepted as evidence: exit 128 happened in scorer setup, not in the frozen test
suite. The bounded result therefore records **0/3 judgeable pairs**, no disagreement estimate, and
**6/6 scorer infrastructure failures**. [#351](https://github.com/spotter-agent/spotter/issues/351)
tracks the worktree lifetime, isolated-home resolution, and classification defect.

## Fixed batch results

| Task | Scorer scope | Control | Guidance | Replay sources |
| --- | --- | --- | --- | ---: |
| `pydata__xarray-6992` | three official test files | `TASK_FAIL` | `TASK_FAIL` | 2/2 |
| `sphinx-doc__sphinx-7590` | official test file | `PASS` | `PASS` | 2/2 |
| `sympy__sympy-13878` | all 20 graded node IDs | `PASS` | `PASS` | 2/2 |

The task-level guidance comparison is descriptive only. It is not neutral evidence and does not
measure intervention benefit.

## Natural-failure admission

The only control failure was `pydata__xarray-6992`, source session
`01a06a49-d733-7b43-9d07-e60540da3138`.

| Coverage measure | Result |
| --- | ---: |
| Candidates | 12 |
| `FORKABLE_EXACT` | 1 |
| `UNSAFE_EXTERNAL_EFFECT` | 11 |
| Context/state/observation-gap exclusions | 0 |
| Pre-mutation candidates | 6 |
| Pre-mutation exact | 1 |
| Earliest exact step | 2 |

The first CLI coverage invocation looked only under the default `~/.codex` and falsely reported the
step as `NOT_FORKABLE_CONTEXT`. That output was discarded. Recomputing the same coverage function
with the retained isolated Codex home found all 12 ordered rollout calls and admitted step 2. This
home-resolution defect is also recorded in #351.

## Neutral execution and infrastructure

One prelaunch attempt retained six append-only `INFRA_FAIL` rows because its explicit PATH omitted
the Codex executable. No model call started. After verifying the exact Codex and Docker binaries,
the fixed three-pair launch ran six continuations from the same prefix and matching environment:

| Measure | Result |
| --- | ---: |
| Requested neutral pairs | 3 |
| Continuations with agent exit 0 | 6/6 |
| Judgeable neutral pairs | 0/3 |
| Mechanical disagreements | unavailable |
| Environment mismatches | 0/3 |
| Scorers reaching tests | 0/6 |
| Scorer infrastructure failures | 6/6 |
| Prelaunch infrastructure failures | 6/6 recorded starts; 0 model calls |
| Continuation elapsed time | 42.674–81.828 seconds |
| Continuation reported tokens | 8,753–27,540 |

The prelaunch retry was not conditioned on a model or scorer outcome: the executable was absent and
zero agents started. Both experiment IDs and all twelve rows remain in the same append-only raw
journal. The second launch was not retried after its scorer failure.

## Provenance

- freeze PR: [#350](https://github.com/spotter-agent/spotter/pull/350), merge commit
  `39d078f0a7c29760f44be3c49ac95cf71c46d186`;
- task set: `corpus/swebench-verified-fidelity-v3.toml`, SHA-256
  `76818ea895e59d366e088455b53f6654ffe8fde22182fbcefaa35a409150c278`;
- xarray manifest SHA-256:
  `7bb4a141f39fbc9da1b6fecf5dbc4d7ed8861ba9918e36b44a5ff65f6fceadff`;
- Sphinx manifest SHA-256:
  `2d566a8af59b60fb4535f7e6917e350b17535fa93d6c9824cf9cc6edc0d51ff7`;
- SymPy manifest SHA-256:
  `f767b3dfa53075f8cb5e5c793b8e4fbaecfb09b086d362a7186a9929ea06aac8`;
- source batch run id: `5dcdbc34-ab7a-49c5-9059-854ae09196d7`;
- source batch raw SHA-256:
  `ba54d6281eba0999840fa18e8604be22d2af2967ed1105e2c18d3b3122ddc0af`;
- neutral raw SHA-256:
  `cc48342e0be66f93f24a475808440c91ec9bf6fb17f1f90986b08a7779f2f0e1`;
- model `gpt-5.6-sol`, reasoning effort `low`, sandbox `workspace-write`;
- Codex `0.153.1`, Python 3.14.7, Docker 29.4.0, macOS 26.5.1 arm64;
- retained isolated root: `~/.spotter-experiments/issue-42-external-v3-20260904`.

The machine-readable result is
[`fork-natural-failure-external-v3-result.json`](fork-natural-failure-external-v3-result.json).
Raw JSONL is retained locally rather than committed because it contains bounded transcript-derived
output and local paths.

## Qualification impact

v3 improves one important fact: a predeclared external cohort produced a naturally occurring
control failure with complete source capture and one exact pre-mutation replay point. It does not
measure the failure-region noise floor because the required scorers never reached tests.

Therefore #42 remains **NO-GO** for representative causal use. The next step is to resolve #351,
freeze an infrastructure-only successor protocol, and rerun the three neutral pairs from this
retained source without treating v3's invalid scorer rows as task outcomes.
