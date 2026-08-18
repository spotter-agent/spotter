# External natural-failure protocol v1

**Frozen before execution:** 2026-08-18

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision posture:** remain **NO-GO** unless this fixed external cohort supplies admissible
failure-region evidence.

## Question

Can a predeclared, externally authored real-repository cohort produce natural control failures with
complete replay-source capture, without selecting tasks from observed Spotter outcomes or tuning
another synthetic fixture?

## Upstream frame

The sampling frame is the official 500-row SWE-bench Verified test split:

- dataset: `SWE-bench/SWE-bench_Verified`;
- dataset revision: `78f471bf655a3137b2e8a75af1501690ec009ec3`;
- `data/test-00000-of-00001.parquet` SHA-256:
  `030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25`;
- source documentation: <https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/datasets.md>;
- Verified description: <https://www.swebench.com/verified.html>.

SWE-bench Verified is used only as an externally authored task/scorer source. This is not a
leaderboard submission and the four-task result must not be generalized to the 500-row benchmark.

## Selection rule

The repository strata were chosen before any arm execution to cover four different pure-Python
project roles: web framework, HTTP library, test framework, and static analyzer. Within each stratum
the rule is deterministic over the pinned dataset:

1. `pallets/flask`: include its sole Verified row as an easy anchor;
2. `psf/requests`: lexicographically first `15 min - 1 hour` row with exactly one
   `FAIL_TO_PASS` test;
3. `pytest-dev/pytest`: lexicographically first `1-4 hours` row with exactly one
   `FAIL_TO_PASS` test;
4. `pylint-dev/pylint`: lexicographically first `1-4 hours` row with exactly one
   `FAIL_TO_PASS` test.

This yields, in fixed execution order:

| Task | Official difficulty | F2P | P2P |
| --- | --- | ---: | ---: |
| `pallets__flask-5014` | `<15 min fix` | 1 | 59 |
| `psf__requests-2931` | `15 min - 1 hour` | 1 | 84 |
| `pytest-dev__pytest-10356` | `1-4 hours` | 1 | 79 |
| `pylint-dev__pylint-8898` | `1-4 hours` | 1 | 18 |

No row may be removed, replaced, or reweighted after preflight or arm outcomes. A correction creates
a new task-set version and does not retroactively repair this frame.

## Frozen task and scorer identities

- task set: `corpus/swebench-verified-fidelity-v1.toml`;
- task-set SHA-256: `b56c0e7acd5cb111c078414d41b1637e3cb67d8a5e6e3358cc9bb4bd6c559b9a`;
- Flask manifest: `47c05af88b2e118fb95571422535b0a840702a43a130b36cbb10ca48f64b1cb5`;
- Requests manifest: `d4a254b2091ec67c14854382b3938d308737102911275e1d778a8611edfe15cd`;
- pytest manifest: `835f7d5e681f61077789c7d2ac347c0ad89230c5771406af9313003d3c0faacb`;
- Pylint manifest: `1f73be6d1ccea07673d939b93f4526d1a03bca8be9602bcadeefe3530a85d88a`.

Every task additionally pins:

- upstream HTTPS repository, full base commit, and tree object;
- canonical Git source SHA-256;
- the dataset row, test patch, and gold solution patch SHA-256;
- the official environment-setup commit;
- the official SWE-bench evaluation image by immutable OCI digest;
- the test file and whole-file pytest command used by the official evaluation script.

Setup applies the frozen test patch after the image is available. Every scorer invocation restores
the upstream test file and reapplies that patch before testing, so an arm cannot pass by weakening
the tests. The gold patch is available only to preflight's manifest-owned `known_good` command and
is not copied into an agent workspace.

## Pre-run gates

Execution may begin only after the freeze PR merges. The result must cite that PR number plus the
task-set, task-manifest, and protocol content hashes as primary durable provenance; a short commit
ID is secondary.

Then, in a retained isolated experiment root:

1. run offline static validation of all task/set hashes;
2. start a Docker-compatible runtime and pull only the four pinned OCI digests;
3. preflight all four tasks, requiring the initial state to fail and the official gold patch to
   pass the same whole-file scorer;
4. stop without substitution if any source, image, setup, scorer, or gold path is not `READY`;
5. install the merged Spotter worktree into an isolated virtual environment;
6. use fresh `SPOTTER_HOME` and `CODEX_HOME` directories and copy only existing Codex
   authentication;
7. run `spotter setup codex --portable` and require capture readiness before the batch header or
   first paid arm;
8. pin the exact Spotter/Codex/model/config/readiness identities in the raw batch.

Preflight and image-pull failures are infrastructure coverage, not task or model outcomes.

## Fixed paid execution

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

Always publish:

- freeze PR and all content hashes above;
- all eight arm classifications in fixed task order;
- source/image/setup/check failures separately from `TASK_FAIL`;
- raw batch SHA-256 and replay-source coverage;
- control-failure identities and every admission/exclusion reason;
- neutral-pair outcome disagreement, environment mismatch, and infrastructure failure separately;
- whether the stop rule prevented neutral forks;
- unchanged or revised #42 qualification decision.

A four-task result is instrument qualification evidence only. It cannot establish intervention
benefit, benchmark-wide agent quality, or production readiness.
