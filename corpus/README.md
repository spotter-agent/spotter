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

The arm builder pins neutral control, raw imperative, scoped advisory, and optional VERIFY-first conditions to one source session/step, prefix identity, and environment fingerprint. A neutral arm emits no steer payload. Preparation creates four independent Git-aware replay forks and refuses missing manifests, shared worktrees, source-environment drift, prefix mismatch, or restored-environment mismatch before delivery.

Delivery resumes each fork, starts the same neutral continuation through App Server `turn/start`, and invokes real `turn/steer` only for the three nudge conditions. Continuation and steer inputs use separate correlation IDs. A rejected or stale steer remains `DELIVERY_FAILED_OR_STALE`; RPC acceptance is recorded separately from observed completion and must not be interpreted as compliance.

After an arm's terminal event is observed, the experiment runs the frozen task manifest's required checks in that arm's worktree. Each arm is fsynced immediately to a versioned experiment-result JSONL with task, prefix, fork, delivery, and check provenance. Missing completion remains `UNJUDGEABLE` without racing checks against a live turn; stale or rejected delivery stays separate even when the resulting workspace is mechanically scored.

Secondary trajectory annotations live in a separate, independently versioned JSONL so they cannot rewrite mechanical run evidence. They pin a fingerprint of the exact arm result, original-task ownership, conservative post-nudge relations, rater identity, and zero or more non-exclusive susceptibility classes. `REFUTED_WITH_EVIDENCE` requires a concrete evidence reference and `REFUTED_AND_CONTINUED`; compliance classes must agree with the mechanical PASS/TASK_FAIL result. Rejected, stale, or incomplete deliveries cannot receive semantic robustness labels.

Coverage-aware reports reload durable mechanical rows, recheck same-experiment prefix/task provenance, and pair each framing condition with its neutral control. They report delivery and completion coverage, mechanically judgeable pair coverage, control-pass → nudge-fail harm, semantic-label coverage, annotation conflicts/staleness, evidence-backed refutation, compliance, task replacement, constraint loss, and persistent contamination. Latest corrections are selected per rater; disagreeing current raters reduce coverage instead of being silently resolved.

Render an offline report without rerunning any agent arm:

```bash
spotter wrong-nudge report ~/.spotter/experiments/wrong-nudges/<run>.jsonl \
  --annotations ~/.spotter/experiments/wrong-nudges/<experiment>-annotations.jsonl
```

Execute one frozen wrong-nudge item from an equivalent recorded prefix only after explicitly
accepting the paid four-arm run:

```bash
spotter wrong-nudge run corpus/wrong-nudges-v1.toml \
  --task-set corpus/dev-v2.toml \
  --wrong-nudge-id wrong/query-parser-false-cause-001 \
  --session <source-session> --step <source-step> \
  --endpoint ws://127.0.0.1:<port> --run
```
