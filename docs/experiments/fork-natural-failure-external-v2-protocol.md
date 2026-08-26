# External natural-failure protocol v2

**Frozen before execution:** 2026-08-24

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Supersedes:** [external natural-failure protocol v1](fork-natural-failure-external-v1-protocol.md)

**Decision posture:** remain **NO-GO** unless this fixed external cohort supplies admissible
failure-region evidence.

## Why a new version exists

The [v1 result](fork-natural-failure-external-v1-result.md) stopped at pre-run gate 4 with
`psf__requests-2931` classified `UNJUDGEABLE`. That task's manifest pinned the official whole-file
command `pytest -q test_requests.py`, but the pinned evaluation image's `testbed` environment does
not contain `pytest-httpbin`. Without that plugin the `httpbin` fixture override in
`test_requests.py:51` has no parent fixture, and 81 tests error at setup.

The defect was reproduced with no Spotter code and no volume mount, directly in the pinned image, so
it is neither an instrument defect nor an emulation artefact. It is a defect in the v1 frame: the
manifest pins a command the pinned image cannot satisfy.

v1's own rule — *"No row may be removed, replaced, or reweighted after preflight or arm outcomes. A
correction creates a new task-set version and does not retroactively repair this frame"* — requires
this version. The v1 frame is left byte-identical and is not repaired.

## Delta from v1

Exactly one change, in exactly one task.

`psf__requests-2931`'s `precheck` and `swebench-resolution` scorer commands are narrowed from the
whole test file to the **graded node IDs of the pinned dataset row** — its single `FAIL_TO_PASS`
test plus its 84 `PASS_TO_PASS` tests, 85 node IDs in total. Nothing else changes: the same base
commit, tree, test patch, gold patch, OCI image digest, timeouts, budget, and prompt.

This narrowing moves the scorer *toward* the official benchmark, not away from it. SWE-bench grades
per node ID through a log parser; the whole-file exit code was always a strictly stronger condition
that no official evaluation applies.

The other three manifests are unchanged and retain their v1 SHA-256 values, which can be checked
against the v1 protocol.

## Rejected alternative: uniform node-ID scoring

Narrowing all four tasks to graded node IDs was evaluated first, because a single scorer definition
across the cohort would remove ungraded-test noise from the fidelity measurement entirely.

It was rejected as **not achievable from the frozen frame**. The pinned SWE-bench Verified row's
`PASS_TO_PASS`/`FAIL_TO_PASS` lists are truncated at whitespace inside parametrized node IDs:

| Task | Graded IDs | Truncated |
| --- | ---: | ---: |
| `pallets__flask-5014` | 60 | 0 |
| `psf__requests-2931` | 85 | 0 |
| `pytest-dev__pytest-10356` | 80 | 14 |
| `pylint-dev__pylint-8898` | 19 | 1 |

Reconstructing a truncated ID by prefix-matching against the image's collected tests is ambiguous,
so it would invent frame data rather than read it:

```text
testing/test_mark.py::test_mark_option[xyz                     → 4 collected matches
testing/test_mark.py::test_keyword_option_custom[not           → 4 collected matches
tests/config/test_config.py::test_csv_regex_comma_in_quantifier[foo,  → 3 collected matches
```

The rule this version therefore applies is narrow and deterministic:

```text
narrow a task's scorer to its graded node IDs
  only when the pinned image cannot satisfy the official whole-file command
  AND the pinned row's node IDs are complete
otherwise keep the official whole-file command
```

`psf__requests-2931` is the only task in this cohort meeting both conditions. The cohort's scorer
definition is therefore **not uniform**, and any report over it must not treat the four tasks'
pass/fail conditions as identically scoped.

## Upstream frame

Unchanged from v1:

- dataset: `SWE-bench/SWE-bench_Verified`;
- dataset revision: `78f471bf655a3137b2e8a75af1501690ec009ec3`;
- `data/test-00000-of-00001.parquet` SHA-256:
  `030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25` — re-downloaded and re-verified
  on 2026-08-24 before the node IDs were read from it.

The selection rule, strata, and fixed execution order are unchanged from v1 and are not restated
here. No row is removed, replaced, or reweighted.

## Frozen task and scorer identities

- task set: `corpus/swebench-verified-fidelity-v2.toml`;
- task-set SHA-256: `50d7d0272859042fa6567f555fa44e02e88d68d484f8f24bdf03e30d42dccff4`;
- Flask manifest `tasks/swebench-flask-5014.toml`:
  `47c05af88b2e118fb95571422535b0a840702a43a130b36cbb10ca48f64b1cb5` (unchanged from v1);
- Requests manifest `tasks/swebench-requests-2931-v2.toml`:
  `5dfbc854695f3b97565d8a372f938f928291fb1ad1f308100c64c70bfed7a164` (**new**);
- pytest manifest `tasks/swebench-pytest-10356.toml`:
  `835f7d5e681f61077789c7d2ac347c0ad89230c5771406af9313003d3c0faacb` (unchanged from v1);
- Pylint manifest `tasks/swebench-pylint-8898.toml`:
  `1f73be6d1ccea07673d939b93f4526d1a03bca8be9602bcadeefe3530a85d88a` (unchanged from v1).

Every task still pins its upstream HTTPS repository, full base commit and tree object, canonical Git
source SHA-256, dataset row / test patch / gold solution patch SHA-256, official environment-setup
commit, and official evaluation image by immutable OCI digest.

Setup applies the frozen test patch after the image is available. Every scorer invocation restores
the upstream test file and reapplies that patch before testing, so an arm cannot pass by weakening
the tests. The gold patch remains available only to preflight's manifest-owned `known_good` command
and is not copied into an agent workspace.

## Pre-run gates

Unchanged from v1. Execution may begin only after the freeze PR merges, and the result must cite
that PR number plus the task-set, task-manifest, and protocol content hashes as primary durable
provenance.

Gates 1–4 were run against this frame on 2026-08-24, before freezing, on macOS 26.5.1 / arm64 with
Docker 29.4.0 executing the pinned `x86_64` images under `--platform linux/amd64` emulation:

```text
spotter tasks preflight corpus/swebench-verified-fidelity-v2.toml

validated swebench-verified-fidelity v2 (validation): 4 task(s)
  swebench/verified/pallets__flask-5014:     READY
  swebench/verified/psf__requests-2931:      READY
  swebench/verified/pytest-dev__pytest-10356: READY
  swebench/verified/pylint-dev__pylint-8898:  READY
```

For `psf__requests-2931` the narrowed scorer produces `1 failed, 84 passed` against the initial
state and `85 passed` after the official gold patch, with zero errors and the image unmodified.

Gates 5–8 — isolated virtual environment, fresh `SPOTTER_HOME`/`CODEX_HOME`,
`spotter setup codex --portable` with capture readiness, and pinned batch identities — are unchanged
from v1 and are **not** satisfied by this pre-freeze run. They must be performed in the retained
isolated experiment root at execution time.

This pre-freeze preflight is a frame-viability check, not the recorded gate run. The result document
must report its own gate execution.

## Emulation caveat

The pre-freeze preflight ran under x86 emulation. v1's two blocking causes were both established as
host-independent, so emulation does not explain either. This does not establish that every future
arm outcome is host-independent; the result must record the execution platform.

## Fixed paid execution

Unchanged from v1:

- four tasks / eight task-batch arms;
- exactly one batch, with no result-conditioned rerun;
- model: `gpt-5.6-sol`;
- reasoning effort: `low`;
- sandbox: `workspace-write`;
- replay-source capture: enabled;
- guidance suffix: `Continue the task.`.

Guidance arms are not neutral evidence. Only control `TASK_FAIL` outcomes enter natural-failure
admission; all eight arms contribute capture and infrastructure coverage.

## Admission and stop rule

Unchanged from v1:

```text
eligible source = arm == control
                  AND classification == TASK_FAIL
                  AND replay source captured
```

- If no control is eligible, publish the fixed null result and stop. Do not rerun or choose another
  SWE-bench row.
- For each eligible source, run `fork-coverage` and choose its earliest `FORKABLE_EXACT`
  pre-mutation point.
- Exclude, without replacement, a source carrying an observation gap, external effect, environment
  mismatch, missing rollout/snapshot, or insufficient context reconstruction.
- For every surviving source run exactly three neutral pairs with the frozen scorer,
  `gpt-5.6-sol`, and `low` reasoning effort.

## Required report

Everything v1 requires, plus one item:

- freeze PR and all content hashes above;
- all eight arm classifications in fixed task order;
- source/image/setup/check failures separately from `TASK_FAIL`;
- raw batch SHA-256 and replay-source coverage;
- control-failure identities and every admission/exclusion reason;
- neutral-pair outcome disagreement, environment mismatch, and infrastructure failure separately;
- whether the stop rule prevented neutral forks;
- unchanged or revised #42 qualification decision;
- **which tasks were scored whole-file and which were scored by graded node ID**, so a reader cannot
  assume a uniform pass condition.

A four-task result is instrument qualification evidence only. It cannot establish intervention
benefit, benchmark-wide agent quality, or production readiness.
