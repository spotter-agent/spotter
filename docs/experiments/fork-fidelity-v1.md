# Fork fidelity qualification v1

**Measured through:** 2026-08-14

**Decision:** **NO-GO for representative causal use by #34 or #23**

> **Erratum (2026-08-17):** v1 correctly classified prefixes after observation gaps or external
> effects as unsafe in coverage, but pair admission did not enforce those classifications before
> model execution. The measured outcomes below are unchanged. The machine-readable control is
> corrected to `false`; current code now rejects both contamination classes before either arm runs.

## Scope

This report consolidates the historical coverage baseline and fresh identical-arm evidence through
the validation-v2 run. It is
the versioned fidelity artifact downstream experiments can cite instead of assuming replay validity.
The decision applies to representative causal claims, not to continued development experiments.

## Coverage

| Cohort | Sessions | Candidates | Exact | Pre-mutation exact |
| --- | ---: | ---: | ---: | ---: |
| Historical retained journals | 8 | 1,246 | 0 | 0/120 |
| Fresh Hook-observed sessions | 11 | 59 | 40 | 22/25 |

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
| Pinned `low` validation-v2 pass | 3 / 3 | 3 | 6/6 | 0/3 | 0/3 | 0/6 |

The strata are not pooled. Reasoning effort was not persisted for the first three experiments, while
the recovery, induced-failure, and validation-v2 cohorts pin and prove `low` effort on both source
and continuations. The induced cohort also changes the source context deliberately. Validation-v2
uses three naturally passing control sources and one pair per prefix, adding breadth rather than
repeat precision.

For orientation only, treating repeated pairs as independent Bernoulli observations gives one-sided
95% zero-event upper bounds of 22.09% for 0/12 and 63.16% for each 0/3 stratum. Those bounds are
**not representative qualification bounds**: some strata repeat prefixes, validation-v2 has only
three task-prefix observations, and all tasks are synthetic Python fixtures.

## Instrument controls now enforced

- shared prefix and captured-environment parity before either arm runs;
- distinct resolved worktrees for both arms;
- environment re-fingerprint immediately before each model continuation;
- source/continuation model and reasoning-effort parity when pins are supplied;
- coverage classification, but not pair-admission enforcement, after observation gaps or external
  effects (corrected in current code after v1);
- session-start baseline for early pre-mutation branch coverage.

## Why the decision is NO-GO

The current result shows no observed disagreement, environment mismatch, or infrastructure failure
on eligible fresh prefixes. It does not establish that the true neutral noise is small enough for
causal deltas:

- no neutral pair has disagreed, so the failure region remains uncalibrated;
- the first six final-outcome-failure arms all failed, but their source failure was deliberately
  induced rather than naturally observed;
- validation-v2 added three passing prefixes but no naturally occurring source failure;
- the fresh sample remains a small synthetic Python corpus with repeated task families;
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
- [Validation-v2 passing-prefix run](fork-neutral-validation-v2.md)
- [Machine-readable qualification](fork-fidelity-v1.json)
