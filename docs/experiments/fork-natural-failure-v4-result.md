# Natural-failure capture v4 result

**Measured:** 2026-08-18

**Protocol commit:** `dc4cd0f`

**Decision:** **NO-GO remains; all replay sources were captured, but the new fixed split still had
no natural control failure**

## Protocol compliance

The new corpus and [protocol](fork-natural-failure-v4-protocol.md) were committed before paid
execution. Static validation proved exact task/set hashes, and preflight proved that all four initial
fixtures failed while every manifest-owned known-good transform passed the same required scorer.

The committed worktree ran from an isolated experiment virtual environment with dedicated portable
Codex and Spotter homes. The user's Codex configuration and Hooks were not changed. Exactly one
batch ran with:

- `gpt-5.6-sol`;
- `low` reasoning effort;
- the runner's `workspace-write` sandbox default;
- Codex CLI 0.147.0;
- four previously unexecuted qualification tasks;
- replay-source capture requested for all eight arms.

## Mechanical result

| Task | Family | Control | Guidance | Eligible natural failure source |
| --- | --- | --- | --- | --- |
| `workflow-scheduler` | dependency ordering | PASS | PASS | no |
| `jsonl-recovery` | durable-state recovery | PASS | PASS | no |
| `endpoint-policy` | security validation | PASS | PASS | no |
| `trace-correlation` | out-of-order correlation | PASS | PASS | no |

All eight agents exited normally. Setup and the frozen required scorer returned 0 for every arm.
There were no scorer, timeout, setup, or infrastructure failures. Guidance arms remain execution and
capture coverage only because their full prompt differs from control.

The selection rule admitted only `control && TASK_FAIL`. The eligible count was 0/4, so the fixed
stop rule started no coverage selection or neutral fork. The batch was not rerun and no task was
removed or replaced after observing the result.

## Capture readiness and source coverage

The #307 guard passed before the batch header and first model arm. The non-secret receipt pinned:

| Field | Value |
| --- | --- |
| Integration generation | `1c2decc1ea8db29350fb82d9c7b4b646a4effcb8449b66d5defd09a8dee4ec44` |
| Setup build | `source` |
| Hook command SHA-256 | `d671737e76c7fbe6b6dad0104834a71ceaf82f64c0e36f62e1e03affb2a03060` |
| Config generation | `cfg-b337cb31971e13c13e4b` |

Replay-source capture succeeded for 8/8 arms with no capture error. Combined with v3, both
post-#307 cohorts have captured 14/14 requested sources. The portable daemon was stopped after this
batch.

## Provenance

The raw append-only batch, journals, snapshots, and source workspaces remain in retained ignored
local evidence storage. They are not committed because the batch contains bounded
transcript-derived output and local paths.

| Field | Value |
| --- | --- |
| Run ID | `904ea6b1-af5d-4790-ad72-a960d9cd11e3` |
| Task-set SHA-256 | `c033efceb1a8d2a52674267590997afed21dcb2bd57b62c1746ba6a9ec97d84d` |
| Raw batch SHA-256 | `2ffb64d89b218c12582659fd6aebe5cef0e0405f31699305e8f713631042fdc2` |
| Raw size | 45,184 bytes |
| Started | `2026-08-18T00:16:13.789754+00:00` |
| Finished | `2026-08-18T00:28:26.230343+00:00` |

The bounded machine-readable result is
[`fork-natural-failure-v4-result.json`](fork-natural-failure-v4-result.json).

## Qualification and calibration impact

The split adds four independent task families and complete captured-source coverage, but one 4/4
control pass cannot establish that the tasks are universally trivial. It does establish that this
fixed run failed to sample the natural failure region for the selected model/config. Adding more
synthetic constraints after seeing this result would be outcome-driven benchmark tuning, so this
version remains immutable.

#42 remains open and the v1 `NO_GO` decision remains unchanged. The next cohort should be
predeclared from externally sourced real-repository tasks or another independently calibrated
difficulty frame; it must not rerun or rewrite this split to search for a failure.
