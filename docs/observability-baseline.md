# Observability ceiling baseline

This document separates the measurement instrument from the evidence it has collected. The
instrument exists; the post-App-Server ceiling required by
[#37](https://github.com/spotter-agent/spotter/issues/37) is not yet measured.

## What is measured

`spotter observability` reports three distinct projection stages:

```text
Codex/App Server source field shapes
          ↓
normalized Trace IR
          ↓
live ThreadState
```

It also reports Hook and App Server Trace IR separately. Counts are grouped by evidence family, not
collapsed into one event percentage. This prevents a parser or state-projection loss from being
misreported as a structural source limit.

Coverage uses the following explicit vocabulary:

| Status | Meaning |
| --- | --- |
| `OBSERVED_EXACT` | the relevant exposed fact was preserved |
| `OBSERVED_PARTIAL` | useful evidence survived, but not the complete fact |
| `OBSERVED_ENCRYPTED` | only encrypted/opaque content was exposed |
| `OBSERVED_UNCORRELATED` | evidence exists without proven thread/turn identity |
| `SOURCE_NOT_EXPOSED` | the runtime did not expose the required fact |
| `ADAPTER_DROPPED` | source or Trace IR exposed the fact, but a later projection lost it |
| `OBSERVATION_GAP` | the fact may have occurred during a known disconnect gap |
| `LEGACY_UNAVAILABLE` | an older record lacks the provenance needed to judge it |
| `NOT_PERFORMED` | Main never performed the evidence-gathering action |
| `NOT_APPLICABLE` | the evidence family is irrelevant to this opportunity |
| `UNKNOWN` | available evidence cannot support a stronger classification |

For a labeled failure or degraded trajectory, required evidence is classified relative to the
failure/decision boundary as `VISIBLE_IN_TIME`, `VISIBLE_TOO_LATE`, `STRUCTURALLY_INVISIBLE`,
`LOST_BY_ADAPTER`, `LOST_BY_GAP`, or `UNJUDGEABLE`. Missing, encrypted, or late facts are never
reconstructed.

## Source audit safety

The daemon retains a bounded audit at `sessions/source-audit/samples.jsonl`. Samples contain method
names, field paths, normalized field names, evidence families, correlation identity, epoch, and
coverage classifications. They do not contain source values, command output, reasoning content, or
encrypted content. The file is mode `0600`, fsynced, and compacted to the most recent 1,000 samples
after it grows beyond 2,000.

The synthetic conformance corpus in `tests/fixtures/app_server_conformance.json` protects command,
file, MCP, plan, reasoning-summary, usage, lifecycle, unknown-method, and encrypted-child behavior.
It is regression coverage only; synthetic cases are not outcome evidence and do not contribute to
the measured ceiling.

## Current aggregate snapshot

On 2026-08-13, the available local dataset reported:

```text
sessions: 9 (hook=9, app_server=0)
events: 2659; observation_gaps: 0; unknown_events: 0
source-vs-adapter samples: none
session observability labels: 0/9
```

The Hook records predate current provenance and therefore classify as `LEGACY_UNAVAILABLE`; they
cannot establish exact source or timing coverage. There are no App Server journals, no raw-shape
samples, and no failed/degraded session labels in this snapshot. Consequently:

- no post-App-Server visible-in-time percentage can be stated;
- no Hook-to-App-Server improvement can be stated;
- no important failure-class ceiling can be stated; and
- the post-App-Server evidence gate remains unmet.

## Evidence still needed after #37's instrumentation work

Collect representative failed or degraded App Server trajectories, label each failure boundary and
minimal required evidence set, and run:

```bash
spotter observability
spotter metrics
```

The final report must include opportunity counts and label coverage, evidence/failure-family rows,
gap contamination, `NOT_PERFORMED` versus source limits versus projection loss, and earliest
source/Trace IR/ThreadState availability where timestamps exist. That evidence—not the existence of
this command—determines whether the next bottleneck is the observation surface, adapter/state
projection, or detector/reviewer.
