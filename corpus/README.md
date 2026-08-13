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

Run paid control/guidance arms from independent clean fixture copies:

```bash
spotter tasks run corpus/dev-v1.toml --guidance "Inspect the failing check first." --run
spotter tasks run corpus/dev-v1.toml --guidance "Inspect the failing check first." --run \
  --resume ~/.spotter/experiments/task-batches/<batch>.jsonl
```

Resume refuses changed task-set hashes, environment, guidance, model, or sandbox settings and skips already journaled arms. These synthetic fixtures establish harness behavior. They are not evidence of intervention advantage and do not replace the later executed experiment across a larger heterogeneous corpus.
