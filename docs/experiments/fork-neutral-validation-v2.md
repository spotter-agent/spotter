# Validation v2 passing-prefix identical-arm fork run

**Measured:** 2026-08-14

**Source:** frozen `validation-v2` corpus with pinned `gpt-5.6-sol` / `low`

**Decision:** **passing-prefix breadth improved; naturally occurring failure noise remains unqualified**

## Design

The previously unexecuted `validation-v2` set contributed three mechanically scored tasks:
`profile-contract`, `routing-investigation`, and `record-contract`. A capture-only source batch ran
one control and one non-directive guidance arm per task. All six arms passed and retained replay
source sessions. The guidance suffix repeats the runner's control sentence, so those source-batch
pairs are not treated as exact-prompt neutral evidence.

Coverage was measured for all six retained sources. The neutral experiment then selected each
task's control source at its earliest exact pre-mutation proposal, step 2, and ran one exact-prompt
neutral pair per source. Model and reasoning effort were pinned and checked against source
provenance; `python3 check.py` was the mechanical scorer.

## Source capture and coverage

| Source task / arm | Candidates | Exact | Pre-mutation exact |
| --- | ---: | ---: | ---: |
| `profile-contract` guidance | 6 | 2 | 2/3 |
| `profile-contract` control | 5 | 4 | 2/2 |
| `routing-investigation` control | 6 | 5 | 3/3 |
| `routing-investigation` guidance | 7 | 4 | 2/2 |
| `record-contract` guidance | 4 | 4 | 2/2 |
| `record-contract` control | 4 | 4 | 2/2 |
| **Total** | **32** | **23** | **13/14** |

Every source had an exact step-2 branch point. No source had an observation gap or missing
state/context. Later proposals in four sessions were excluded after effects that the coverage model
does not treat as cleanly reversible; those exclusions are not counted as exact coverage.

## Neutral result

| Task | Source session | Pair outcome | Disagreement | Environment mismatch | Infra failure |
| --- | --- | --- | ---: | ---: | ---: |
| `profile-contract` | `019fff1f-feab-70a0-8689-b22eb9d08088` | 2/2 PASS | 0/1 | 0/1 | 0/2 |
| `routing-investigation` | `019fff20-6b17-74d3-a295-5dd881a3de7f` | 2/2 PASS | 0/1 | 0/1 | 0/2 |
| `record-contract` | `019fff22-19b3-7810-a5b7-dc6f8e6c54a0` | 2/2 PASS | 0/1 | 0/1 | 0/2 |
| **Total** | **3 prefixes** | **6/6 PASS** | **0/3** | **0/3** | **0/6** |

Continuation diagnostics ranged from 5 to 9 tool calls, 29 to 65 elapsed seconds, and 123,787 to
227,376 reported cumulative tokens. Sequence and cost spread are diagnostic only; mechanical
outcome disagreement remains the primary noise measure.

The raw source batch is
`~/.spotter/experiments/task-batches/spotter-validation-v2-801912d3-9eb5-408d-a958-ba3be34ed62f.jsonl`
with SHA-256 `944009c55d1c0871040f97793cd5f0017e575f048542fdd561690c9f48c21f5c`.
The three raw neutral results remain under `~/.spotter/experiments/`; their hashes and bounded
derived records are in
[`fork-neutral-validation-v2.json`](fork-neutral-validation-v2.json). Raw JSONL is not committed
because it contains bounded transcript-derived output and local paths.

## Qualification impact

This adds three pinned-low, single-pair passing prefixes and six more captured source sessions. It
also exercises one task family (`record-contract`) not present in the prior neutral evidence. No
naturally occurring source failure was observed, so the run does not calibrate failure-region
noise and does not change the v1 **NO-GO** decision for #34/#23.
