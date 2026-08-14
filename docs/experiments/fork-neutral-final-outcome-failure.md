# Final-outcome-failure identical-arm fork run

**Measured:** 2026-08-14

**Source:** frozen `profile-contract` fixture with pinned low reasoning effort

**Decision:** **the induced final failure replayed consistently; natural failure noise remains unqualified**

## Design

The source batch used `validation-v1` with `gpt-5.6-sol` at `low` reasoning effort. Its control arm
passed. The guidance arm was deliberately constrained to update only `profile.py`, not inspect or
modify `renderer.py`, and skip checks. It exited normally but failed the frozen `python3 check.py`
scorer, producing the first capture-only replay source whose final mechanical outcome was
`TASK_FAIL`.

The experiment selected step 2, immediately before the source's first tool proposal and before its
first mutation. Both of the source's proposals were `FORKABLE_EXACT`; the selected prefix ID is
`af47f3f8c19be5f4fcd59b2b2c8d7746d7373d040615cff941f6afb4d3d136fd`.

Three pairs (six independent continuations) resumed with the identical `Continue the task.` prompt.
Both model and reasoning effort were pinned and proven equal to the source rollout. The source's
constrained guidance remains part of the shared prefix, so this is an induced failure cohort rather
than a natural task-failure sample.

## Result

| Measure | Result |
| --- | ---: |
| Judgeable neutral pairs | 3/3 |
| Passing arms | 0/6 |
| Failing arms | 6/6 |
| Mechanical outcome disagreements | 0/3 (0%) |
| Environment mismatches | 0/3 |
| Infrastructure failures | 0/6 |
| Continuation tool calls | 3–5 |
| Continuation elapsed time | 19.054–40.357 seconds |
| Continuation reported tokens | 85,201–129,740 |

All six continuations exited normally, reached the scorer, and reproduced the mechanical failure.
The result proves the capture/fork path can retain, branch, and classify a final-outcome failure
without turning it into an infrastructure loss. It does not show that naturally occurring failures
have the same stability: the shared source context intentionally constrained the implementation.

The derived machine-readable result is
[`fork-neutral-final-outcome-failure.json`](fork-neutral-final-outcome-failure.json). The raw source
batch remains at
`~/.spotter/experiments/task-batches/spotter-validation-v1-0395b855-f638-4f4b-98a6-87f42fb1357b.jsonl`
with SHA-256 `b58b04d00d30f6ec98b6b375ca3cafb6ceeacbe1409fb8a036a48081c3ad83df`.
The raw neutral result remains at
`~/.spotter/experiments/019ffefd-c083-7ba0-8906-bb95c46a586c-step2.jsonl` with SHA-256
`4c8c1fc057ffdd4fd1781dabbf7420da3232325722736bec6c644c4526bba101`.

## Qualification impact

This adds a separate pinned-low induced-failure stratum: 0/3 disagreements, 0/3 environment
mismatches, and 0/6 infrastructure failures, with all six arms ending in `TASK_FAIL`. It is the
first final-outcome-failure evidence but not a representative failure distribution and therefore
does not change the v1 **NO-GO** decision for #34/#23. #42 remains open for naturally occurring
final failures, more independent task families, and broader natural environment drift.
