# Spotter task corpus

`dev-v2.toml` is the current harness/supervision tuning set. `validation-v2.toml` is the current frozen held-out split for the first decision-quality measurements. Each contains three disjoint tasks; together they cover localized fixes, missing validation, regression avoidance, evidence inspection, and multi-file contracts. The v1 sets remain immutable for provenance.

Changing a fixture or task manifest requires a new set version and new hashes; do not rewrite an observed validation set in place. In-place re-freezing is allowed only before the set has been used in a recorded run.

Static freeze validation is safe for untrusted input:

```bash
spotter tasks validate corpus/dev-v1.toml
spotter tasks validate corpus/validation-v1.toml
spotter tasks validate corpus/dev-v2.toml
spotter tasks validate corpus/validation-v2.toml
```

Preflight executes the repo-authored setup and scorer commands in temporary fixture copies:

```bash
spotter tasks preflight corpus/dev-v1.toml
spotter tasks preflight corpus/validation-v1.toml
spotter tasks preflight corpus/dev-v2.toml
spotter tasks preflight corpus/validation-v2.toml
```

Run paid control/guidance arms from independent clean fixture copies:

```bash
spotter tasks run corpus/dev-v2.toml --guidance "Inspect the failing check first." --run
spotter tasks run corpus/dev-v2.toml --guidance "Inspect the failing check first." --run \
  --resume ~/.spotter/experiments/task-batches/<batch>.jsonl
```

Resume refuses changed task-set hashes, environment, guidance, model, or sandbox settings and skips already journaled arms. These synthetic fixtures establish harness behavior. They are not evidence of intervention advantage and do not replace the later executed experiment across a larger heterogeneous corpus.

## Wrong-nudge corpus

`wrong-nudges-v1.toml` freezes plausible false guidance for the first #23 susceptibility runs. Each manifest records the false premise, contradictory evidence already available to Main, intended scope, payload version, and expected healthy response. Manifest hashes prevent an observed item from being silently rewritten; corrections require a new corpus version.

This corpus is experiment input, not evidence that Main rejects bad supervision. Control, raw-imperative, and Spotter-advisory arms still need equivalent prefixes before drawing safety conclusions.
