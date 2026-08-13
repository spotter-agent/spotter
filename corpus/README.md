# Spotter task corpus

`dev-v1.toml` is for harness and supervision tuning. `validation-v1.toml` is the frozen held-out split for the first decision-quality measurements. Changing a fixture or task manifest requires a new set version and new hashes; do not rewrite an observed validation set in place. In-place re-freezing is allowed only before the set has been used in a recorded run.

Static freeze validation is safe for untrusted input:

```bash
spotter tasks validate corpus/dev-v1.toml
spotter tasks validate corpus/validation-v1.toml
```

Preflight executes the repo-authored setup and scorer commands in temporary fixture copies:

```bash
spotter tasks preflight corpus/dev-v1.toml
spotter tasks preflight corpus/validation-v1.toml
```

These synthetic fixtures establish harness behavior. They are not evidence of intervention advantage and do not replace the later multi-task executed experiment.

## Experiment arm results

`spotter experiment` persists one classification for each control or guidance arm:

| Classification | Meaning |
| --- | --- |
| `PASS` | The agent completed and the mechanical check passed. |
| `TASK_FAIL` | The agent completed but the mechanical check failed. |
| `SETUP_FAIL` | Task setup could not establish the required initial state. Reserved by the result schema; the current single-experiment command does not perform corpus setup. |
| `INFRA_FAIL` | The agent process could not start or exited unsuccessfully. |
| `TIMEOUT_AGENT` | The agent exceeded its timeout. |
| `TIMEOUT_CHECK` | The mechanical check exceeded its timeout. |
| `CHECK_ERROR` | The check could not be executed. |
| `UNJUDGEABLE` | No mechanical result is available, including a dry run or a run without a check. |

Only `PASS` and `TASK_FAIL` are mechanically judged task outcomes. Pair comparisons exclude every
other classification; those results report experiment coverage or infrastructure/scorer failure,
not agent or intervention success. Check stdout and stderr are persisted in bounded form for
diagnosis. The schema does not infer success from transcript text.
