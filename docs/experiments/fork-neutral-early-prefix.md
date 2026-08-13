# Early-prefix identical-arm fork run

**Measured:** 2026-08-13

**Source:** frozen `record-contract` fixture observed through the managed four-Hook integration

**Decision:** **early pre-mutation replay works for this task; representative #42 qualification remains open**

## Design

The source session fixed the multi-file record-normalization contract and passed `python3 check.py`.
After Code Mode call correlation was restored, its coverage report classified four of five proposals
as `FORKABLE_EXACT`, including both pre-mutation proposals. The experiment selected the earliest
point, step 2 before the first repository inspection, with prefix ID
`2f8575d9fcf973dba581328681129677c0afc08b81747bb6b6d9d3e62b248b8f`.

Three pairs (six independent continuations) resumed that prefix with the identical
`Continue the task.` prompt and the pinned `gpt-5.6-sol` model. Every pair passed shared-prefix and
environment-fingerprint preflight. The mechanical scorer was `python3 check.py`.

## Result

| Measure | Result |
| --- | ---: |
| Judgeable neutral pairs | 3/3 |
| Passing arms | 6/6 |
| Mechanical outcome disagreements | 0/3 (0%) |
| Environment mismatches | 0/3 |
| Infrastructure failures | 0/6 |
| Continuation tool calls | 4–6 |
| Continuation elapsed time | 22.497–31.720 seconds |
| Continuation reported tokens | 107,489–154,155 |

This is the first measured exact prefix before any source-session mutation or repository inspection.
It removes the earlier operational blind region for this task, while the wider tool/token spread
shows that identical mechanical outcomes do not imply identical continuations.

The derived machine-readable result is
[`fork-neutral-early-prefix.json`](fork-neutral-early-prefix.json). The raw local JSONL remains at
`~/.spotter/experiments/019ffb20-aa92-7f32-857d-e3bcf7f3ebb2-step2.jsonl` with SHA-256
`ed49358b6b36d280e1252901e60cf12658955959e03db1eb4f081c4f6d39378e`.

## Coverage and limitations

The source exposed five proposals: four exact and one excluded after a Class C external-effect
classification. Both pre-mutation candidates were exact, compared with 0/2 in the earlier run that
lacked a session baseline and Code Mode correlation.

Across the two fresh reported tasks, Spotter has now observed 0/6 mechanical disagreements, 0/6
environment mismatches, and 0/12 infrastructure failures. This remains a small synthetic Python
sample, not a representative global noise bound. #42 should remain open for additional failure
families, prefixes near labeled intervention opportunities, and explicit environment-drift cases.
