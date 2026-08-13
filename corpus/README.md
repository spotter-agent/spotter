# Spotter task corpus

`dev-v1.toml` is for harness and supervision tuning. `validation-v1.toml` is the frozen held-out split for the first decision-quality measurements. Changing a fixture or task manifest requires a new set version and new hashes; do not rewrite an observed validation set in place.

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
