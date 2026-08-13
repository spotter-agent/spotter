# Historical fork-coverage baseline

**Measured:** 2026-08-13

**Instrument:** `spotter fork-coverage` introduced by the #42 branch-coverage slice

**Decision:** **NO-GO for neutral continuation runs on this historical sample**

## Result

The read-only coverage scan evaluated every tool proposal in the eight retained Spotter journals
that also had a Codex rollout. None provided a clean exact prefix under the current causal-safety
rules.

| Session | Candidates | Exact | Missing state | Missing context | Unsafe/unknown external state | Gap |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `019fee58-ab26-72f2-b00e-0a2be2c04fe5` | 244 | 0 | 0 | 1 | 243 | 0 |
| `019fee8e-5efe-7861-9bca-1104e5635c76` | 62 | 0 | 0 | 1 | 61 | 0 |
| `019feea7-28a4-7cc1-b704-fe9ad102d3ec` | 180 | 0 | 1 | 0 | 179 | 0 |
| `019fef84-c367-75c0-bed1-0e1dac06fe94` | 57 | 0 | 1 | 0 | 56 | 0 |
| `019fefe6-d2f6-7c82-9765-0d8d29239783` | 69 | 0 | 1 | 0 | 68 | 0 |
| `019ff34e-9057-7181-8387-021b717aba65` | 600 | 0 | 1 | 0 | 599 | 0 |
| `019ff4c2-b8aa-7261-9068-3fcc89910a9b` | 30 | 0 | 1 | 0 | 29 | 0 |
| `019ff55f-2ec5-7592-995f-1901579c9c38` | 4 | 0 | 1 | 0 | 3 | 0 |
| **Total** | **1,246** | **0** | **6** | **2** | **1,238** | **0** |

Pre-mutation coverage was **0/120** candidate proposals. No earliest clean forkable step exists in
this sample.

## Interpretation

These journals predate complete reversibility/effect classification. After an unclassified proposal,
the instrument cannot prove that the world state remained local and reversible, so later prefixes
are conservatively `UNSAFE_EXTERNAL_EFFECT`; that label means **unknown or unsafe**, not proof that
1,238 external writes occurred. A few first proposals instead fail immediately because their Git
snapshot object or rollout correlation is unavailable.

Running neutral continuations from these prefixes would mix stochastic model variance with unknown
external-state drift, so no noise rate is reported. This is an evidence-bearing null result, not an
infrastructure success disguised as zero noise.

## Next qualification

Collect fresh Hook-observed sessions after the baseline-checkpoint and reversibility provenance
changes, rerun `fork-coverage`, and execute `spotter experiment --neutral` only for prefixes reported
as `FORKABLE_EXACT`. #42 remains open until representative exact prefixes establish outcome noise
and coverage around relevant intervention opportunities.

The first such [fresh identical-arm run](fork-neutral-first-run.md) subsequently found one exact
post-mutation prefix and 0/3 mechanical disagreements across its repeated neutral pairs. It does not
change this historical sample's NO-GO decision or establish representative early-prefix coverage.
