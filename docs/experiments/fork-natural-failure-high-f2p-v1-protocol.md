# Highest-F2P natural-failure protocol v1

**Frozen before paid execution:** 2026-09-04

**Issue:** [#365](https://github.com/spotter-agent/spotter/issues/365)

**Parent qualification:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision posture:** #42 remains **NO-GO** until this independent hard stratum supplies another
admissible failure region or publishes its predeclared null result.

## Question

Does an objective sample of the highest-`FAIL_TO_PASS` unmeasured hard SWE-bench Verified tasks
produce a natural control failure with complete replay-source capture and exact scorer parity?

## Fixed selection rule

Use SWE-bench Verified revision `78f471bf655a3137b2e8a75af1501690ec009ec3`. Exclude the seven
tasks already measured by external v2/v3/#362, keep rows whose official difficulty is exactly
`1-4 hours`, sort by descending `FAIL_TO_PASS` count and then `instance_id`, and take the first
three rows.

The pinned dataset contains 42 rows in that difficulty band and 40 after the exclusion. The rule
yields exactly:

| Rank | Task | F2P | P2P | Scorer scope |
| ---: | --- | ---: | ---: | --- |
| 1 | `django__django-14011` | 17 | 2 | official module scope |
| 2 | `pylint-dev__pylint-4551` | 10 | 0 | official whole-file scope |
| 3 | `django__django-16560` | 8 | 66 | official module scope |

No Spotter or model outcome participates in selection. No task may be removed, replaced, rerun, or
reweighted after this freeze. A correction requires a new task-set version.

The upstream parquet file has SHA-256
`030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25`. Each manifest pins the
dataset row, source commit/tree, test and gold patches, environment-setup commit, OCI image digest,
official scorer command, timeouts, and agent budget.

## Frozen identities

- task set: `corpus/swebench-verified-high-f2p-v1.toml`;
- task-set SHA-256: `e261ac23e850e69c36223df70cc35ebd1e6c8557e2bb94788333e453c4439155`;
- `django__django-14011` manifest SHA-256:
  `3b0c513f71c88d3cc5d9353836b9b63a83411e3ec36b065009807bbb936e07ec`;
- `pylint-dev__pylint-4551` manifest SHA-256:
  `1cefb8fe4ca6b6d45d41da2e8dbc696fb56c308764266361a3a3a6f8e6e0d2ad`;
- `django__django-16560` manifest SHA-256:
  `1f7bc3bba68f978930f57295bf09c1a926260bb7d13014cade3cff7ea44f26ed`;
- `django__django-14011` image:
  `docker.io/swebench/sweb.eval.x86_64.django_1776_django-14011@sha256:453eb9c941d6adadbe078e38620d787616d545c50d4ce4bffaf770db45fabf56`;
- `pylint-dev__pylint-4551` image:
  `docker.io/swebench/sweb.eval.x86_64.pylint-dev_1776_pylint-4551@sha256:f0f898ca4ef3fec27a985b1c2c6ca6ed3cb8fbf5be393e0bb6caf4aaf1512c8c`;
- `django__django-16560` image:
  `docker.io/swebench/sweb.eval.x86_64.django_1776_django-16560@sha256:0bafff953ce186aa261162d4091549fb4ad49df938900474b5f070d511bb1604`.

The result must also report the merged freeze commit and protocol SHA-256.

## Pre-freeze validation

Static validation and scorer preflight passed on 2026-09-04 using macOS 26.5.1 / arm64, Python
3.12.13, and Docker 29.4.0 with the pinned x86_64 images under `linux/amd64` emulation:

```text
validated swebench-verified-high-f2p v1 (validation): 3 task(s)
  swebench/verified/django__django-14011: READY
  swebench/verified/pylint-dev__pylint-4551: READY
  swebench/verified/django__django-16560: READY
```

This proves only source/image/scorer viability: every initial state fails and every official gold
patch passes. It is not a model outcome.

## Pre-run gates

Paid execution may begin only after the freeze PR merges. In a retained isolated experiment root:

1. repeat static validation and scorer preflight against the merged files;
2. install the merged Spotter commit into an isolated virtual environment;
3. use isolated `SPOTTER_HOME` and `CODEX_HOME`, copying only existing Codex authentication;
4. run `spotter setup codex --portable` and require replay-capture readiness before any paid arm;
5. pin exact Spotter, Codex, model, config, readiness, platform, image, task, and scorer identities
   in the raw batch.

Any source, image, setup, scorer, or capture-readiness failure stops the frame without substitution
and is reported as infrastructure, not a task failure.

## Fixed paid execution

- three tasks / six task-batch arms in manifest order;
- exactly one batch, with no result-conditioned retry;
- model: `gpt-5.6-sol`;
- reasoning effort: `low`;
- sandbox: `workspace-write`;
- task budget: 1,800 seconds and 30 turns per arm;
- replay-source capture: enabled;
- guidance suffix: `Continue the task.`.

Guidance failures are ineligible because their context contains intervention content. All six arms
still contribute capture and infrastructure coverage.

## Admission and stop rule

```text
eligible source = arm == control
                  AND classification == TASK_FAIL
                  AND replay source captured
```

- If no control is eligible, publish the fixed null result and stop.
- For each eligible source, run `fork-coverage` and select its earliest `FORKABLE_EXACT`
  pre-mutation point.
- Exclude, without replacement, a source carrying an observation gap, external effect, environment
  mismatch, missing rollout/snapshot, insufficient context reconstruction, or scorer-parity failure.
- For every surviving source, run exactly three neutral pairs with its frozen scorer,
  `gpt-5.6-sol`, and `low` reasoning effort.
- Never rerun a paid arm or replace a task because of an agent, scorer, or neutral-pair outcome.

## Required report

Always publish the freeze PR/commit and content hashes; all six arm classifications; raw batch
SHA-256 and capture coverage; source, image, setup, scorer, environment, and infrastructure failures
separately from `TASK_FAIL`; every control-failure admission or exclusion; and scorer scope.

If neutral pairs run, also publish exact prefix/environment parity and outcome disagreement for each
source. If none run, state which stop rule prevented them. Update #42 without pooling this stratum
with xarray or interpreting guidance-arm differences as intervention effect.
