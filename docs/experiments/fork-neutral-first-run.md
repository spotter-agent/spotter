# First fresh identical-arm fork run

**Measured:** 2026-08-13

**Source:** fresh isolated Git fixture observed by the current Hook/replay implementation

**Decision:** **instrument works for this prefix; broader #42 qualification remains open**

## Design

A fresh Codex session fixed a deterministic two-test Python tag-normalization task. Coverage was
measured before spending on repetitions, and only its one `FORKABLE_EXACT` point was selected: step
7, immediately before the patch, with prefix ID
`32f06bdf4f2a6dba5b166c494861ecf52391d6d344599469d2749935e5b4624a`.

Three pairs (six independent continuations) resumed that exact prefix with the identical
`Continue the task.` prompt. Every pair passed shared-prefix and environment-fingerprint preflight.
The mechanical scorer was `python3 -m unittest -q`.

## Result

| Measure | Result |
| --- | ---: |
| Judgeable neutral pairs | 3/3 |
| Passing arms | 6/6 |
| Mechanical outcome disagreements | 0/3 (0%) |
| Environment mismatches | 0/3 |
| Infrastructure failures | 0/6 |
| Continuation tool calls | 4 each |
| Continuation elapsed time | 17.031–25.113 seconds |
| Continuation reported tokens | 102,801–135,073 |

This establishes a **zero observed mechanical disagreement for one repeated prefix**, not a global
zero-noise claim. Tool-count behavior was identical while elapsed time and reported tokens varied,
which is compatible with stochastic continuation/caching/runtime variance even when the scored
outcome agrees.

The derived machine-readable result is
[`fork-neutral-first-run.json`](fork-neutral-first-run.json). The raw local JSONL remains at
`~/.spotter/experiments/019ffaf5-9f25-7782-834a-e88b7ea7affb-step7.jsonl` with SHA-256
`72d6d93f99f8204d9f2dc0179247b9ce2241e59a1d468a56dd8a9ee4b43190bd`.

## Coverage and limitations

The fresh session exposed five candidate proposals: one exact, three without rollout correlation,
and one after an external-effect-classified command. Pre-mutation read-only coverage was 0/2 because
the machine's legacy Hook registration invokes Spotter for prompt/tool events but not
`SessionStart`; therefore the baseline-checkpoint code did not run for this session. That operational
registration gap must be fixed or migrated before claiming early-intervention coverage.

One small synthetic task and one prefix are not representative of the failure families consumed by
#34/#23. #42 should remain open until fresh exact prefixes cover multiple tasks and relevant
intervention opportunities, with repeats sufficient to bound outcome disagreement rather than merely
observe zero once.
