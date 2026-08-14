# Fork fidelity qualification v1

**Measured through:** 2026-08-14

**Decision:** **NO-GO for representative causal use by #34 or #23**

## Scope

This report consolidates the historical coverage baseline and six fresh identical-arm runs. It is
the versioned fidelity artifact downstream experiments can cite instead of assuming replay validity.
The decision applies to representative causal claims, not to continued development experiments.

## Coverage

| Cohort | Sessions | Candidates | Exact | Pre-mutation exact |
| --- | ---: | ---: | ---: | ---: |
| Historical retained journals | 8 | 1,246 | 0 | 0/120 |
| Fresh Hook-observed tasks | 5 | 27 | 17 | 9/11 |

The historical cohort is not executable evidence: missing snapshots/context and unknown external
effects make every candidate ineligible. Fresh baseline capture and call correlation reach early
read-only and scored-decision points, including a prefix after an incomplete patch that later failed
the scorer and required recovery.

## Neutral outcome strata

| Stratum | Tasks / prefixes | Pairs | Passing arms | Disagreements | Env mismatch | Infra failure |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Legacy unpinned effort | 3 / 4 | 12 | 24/24 | 0/12 | 0/12 | 0/24 |
| Pinned `low` recovery | 1 / 1 | 3 | 6/6 | 0/3 | 0/3 | 0/6 |
| Pinned `low` induced final failure | 1 / 1 | 3 | 0/6 | 0/3 | 0/3 | 0/6 |

The strata are not pooled. Reasoning effort was not persisted for the first three experiments, while
the recovery and induced-failure cohorts pin and prove `low` effort on both source and
continuations. The induced cohort also changes the source context deliberately, so it is not pooled
with the naturally recovered prefix despite sharing model and effort.

For orientation only, treating repeated pairs as independent Bernoulli observations gives one-sided
95% zero-event upper bounds of 22.09% for 0/12 and 63.16% for each 0/3 stratum. Those bounds are
**not valid cross-task qualification bounds** because pairs repeat the same prefixes. With only
three, one, and one independent task families respectively, no useful hierarchical estimate is
identifiable.

## Instrument controls now enforced

- shared prefix and captured-environment parity before either arm runs;
- distinct resolved worktrees for both arms;
- environment re-fingerprint immediately before each model continuation;
- source/continuation model and reasoning-effort parity when pins are supplied;
- explicit exclusion after observation gaps or unresolved external effects;
- session-start baseline for early pre-mutation branch coverage.

## Why the decision is NO-GO

The current result shows no observed disagreement, environment mismatch, or infrastructure failure
on eligible fresh prefixes. It does not establish that the true neutral noise is small enough for
causal deltas:

- no neutral pair has disagreed, so the failure region remains uncalibrated;
- the first six final-outcome-failure arms all failed, but their source failure was deliberately
  induced rather than naturally observed;
- the fresh sample is five synthetic Python tasks;
- the largest stratum lacks persisted effort provenance;
- undeclared ignored files and environment variables remain explicitly uncaptured.

#34 and #23 may use this instrument for development runs, but must not cite v1 as a representative
noise bound or interpret an intervention delta as causal. Qualification needs naturally occurring
final-outcome failure prefixes, more independent task families, and explicit
environment-resource/drift cases.

## Evidence index

- [Historical coverage baseline](fork-coverage-baseline.md)
- [First fresh exact-prefix run](fork-neutral-first-run.md)
- [Early pre-mutation run](fork-neutral-early-prefix.md)
- [Routing-decision run](fork-neutral-routing-decision.md)
- [Pinned low-effort recovery-prefix run](fork-neutral-recovery-prefix.md)
- [Edit-decision run](fork-neutral-edit-decision.md)
- [Induced final-outcome-failure run](fork-neutral-final-outcome-failure.md)
- [Machine-readable qualification](fork-fidelity-v1.json)
