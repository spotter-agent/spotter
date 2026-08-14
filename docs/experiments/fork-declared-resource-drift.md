# Declared ignored-resource drift preflight

**Measured:** 2026-08-14

**Decision:** **the source-to-fork guard correctly blocks declared ignored-file loss before either agent arm runs**

## Scope

This is a post-v1 qualification of the declared-resource mechanism added for
[#42](https://github.com/spotter-agent/spotter/issues/42). It reuses the retained routing source
session and its exact step-8 prefix:

```text
source session: 019ffb34-3373-7901-a3d1-b175d5616ac5
branch step: 8
prefix: 2cd0da0338ed463361a7ef7cc0b96d375b7a4b3ff3715bf3c0f530de15a6c3a9
```

A temporary, non-secret `.fixture-config` file was added to the retained source worktree together
with an exact `.gitignore` rule. This deliberately creates the failure mode where both Git-restored
arms lose the same ignored input and would otherwise match each other. The two temporary files were
removed immediately after the run; the source's prior tracked and untracked state was preserved.

The command exercised the real CLI, fork manifests, pair preflight, result persistence, and cleanup:

```bash
spotter experiment \
  --session 019ffb34-3373-7901-a3d1-b175d5616ac5 \
  --step 8 \
  --neutral --pairs 1 \
  --check "python3 check.py" \
  --environment-resource .fixture-config \
  --run
```

`--run` was necessary to exercise the execution admission path. The preflight rejected both arms
before `_run_arm`, so no Codex continuation or paid model call started.

## Result

| Arm | Classification | Source preflight | Agent started |
| --- | --- | --- | --- |
| `neutral_a` | `INFRA_FAIL` | `SOURCE_ENVIRONMENT_MISMATCH:MISSING_IGNORED_FILE` | no |
| `neutral_b` | `INFRA_FAIL` | `SOURCE_ENVIRONMENT_MISMATCH:MISSING_IGNORED_FILE` | no |

Both schema-v2 fork manifests retained the same prefix ID and recorded `.fixture-config` as missing
with no content hash in the restored worktree. The summary reported one environment mismatch across
one pair, two infrastructure failures across two arms, and zero judgeable outcome pairs. The fork
worktrees were removed through the normal Git-aware experiment cleanup path.

This proves the operational guard, not replay fidelity or a model noise rate. The drift was induced,
not naturally observed at the historical prefix. Undeclared ignored files, directories, environment
variables, external services, and final-outcome-failure coverage remain open qualification gaps.

## Artifacts

- Machine-readable scoped result: [`fork-declared-resource-drift.json`](fork-declared-resource-drift.json)
- Raw local experiment journal: `~/.spotter/experiments/019ffb34-3373-7901-a3d1-b175d5616ac5-step8.jsonl`
- Raw journal SHA-256 after this append: `0f1114884e5c53bcf07bcbe117dca7ba98e656bef86b0c568ca8198ce36eb083`
