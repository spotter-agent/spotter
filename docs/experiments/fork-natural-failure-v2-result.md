# Natural-failure capture v2 result

**Measured:** 2026-08-17

**Protocol commit:** `c5cc70d`

**Decision:** **NO-GO remains; the fixed cohort had no natural control failure and replay-source
capture was unavailable**

## Protocol compliance

The [protocol](fork-natural-failure-v2-protocol.md) was committed before paid execution. The frozen
validation-v2 manifest and all three task setup/precheck/known-good scorer paths passed before the
batch started.

Exactly one batch ran with:

- `gpt-5.6-sol`;
- `low` reasoning effort;
- `workspace-write` sandbox;
- Codex CLI 0.147.0;
- three frozen validation-v2 tasks;
- replay-source capture requested for all six arms.

The initial `python -m spotter.cli` invocation was a no-op because that module does not call
`main()`. It started no paid arms and created no batch. Validation, preflight, and the fixed batch
then used the real `python -m spotter` entrypoint.

## Mechanical result

| Task | Control | Guidance | Eligible natural failure source |
| --- | --- | --- | --- |
| `profile-contract` | PASS | PASS | no |
| `routing-investigation` | PASS | PASS | no |
| `record-contract` | PASS | PASS | no |

All six agents exited normally. Setup and the frozen required scorer returned 0 for every arm.
Guidance arms are reported for execution coverage only; their extra suffix means they are not neutral
evidence and were never eligible natural-failure sources.

The protocol admitted only `control && TASK_FAIL`. The eligible count was 0/3, so the fixed stop
rule prevented any neutral fork run. The batch was not rerun to search for a failure.

## Replay-source capture failure

Capture was requested for 6/6 arms and succeeded for 0/6. Every row reported the same bounded class:

```text
SPOTTER_JOURNAL_MISSING_OR_UNREADABLE
```

The active Codex configuration had no Spotter Hook registration, and `spotter status` reported that
Spotter was not configured for Codex. `--dangerously-bypass-hook-trust` can bypass trust for an
existing Hook; it cannot install a missing one. The task runner checked capture only after each paid
arm finished, so the entire batch incurred model execution without producing a replay source.

This was tracked as [#307](https://github.com/spotter-agent/spotter/issues/307), which blocked the
next #42 capture attempt. A future capture cohort was not allowed to run until the runner could prove
the selected Codex/Spotter integration and effective `SPOTTER_HOME` before the first paid arm.

## Provenance

The raw append-only batch remains in ignored local evidence storage. It is not committed because it
contains bounded transcript-derived output and local paths.

| Field | Value |
| --- | --- |
| Run ID | `d52b4f98-d781-447a-aa2f-f9c0a7075a3d` |
| Task-set SHA-256 | `b232d3fc3647a1e0924822ddd6530479f5933c9b61374db2106cd0cbd4040c7e` |
| Raw batch SHA-256 | `3bcf75713e1c4b7b79d1b84f000e2d53a8f6c6beb56f5b0bc960616b61224106` |
| Raw size | 36,332 bytes |
| Started | `2026-08-17T10:53:11.097333+00:00` |
| Finished | `2026-08-17T10:59:32.296652+00:00` |

The bounded machine-readable result is
[`fork-natural-failure-v2-result.json`](fork-natural-failure-v2-result.json).

## Qualification impact

This is a valid null result for one predeclared three-task control cohort: no naturally occurring
final failure was observed. It does not estimate failure-region replay noise, prove that failures are
rare, or add representative task-family breadth.

#42 remains open and the v1 `NO_GO` decision remains unchanged. At measurement time, the next
attempt was blocked by #307. That guard later closed and the
[v3 cohort](fork-natural-failure-v3-result.md) passed readiness and captured 6/6 sources, but still
found no natural control failure. #42 continues to need naturally occurring final failures, broader
task families, labeled intervention-opportunity coverage, natural drift, and neutral repetitions.
