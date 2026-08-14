# Recovery-prefix identical-arm fork run

**Measured:** 2026-08-14

**Source:** frozen `settings-validation` fixture at pinned low reasoning effort

**Decision:** **recovery-path replay was stable in this low-effort cohort; do not pool it with default-effort noise**

## Design

The fixture requires endpoint trimming and positive-integer retry validation. A fresh source session
running `gpt-5.6-sol` at `low` reasoning effort made an incomplete first patch, then observed the
mechanical scorer fail because `None` leaked `TypeError`. It recovered with a second patch and
passed `python3 check.py`.

The experiment selected step 14, after the incomplete patch but before the failing scorer run, with
prefix ID `20a086ad71aff70f39a174f5600baeaff2982c6b0d24a7b33e0ae2ab73bc3928`.
Three pairs (six independent continuations) resumed with the identical `Continue the task.` prompt.
Both model and reasoning effort were pinned to match the source session.

## Result

| Measure | Result |
| --- | ---: |
| Judgeable neutral pairs | 3/3 |
| Passing arms | 6/6 |
| Mechanical outcome disagreements | 0/3 (0%) |
| Environment mismatches | 0/3 |
| Infrastructure failures | 0/6 |
| Continuation tool calls | 3–7 |
| Continuation elapsed time | 23.537–5,169.514 seconds |
| Continuation reported tokens | 190,134–265,549 |

All six continuations found the same `None` conversion failure and repaired it. This removes the
previous reports' source-success ceiling: the source itself made a mechanically failing attempt,
and the chosen prefix contains that incomplete implementation. Outcome variance still did not
appear in three repeated pairs.

The derived machine-readable result is
[`fork-neutral-recovery-prefix.json`](fork-neutral-recovery-prefix.json). The scoped experiment ID
is `4a0ae646-68f7-489d-a5d0-d998b405bce2`. The append-only raw local JSONL remains at
`~/.spotter/experiments/019ffc08-9b3d-7f83-bfeb-6cf3f833671a-step14.jsonl` with SHA-256
`3a863f28a6cc1460d456e2d6b62034bdf3915bf528cfbd8afe75bc955ab1b2dd`; other incomplete and
prepare-only attempts in that file are excluded by experiment ID.

## Coverage and limitations

The source exposed nine proposals: five exact and four excluded after Class C effects. All three
pre-mutation candidates were exact. The experiment deliberately uses `low` reasoning effort to
produce a recoverable source failure, so its 0/3 disagreement rate describes only that pinned
configuration. It must not be pooled with the preceding default-effort cohort or used as its noise
bound.

Across four fresh tasks, Spotter has now observed 0/14 mechanical disagreements, 0/14 environment
mismatches, and 0/28 infrastructure failures when listed descriptively. The defensible strata are
still 0/11 at the prior default effort and 0/3 at low effort. #42 remains open for failures that
survive to final mechanical outcomes and for explicit environment-drift qualification.
