# Routing-decision identical-arm fork run

**Measured:** 2026-08-13

**Source:** frozen `routing-investigation` validation fixture

**Decision:** **no outcome noise observed near this scored decision; seek non-ceiling tasks next**

## Design

The fixture requires lowercase-method and single-trailing-slash normalization without accepting a
double trailing slash. A fresh source session inspected the route table, application wrapper, scorer,
and all call sites before proposing the correct one-file patch.

The experiment selected step 8, immediately after that evidence inspection and before the patch,
with prefix ID `2cd0da0338ed463361a7ef7cc0b96d375b7a4b3ff3715bf3c0f530de15a6c3a9`.
This is a mechanically scored decision opportunity: an over-broad slash-stripping implementation
fails `python3 check.py`. Five pairs (ten independent continuations) resumed with the identical
`Continue the task.` prompt and pinned `gpt-5.6-sol` model.

## Result

| Measure | Result |
| --- | ---: |
| Judgeable neutral pairs | 5/5 |
| Passing arms | 10/10 |
| Mechanical outcome disagreements | 0/5 (0%) |
| Environment mismatches | 0/5 |
| Infrastructure failures | 0/10 |
| Continuation tool calls | 5–7 |
| Continuation elapsed time | 21.972–35.388 seconds |
| Continuation reported tokens | 148,449–204,601 |

The hidden negative case did not create outcome disagreement: every continuation preserved unknown
routes. Tool and token variation remained visible despite identical scored outcomes.

The derived machine-readable result is
[`fork-neutral-routing-decision.json`](fork-neutral-routing-decision.json). The raw local JSONL
remains at `~/.spotter/experiments/019ffb34-3373-7901-a3d1-b175d5616ac5-step8.jsonl` with SHA-256
`c954c58ee4e4e58b1f68bbbd8c7ff1c89c1aca3638cbf8a88e6c5a502957ac11`.

## Coverage and limitations

The source exposed six proposals: five exact and one excluded after a Class C external effect. All
three pre-mutation candidates were exact. Across three fresh tasks, Spotter has now observed 0/11
mechanical disagreements, 0/11 environment mismatches, and 0/22 infrastructure failures.

This run deliberately moved from an initial prefix to a scored implementation decision, but all ten
arms still passed. The next #42 sample must come from a task/prefix whose observed continuations
include mechanical failures; adding more repetitions to this ceiling task would not tighten the
noise estimate where #34 and #23 need it.
