# External natural-failure v1 preflight result

**Measured:** 2026-08-21

**Protocol:** [`fork-natural-failure-external-v1`](fork-natural-failure-external-v1-protocol.md)

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision:** **stopped at pre-run gate 4; no paid arm executed; #42 remains NO-GO**

## Outcome

The frozen four-task cohort did not pass preflight. Two of four tasks returned `UNJUDGEABLE`, so
protocol gate 4 — *"stop without substitution if any source, image, setup, scorer, or gold path is
not `READY`"* — forbids starting the paid batch. No arm ran, no model tokens were spent, and no row
was removed, replaced, or reweighted.

This is an infrastructure-coverage result, not a task or model outcome.

## Provenance

- freeze PR: [#317](https://github.com/spotter-agent/spotter/pull/317)
- task set: `corpus/swebench-verified-fidelity-v1.toml`
- task-set SHA-256: `b56c0e7acd5cb111c078414d41b1637e3cb67d8a5e6e3358cc9bb4bd6c559b9a` (matches the
  protocol's declared hash; the frame is intact)
- protocol SHA-256: `e6a81a6280cf88dd59428b9e8c43fdab7c476d6c010bb71bcc57df99eb131921`
- Spotter commit: `bb70736` (secondary to the hashes above)

All four pinned OCI digests were pulled and matched their manifests:

| Task | Manifest SHA-256 | Image digest verified |
| --- | --- | --- |
| `pallets__flask-5014` | `47c05af88b2e118fb95571422535b0a840702a43a130b36cbb10ca48f64b1cb5` | yes |
| `psf__requests-2931` | `d4a254b2091ec67c14854382b3938d308737102911275e1d778a8611edfe15cd` | yes |
| `pytest-dev__pytest-10356` | `835f7d5e681f61077789c7d2ac347c0ad89230c5771406af9313003d3c0faacb` | yes |
| `pylint-dev__pylint-8898` | `1f73be6d1ccea07673d939b93f4526d1a03bca8be9602bcadeefe3530a85d88a` | yes |

## Preflight classifications

Fixed execution order.

| Task | Classification | Failing phase | Observed |
| --- | --- | --- | --- |
| `pallets__flask-5014` | `READY` | — | — |
| `psf__requests-2931` | `UNJUDGEABLE` | `positive:swebench-resolution` | exit 1; `86 passed, 1 xfailed, 81 errors in 0.48s` |
| `pytest-dev__pytest-10356` | `UNJUDGEABLE` | `positive:swebench-resolution` | exit 4; `'minversion' requires pytest-2.0, actual pytest-0.1.dev1+g3c1534944.d20260821` |
| `pylint-dev__pylint-8898` | `READY` | — | — |

Both failures are at the **positive** phase: the initial state failed as required and the official
gold patch was applied cleanly, but the whole-file scorer still did not pass afterwards. Protocol
gate 3 requires both halves; only the second half failed.

Neither `UNJUDGEABLE` is a source, image, or setup failure. Source materialisation, digest-pinned
image pull, and setup succeeded for all four tasks.

## Attribution

### `pytest-dev__pytest-10356` — instrument defect, host-independent

`_materialize_task_source` fetches the pinned commit with `--no-tags --depth=1`. `setuptools_scm`
therefore has neither tags nor reachable history to describe, and derives
`0.1.dev1+g3c1534944.d20260821` instead of a real version. pytest's own `pyproject.toml` sets
`minversion`, so pytest refuses to run its own test suite against that version.

This is an **environment fidelity** failure in the sense the protocol separates: the materialised
source is not materially equivalent to the upstream state the benchmark's scorer assumes. It is
caused by Spotter, not by the task or the model, and it does not depend on the host platform.

A shallow, tagless fetch cannot be repaired by fetching tags alone — at `--depth=1` there is no
history reachable to the tag for `git describe` to use. Correcting it trades clone cost against
fidelity and is a deliberate design decision, not a patch to apply under a failing run.

### `psf__requests-2931` — unattributed

81 collection errors in 0.48 s, against 86 passed and 1 xfailed. The speed and the error/failure
split point at collection-time environment breakage rather than the tested behaviour, but this run
did not isolate a cause. Recorded as unattributed rather than guessed.

## Execution environment caveat

This preflight ran on **macOS 26.5.1 / arm64** with Docker 29.4.0, executing the pinned `x86_64`
evaluation images under emulation (`--platform linux/amd64`).

The protocol asks only for "a Docker-compatible runtime", but emulation is a material difference
from the x86 host the official images target. The `pytest` attribution above is host-independent
and would reproduce anywhere. The `requests` result is **not** established as host-independent, and
this run cannot distinguish an emulation artefact from a genuine environment defect.

A clean x86_64 execution could therefore produce a different classification for `requests-2931`,
and possibly a different gate outcome. This result documents the attempt and its blocking outcome;
it does not establish that the frozen cohort is unusable on the intended platform.

## Required-report items

- freeze PR and content hashes: above;
- all eight arm classifications: **not applicable — no arm executed**;
- source/image/setup/check failures separately from `TASK_FAIL`: both failures are check failures at
  the positive phase; zero source, image, or setup failures;
- raw batch SHA-256 and replay-source coverage: **none — no batch was created**;
- control-failure identities and admission/exclusion reasons: **none — admission was never reached**;
- neutral-pair disagreement, environment mismatch, infrastructure failure: **no neutral pair ran**;
- did the stop rule prevent neutral forks: **yes, at pre-run gate 4**;
- #42 qualification decision: **unchanged, NO-GO**.

## What this does and does not support

It supports one claim: the frozen external cohort did not reach an executable state in this
environment, and the instrument's own source materialisation is responsible for at least one of the
two blocking tasks.

It does not qualify the fork instrument, measure a noise floor, or say anything about intervention
benefit. The causal noise floor named in `docs/status.md` remains unmeasured.

## Follow-ups

Deliberate, and not applied here:

1. decide the source-materialisation fidelity trade-off (tags and history depth versus clone cost);
2. attribute the `requests-2931` collection errors, ideally on a native x86_64 host;
3. if either correction lands, publish a **new task-set version** — this frame is not retroactively
   repaired.

---

## Update — second preflight attempt (instrument corrected)

**Measured:** 2026-08-21, after follow-up 1 was decided and applied.

`_materialize_task_source` now fetches full history with tags instead of `--no-tags --depth=1`.
Re-running the same frozen frame against the corrected instrument:

| Task | Attempt 1 | Attempt 2 |
| --- | --- | --- |
| `pallets__flask-5014` | `READY` | `READY` |
| `psf__requests-2931` | `UNJUDGEABLE` | `UNJUDGEABLE` |
| `pytest-dev__pytest-10356` | `UNJUDGEABLE` | **`READY`** |
| `pylint-dev__pylint-8898` | `READY` | `READY` |

This confirms the attribution: the tagless shallow fetch, not the task, was responsible for
`pytest-10356`. The correction was chosen over a larger `--depth`, because no fixed depth is
defensible across repositories and a tag must be reachable for `git describe` to name it.

`requests-2931` is unchanged, down to the same 86 passed / 1 xfailed / 81 errors in under half a
second. Its cause is therefore independent of source materialisation and remains unattributed.

Gate 4 requires every task to be `READY`, so **execution is still blocked and #42 remains NO-GO**.
One of the two blocking causes is resolved; the remaining one is the unattributed cohort task.

The paired `.json` records attempt 1 only. It is left unmodified: a published result should not be
rewritten by a later run, and this attempt used a different instrument.

Remaining follow-ups: attribute `requests-2931`, ideally on a native x86_64 host, which would also
settle whether the emulation caveat above affects it.
