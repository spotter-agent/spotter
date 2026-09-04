# Independent natural-failure protocol v1

**Frozen before paid execution:** 2026-09-04

**Issue:** [#362](https://github.com/spotter-agent/spotter/issues/362)

**Parent qualification:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision posture:** #42 remains **NO-GO** until this independent stratum supplies admissible
failure-region evidence or publishes its predeclared null result.

## Question

Does a time-separated run over the previously frozen external SWE-bench tasks outside the measured
xarray region produce a natural control failure with complete replay-source capture and exact scorer
parity, without selecting on Spotter intervention outcomes?

## Fixed selection rule

The frame is the union of every immutable external task manifest already frozen in the v2 and v3
protocols. Exclude only `pydata__xarray-6992`, because its retained prefix is the failure region
already measured by v6. Order the remaining tasks by `task_id`.

This rule yields exactly six tasks:

| Task | Original frame | Scorer scope |
| --- | --- | --- |
| `pallets__flask-5014` | v2 | official whole-file command |
| `psf__requests-2931` | v2 | all 85 graded node IDs |
| `pytest-dev__pytest-10356` | v2 | official whole-file command |
| `pylint-dev__pylint-8898` | v2 | official whole-file command |
| `sphinx-doc__sphinx-7590` | v3 | official whole-file command |
| `sympy__sympy-13878` | v3 | all 20 graded test names |

No prior task outcome filters membership. No task may be removed, replaced, rerun, or reweighted
after this freeze. A frame correction requires a new task-set version.

The upstream frame remains SWE-bench Verified revision
`78f471bf655a3137b2e8a75af1501690ec009ec3`; its sole parquet file has SHA-256
`030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25`.
Each reused manifest already pins the dataset row, source commit/tree, test and gold patches,
environment-setup commit, OCI image digest, scorer, timeouts, and agent budget.

## Frozen identities

- task set: `corpus/swebench-verified-independent-v1.toml`;
- task-set SHA-256: `d928ac5c4c1e1ca8d970644091799919fbf25d07bbc095127d50503df8abef9b`;
- Flask manifest SHA-256:
  `47c05af88b2e118fb95571422535b0a840702a43a130b36cbb10ca48f64b1cb5`;
- Requests manifest SHA-256:
  `5dfbc854695f3b97565d8a372f938f928291fb1ad1f308100c64c70bfed7a164`;
- pytest manifest SHA-256:
  `835f7d5e681f61077789c7d2ac347c0ad89230c5771406af9313003d3c0faacb`;
- Pylint manifest SHA-256:
  `1f73be6d1ccea07673d939b93f4526d1a03bca8be9602bcadeefe3530a85d88a`;
- Sphinx manifest SHA-256:
  `2d566a8af59b60fb4535f7e6917e350b17535fa93d6c9824cf9cc6edc0d51ff7`;
- SymPy manifest SHA-256:
  `f767b3dfa53075f8cb5e5c793b8e4fbaecfb09b086d362a7186a9929ea06aac8`.

The result must also report the merged freeze commit and protocol SHA-256 as durable provenance.

## Pre-freeze validation

Static validation and scorer preflight passed on 2026-09-04 using macOS 26.5.1 / arm64, Python
3.12.13, and Docker 29.4.0 with the pinned x86_64 images under `linux/amd64` emulation:

```text
validated swebench-verified-independent v1 (validation): 6 task(s)
  swebench/verified/pallets__flask-5014: READY
  swebench/verified/psf__requests-2931: READY
  swebench/verified/pytest-dev__pytest-10356: READY
  swebench/verified/pylint-dev__pylint-8898: READY
  swebench/verified/sphinx-doc__sphinx-7590: READY
  swebench/verified/sympy__sympy-13878: READY
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

- six tasks / twelve task-batch arms in manifest order;
- exactly one batch, with no result-conditioned retry;
- model: `gpt-5.6-sol`;
- reasoning effort: `low`;
- sandbox: `workspace-write`;
- task budget: 1,800 seconds and 30 turns per arm;
- replay-source capture: enabled;
- guidance suffix: `Continue the task.`.

Guidance failures are ineligible because their context contains intervention content. All twelve arms
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

Always publish the freeze PR/commit and content hashes; all twelve arm classifications; raw batch
SHA-256 and capture coverage; source, image, setup, scorer, environment, and infrastructure failures
separately from `TASK_FAIL`; every control-failure admission or exclusion; and whole-file versus
graded-name scorer scope.

If neutral pairs run, also publish exact prefix/environment parity and outcome disagreement for each
source. If none run, state which stop rule prevented them. Update #42 without pooling this stratum
with xarray or interpreting guidance-arm differences as intervention effect.
