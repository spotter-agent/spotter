# External natural-failure protocol v3

**Frozen before paid execution:** 2026-09-04

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Supersedes:** [external natural-failure protocol v2](fork-natural-failure-external-v2-protocol.md)

**Decision posture:** remain **NO-GO** unless this fixed cohort supplies admissible
failure-region evidence.

## Question

Does the highest official difficulty stratum in the pinned SWE-bench Verified frame produce at
least one natural control failure with complete replay-source capture, without selecting on any
Spotter arm outcome?

## Why a new cohort exists

The [v2 run](fork-natural-failure-external-v2-result.md) completed all eight paid arms with zero
infrastructure failures and captured 8/8 replay sources, but all four controls passed. Its stop rule
therefore admitted no failure source and ran no neutral pair. That result established that the
external frame is executable, not a failure-region noise floor.

v3 changes only the task cohort. It keeps the same pinned dataset, model, reasoning effort,
sandbox, task budget, arm structure, replay capture, admission rule, and stop rule.

## Upstream frame and selection rule

The sampling frame remains the official 500-row SWE-bench Verified test split:

- dataset: `SWE-bench/SWE-bench_Verified`;
- dataset revision: `78f471bf655a3137b2e8a75af1501690ec009ec3`;
- `data/test-00000-of-00001.parquet` SHA-256:
  `030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25`.

The fixed rule is deliberately shorter than v1/v2:

```text
include every row whose official difficulty is exactly ">4 hours"
order by instance_id
```

The pinned frame contains exactly three such rows, so the rule has no repository choice, sample,
tie-break, or `FAIL_TO_PASS`-count filter:

| Task | Official difficulty | F2P | P2P | Scorer scope |
| --- | --- | ---: | ---: | --- |
| `pydata__xarray-6992` | `>4 hours` | 12 | 945 | official three test files |
| `sphinx-doc__sphinx-7590` | `>4 hours` | 1 | 24 | official test file |
| `sympy__sympy-13878` | `>4 hours` | 1 | 19 | all 20 graded test names |

No Spotter control, guidance, replay, or prior arm outcome participated in selection. No row may be
removed, replaced, or reweighted after this freeze. A frame correction requires a new task-set
version and cannot retroactively repair this one.

## Scorer scope

xarray and Sphinx retain the whole-file commands from their pinned upstream evaluation scripts.

SymPy uses all 20 test names declared by the pinned row: its one `FAIL_TO_PASS` name and all 19
`PASS_TO_PASS` names. Before freeze, the whole-file command was tested and rejected because the
pinned image reports 36 unrelated exceptions even after the gold patch. The graded-name command
keeps the initial state failing and the official gold patch passing. This is the same narrow rule
used for Requests in v2: use complete graded IDs when the pinned image cannot make its official
whole-file command judgeable.

The cohort therefore does not have a uniform pass scope. Reports must identify whole-file and
graded-name tasks rather than pooling them as equivalent coverage.

## Frozen identities

- task set: `corpus/swebench-verified-fidelity-v3.toml`;
- task-set SHA-256: `76818ea895e59d366e088455b53f6654ffe8fde22182fbcefaa35a409150c278`;
- xarray manifest `tasks/swebench-xarray-6992.toml`:
  `7bb4a141f39fbc9da1b6fecf5dbc4d7ed8861ba9918e36b44a5ff65f6fceadff`;
- Sphinx manifest `tasks/swebench-sphinx-7590.toml`:
  `2d566a8af59b60fb4535f7e6917e350b17535fa93d6c9824cf9cc6edc0d51ff7`;
- SymPy manifest `tasks/swebench-sympy-13878.toml`:
  `f767b3dfa53075f8cb5e5c793b8e4fbaecfb09b086d362a7186a9929ea06aac8`.

Every task pins its upstream HTTPS repository, full base commit and tree object, canonical Git
source SHA-256, dataset row and patches, official environment-setup commit, and official evaluation
image by immutable amd64 OCI manifest digest. Setup applies the frozen test patch. Every scorer
invocation restores the upstream test files and reapplies that patch, so an arm cannot pass by
weakening tests. The gold patch is available only to preflight's manifest-owned `known_good`
command and is not copied into an agent workspace.

## Pre-freeze validation

Static validation and scorer preflight passed on 2026-09-04 using macOS 26.5.1 / arm64 and Docker
29.4.0 with the pinned x86_64 images under `--platform linux/amd64` emulation:

```text
validated swebench-verified-fidelity v3 (validation): 3 task(s)
  swebench/verified/pydata__xarray-6992: READY
  swebench/verified/sphinx-doc__sphinx-7590: READY
  swebench/verified/sympy__sympy-13878: READY
```

This proves only that each source and image resolves, the initial scorer fails, and the official
gold patch passes. It is not a model outcome and does not predict which controls will fail.

## Pre-run gates

Paid execution may begin only after the freeze PR merges. The result must cite that PR and the
content hashes above. In a retained isolated experiment root:

1. repeat static validation and preflight against the merged files;
2. install the merged Spotter worktree into an isolated virtual environment;
3. use fresh `SPOTTER_HOME` and `CODEX_HOME` directories and copy only existing Codex
   authentication;
4. run `spotter setup codex --portable` and require replay-capture readiness before the batch
   header or first paid arm;
5. pin the exact Spotter, Codex, model, config, readiness, platform, and image identities in the raw
   batch.

Any source, image, setup, scorer, or capture-readiness failure stops the frame without substitution
and is reported as infrastructure coverage, not a task failure.

## Fixed paid execution

- three tasks / six task-batch arms;
- exactly one batch, with no result-conditioned rerun;
- model: `gpt-5.6-sol`;
- reasoning effort: `low`;
- sandbox: `workspace-write`;
- task budget: 1,800 seconds and 30 turns per arm;
- replay-source capture: enabled;
- guidance suffix: `Continue the task.`.

Guidance arms are not neutral evidence. Only control `TASK_FAIL` outcomes enter natural-failure
admission; all six arms contribute capture and infrastructure coverage.

## Admission and stop rule

```text
eligible source = arm == control
                  AND classification == TASK_FAIL
                  AND replay source captured
```

- If no control is eligible, publish the fixed null result and stop. Do not rerun or select another
  row.
- For each eligible source, run `fork-coverage` and choose its earliest `FORKABLE_EXACT`
  pre-mutation point.
- Exclude, without replacement, a source carrying an observation gap, external effect,
  environment mismatch, missing rollout/snapshot, or insufficient context reconstruction.
- For every surviving source, run exactly three neutral pairs with the frozen scorer,
  `gpt-5.6-sol`, and `low` reasoning effort.

## Required report

Always publish:

- freeze PR and every content hash above;
- all six arm classifications in fixed task order;
- source/image/setup/check failures separately from `TASK_FAIL`;
- raw batch SHA-256 and replay-source coverage;
- control-failure identities and every admission/exclusion reason;
- neutral-pair outcome disagreement, environment mismatch, and infrastructure failure separately;
- whether the stop rule prevented neutral forks;
- whole-file versus graded-name scorer scope;
- unchanged or revised #42 qualification decision.

This cohort can qualify the instrument only in its observed failure region. It cannot establish
intervention benefit, benchmark-wide model quality, or production readiness.
