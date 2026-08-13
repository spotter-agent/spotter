# Dev v2 first multi-task run

Date: 2026-08-13

Issue: [#21](https://github.com/spotter-agent/spotter/issues/21)

Status: complete

## Protocol frozen before execution

Hypothesis: one generic evidence-first guidance message may preserve or improve mechanical task
success versus the neutral continuation, particularly on validation and regression-sensitive tasks.

The development set `corpus/dev-v2.toml` is used for this harness qualification run. The held-out
`validation-v2` set remains unexecuted. Each of the three tasks receives one control arm and one
guidance arm from independent clean fixture copies; order is counterbalanced by the frozen batch
runner. This is six paid agent arms in total.

Control suffix:

```text
Continue the task.
```

Guidance suffix:

```text
Before editing, inspect the failing check and relevant code. Preserve unaffected behavior, make the
smallest justified fix, and run the task check afterward.
```

Each task's required mechanical scorer is the only success criterion. Agent, setup, check, timeout,
and infrastructure outcomes remain separate classifications. Declared wall-time and max-turn
budgets come from the frozen task manifests; the Codex backend enforces wall time but cannot enforce
the recorded max-turn declaration.

Decision rule: the instrument qualifies if all six arms produce durable explicit classifications
and all mechanically judgeable pairs can be reported without infrastructure outcomes entering the
task-success numerator. With only three development tasks and one continuation per arm, any observed
benefit, harm, or tie is descriptive and must not enable live intervention or tune the held-out set.

Execution command:

```bash
spotter tasks run corpus/dev-v2.toml \
  --guidance "Before editing, inspect the failing check and relevant code. Preserve unaffected behavior, make the smallest justified fix, and run the task check afterward." \
  --run
```

## Result

The batch completed successfully from 2026-08-13 10:42:10Z through 10:45:23Z (193.50 seconds).
All six arms produced durable `PASS` classifications with setup and required-check exit code 0.

| Task | Control | Guidance | Pair outcome |
| --- | --- | --- | --- |
| `fixture/query-parser-001` | `PASS` | `PASS` | tie-success |
| `fixture/settings-validation-001` | `PASS` | `PASS` | tie-success |
| `fixture/cache-regression-001` | `PASS` | `PASS` | tie-success |

Coverage and paired counts:

```text
executed arms:       6/6
mechanically judged: 6/6
usable pairs:        3/3
guidance better:     0
control better:      0
tie-success:         3
tie-failure:         0
infrastructure loss: 0
```

The requested model was the Codex configuration default. All six retained Codex rollout headers
resolved it to `gpt-5.6-sol`; the runner was `codex-cli 0.147.0`, Python was 3.12.13, and the sandbox
was `workspace-write`. Agent-reported token totals were 41,912 for control and 39,270 for guidance.
Summed arm elapsed time was 96.72 seconds for control and 96.77 seconds for guidance. These cost
numbers are descriptive only: one continuation per task is not a stable efficiency estimate.

All six agent stderr tails contained non-fatal MCP shutdown warnings. Agent exits and scorers still
returned 0, so they did not reduce mechanical coverage; they remain an operational diagnostic rather
than a task failure.

The raw result remains under Spotter-owned storage as
`experiments/task-batches/spotter-dev-v2-16532470-1be0-41e8-a0ea-b52f6f92b0a8.jsonl` relative to
`SPOTTER_HOME`. Its SHA-256 is
`948226a9d1fcd768b69023b22d0a2fedc121bc058220e8550bdbc6bf4fb9f434`. It is not committed because
it contains bounded transcript-derived agent output; the checked-in
[derived result](dev-v2-first-run.json) omits that content while preserving per-arm outcomes and
provenance.

## Interpretation

This run qualifies the #21 instrument end to end: frozen multi-task set, preflight, independent
arms, durable classified outcomes, coverage reporting, and a reproducible derived record all worked.
The observed result is a descriptive null/tie on three easy development tasks. It does not establish
intervention advantage, does not estimate the replay noise floor, and does not justify changing live
intervention defaults. `validation-v2` remains held out.
