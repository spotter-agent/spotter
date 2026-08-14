# Edit-decision identical-arm fork run

**Measured:** 2026-08-14

**Source:** frozen `record-contract` fixture at the first edit proposal

**Decision:** **the edit-decision prefix replayed cleanly, but adds no failure-outcome evidence**

## Design

The earlier `record-contract` experiment branched before any repository inspection. This run uses
the same retained source session at step 8, after the agent inspected the failing check and both
implementation files but immediately before its first `apply_patch` executes. The exact prefix ID
is `e55f9f877357634da889ce9b062b104e2131369571bf9b46fc38423734877072`.

One pair (two independent continuations) resumed with the identical `Continue the task.` prompt and
the pinned `gpt-5.6-sol` model. The source did not persist reasoning effort, so the run retained the
Codex configuration default and remains in the legacy unpinned-effort stratum. The mechanical scorer
was `python3 check.py`.

## Result

| Measure | Result |
| --- | ---: |
| Judgeable neutral pairs | 1/1 |
| Passing arms | 2/2 |
| Mechanical outcome disagreements | 0/1 (0%) |
| Environment mismatches | 0/1 |
| Infrastructure failures | 0/2 |
| Continuation tool calls | 4–4 |
| Continuation elapsed time | 21.389–23.241 seconds |
| Continuation reported tokens | 142,596–142,888 |

Both continuations independently applied the same two-file contract fix and passed the scorer. This
extends exact replay coverage from the initial-inspection boundary to the first edit decision, but it
does not change the qualification decision: the source and both continuations all ended in mechanical
success, so the instrument still has no observed failure-outcome noise.

The derived machine-readable result is
[`fork-neutral-edit-decision.json`](fork-neutral-edit-decision.json). The append-only raw JSONL
remains at `~/.spotter/experiments/019ffb20-aa92-7f32-857d-e3bcf7f3ebb2-step8.jsonl` with SHA-256
`a7dd3324d05cff4c063bf5efd781be5113b0a5c544830fd8d79270f8ebeb8c8e`.

## Qualification impact

The legacy unpinned-effort stratum now contains 12 pairs across four prefixes and three tasks, with
0/12 mechanical disagreements, 0/12 environment mismatches, and 0/24 infrastructure failures. The
new prefix is not an independent task family, and reasoning effort is still unavailable, so it must
not tighten a representative causal bound. #42 remains open for failures that survive to final
mechanical outcomes and broader natural environment drift.
