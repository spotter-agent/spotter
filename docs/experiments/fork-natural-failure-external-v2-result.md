# External natural-failure v2 result — predeclared null

**Measured:** 2026-08-27

**Protocol:** [`fork-natural-failure-external-v2`](fork-natural-failure-external-v2-protocol.md)

**Issue:** [#42](https://github.com/spotter-agent/spotter/issues/42)

**Decision:** **no control arm failed; the stop rule applies; #42 remains NO-GO**

## Outcome

The batch executed. All eight arms completed, and every one passed.

```text
task batch: 8 arm(s), 4 task(s)
control:  PASS=4
guidance: PASS=4
pairs: n=4/4 mechanically judgeable; guidance better=0, control better=0, tied=4
replay sources: 8/8 captured
```

The admission rule is:

```text
eligible source = arm == control
                  AND classification == TASK_FAIL
                  AND replay source captured
```

Zero control arms produced `TASK_FAIL`, so no natural-failure source was eligible, so no neutral
pair ran. Per the protocol, this is published as a fixed null result and stopped. No row was
rerun, substituted, or reweighted, and no other SWE-bench row was selected.

**This is the instrument working, not the instrument failing.** The cohort was chosen before any arm
ran, and the model solved all four tasks. That is a fact about the cohort and the model, not evidence
about intervention.

## Provenance

- task set: `corpus/swebench-verified-fidelity-v2.toml`
- task-set SHA-256: `50d7d0272859042fa6567f555fa44e02e88d68d484f8f24bdf03e30d42dccff4`
- raw batch SHA-256: `4cec7ab092c800eb66ac33d316e37fdff0309ce33d3048372a591e8645cabcb5`
- run id: `b9d8cc92-5400-4f12-80e8-b7202d60a8f9`
- started: `2026-08-27T01:00:47Z`
- Codex: `codex-cli 0.149.1`; Python 3.12.13
- model `gpt-5.6-sol`, reasoning effort `low`, sandbox `workspace-write`

## All eight arm classifications

| Task | Arm | Classification | Replay source |
| --- | --- | --- | --- |
| `pallets__flask-5014` | control | `PASS` | captured |
| `pallets__flask-5014` | guidance | `PASS` | captured |
| `psf__requests-2931` | control | `PASS` | captured |
| `psf__requests-2931` | guidance | `PASS` | captured |
| `pytest-dev__pytest-10356` | control | `PASS` | captured |
| `pytest-dev__pytest-10356` | guidance | `PASS` | captured |
| `pylint-dev__pylint-8898` | control | `PASS` | captured |
| `pylint-dev__pylint-8898` | guidance | `PASS` | captured |

## Required-report items

- **freeze PR and content hashes:** above; the v2 frame was frozen in
  [#334](https://github.com/spotter-agent/spotter/pull/334).
- **all eight arm classifications in fixed task order:** above.
- **source/image/setup/check failures separately from `TASK_FAIL`:** none. Every task reached
  `READY` at preflight and every arm completed. Zero source, image, setup, or scorer failures — the
  first run of this cohort with no infrastructure failure at all.
- **raw batch SHA-256 and replay-source coverage:** above; **8/8** captured.
- **control-failure identities and admission/exclusion reasons:** no control failed, so nothing was
  admitted and nothing was excluded.
- **neutral-pair outcome disagreement, environment mismatch, infrastructure failure:** no neutral
  pair ran.
- **did the stop rule prevent neutral forks:** **yes.** This is the rule doing its job: without an
  eligible failure source there is nothing to fork, and running neutral pairs anyway would have
  produced numbers with no failure region to describe.
- **#42 qualification decision:** **unchanged — NO-GO.** The causal noise floor is still unmeasured.
- **which tasks were scored whole-file and which by graded node ID:** `psf__requests-2931` by its 85
  graded node IDs; the other three whole-file. The cohort's pass condition is not uniformly scoped,
  and the four `PASS` results above must not be read as identically scoped.

## What this does and does not support

It supports two claims:

1. The v2 frame is **executable end to end**. Preflight, eight paid arms, mechanical scoring, and
   replay-source capture all completed with no infrastructure failure. v1 never reached a paid arm.
2. `gpt-5.6-sol` at `low` effort solves all four of these SWE-bench Verified tasks, with and without
   the guidance suffix.

It does not qualify the fork instrument, measure a noise floor, or say anything about intervention
benefit or harm. `guidance better=0, control better=0, tied=4` is **not** evidence that guidance
does nothing: four tasks the model solves anyway cannot separate the arms, by construction.

## What this says about the selection rule

The cohort was drawn to cover four repository roles, taking the lexicographically first row meeting
a difficulty and `FAIL_TO_PASS` constraint. That rule is reproducible and was fixed before execution,
which is what made this result publishable rather than negotiable — but it selects for nothing about
difficulty *for the model under test*.

A natural-failure experiment needs a cohort where the control arm sometimes fails. Getting one
without selecting on observed Spotter outcomes is the open design problem, and it is now the
concrete blocker on #42 rather than an instrument defect.

## Follow-ups

1. Design a selection rule that yields control failures without conditioning on Spotter's own
   results — harder rows, higher-difficulty strata, or a different benchmark.
2. The eight captured replay sources are usable inputs regardless: all eight are `PASS` sources, so
   they can measure fork fidelity on passing prefixes even though they cannot supply a failure
   region.
