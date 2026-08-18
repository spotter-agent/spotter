# Natural-failure capture v3 result

**Measured:** 2026-08-18

**Protocol commit:** `1002292`

**Decision:** **NO-GO remains; capture succeeded for every arm, but the fixed cohort had no natural
control failure**

## Protocol compliance

The [protocol](fork-natural-failure-v3-protocol.md) was committed before paid execution. The frozen
development-v2 manifest and all three task setup/precheck/known-good scorer paths passed before the
batch started. The committed worktree was installed in an isolated experiment virtual environment,
and portable setup created dedicated Codex and Spotter homes without changing the user's Codex
configuration or Hooks.

Exactly one batch ran with:

- `gpt-5.6-sol`;
- `low` reasoning effort;
- the runner's `workspace-write` sandbox default;
- Codex CLI 0.147.0;
- three frozen development-v2 tasks;
- replay-source capture requested for all six arms.

An initial invocation supplied `--sandbox workspace-write`, which the CLI does not expose. Argument
parsing rejected it before validation, batch creation, capture readiness, or any model arm. The one
executed batch omitted that redundant option and retained the declared `workspace-write` default.

## Mechanical result

| Task | Family | Control | Guidance | Eligible natural failure source |
| --- | --- | --- | --- | --- |
| `query-parser` | localized bug fix | PASS | PASS | no |
| `settings-validation` | missing validation | PASS | PASS | no |
| `cache-regression` | regression avoidance | PASS | PASS | no |

All six agents exited normally. Setup and the frozen required scorer returned 0 for every arm.
Guidance arms are reported for execution and capture coverage only; their extra suffix means they are
not neutral evidence and were never eligible natural-failure sources.

The protocol admitted only `control && TASK_FAIL`. The eligible count was 0/3, so the fixed stop
rule prevented any neutral fork run. The batch was not rerun to search for a failure.

## Capture readiness and source coverage

The #307 guard passed before the batch header and first paid arm. It proved the exact owned portable
Hook integration, isolated homes, selected config generation, and reversible capture-only journal
round-trip. The non-secret receipt pinned:

| Field | Value |
| --- | --- |
| Integration generation | `d8a6575689093303b43cf55eddb535d10443f8140c8d8524d90f6e2dac21244c` |
| Setup build | `source` |
| Hook command SHA-256 | `a0b8598636cf1ceff64010a2009eb8228a7f475f348878e60a98f9cee875ba4d` |
| Config generation | `cfg-b337cb31971e13c13e4b` |

Replay-source capture then succeeded for 6/6 arms with no capture error. This closes the operational
uncertainty exposed by v2: a correctly configured isolated Hook path can now produce a replay source
for every paid task arm, and unavailable capture would have failed before model cost. The portable
daemon was stopped after the batch.

## Provenance

The raw append-only batch, journals, snapshots, and source workspaces remain in retained ignored
local evidence storage. They are not committed because the batch contains bounded
transcript-derived output and local paths.

| Field | Value |
| --- | --- |
| Run ID | `c618d8c5-95db-4ee9-9236-5bc1c7015afc` |
| Task-set SHA-256 | `d828672a717cac1c4acb214a127f32e09a0a099b6462d5b0638593d34ace60ff` |
| Raw batch SHA-256 | `aea0c866353988c3d6eea38f606ed113e94651a1e5d40935f53e47128661bdd4` |
| Raw size | 34,999 bytes |
| Started | `2026-08-17T23:58:25.237824+00:00` |
| Finished | `2026-08-18T00:03:00.183102+00:00` |

The bounded machine-readable result is
[`fork-natural-failure-v3-result.json`](fork-natural-failure-v3-result.json).

## Qualification impact

This is a valid null result for one predeclared three-task control cohort. It adds three task
families and proves complete replay-source capture after #307, but no naturally occurring final
failure was observed. It therefore does not estimate failure-region replay noise, prove that
failures are rare, or add any neutral disagreement measurement.

#42 remains open and the v1 `NO_GO` decision remains unchanged. Further work needs a new
predeclared, broader and harder corpus capable of producing naturally occurring failures; it must
not rerun this cohort to search for one.
