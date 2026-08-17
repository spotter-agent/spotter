# App Server observability v1 predeclaration

**Declared:** 2026-08-18, before cohort execution

**Issue:** [#303](https://github.com/spotter-agent/spotter/issues/303)

This plan freezes the cohort, classifications, minimum coverage, and stop rule used to decide
whether App Server evidence can replace each observation Hook responsibility. The resulting report
must cite the commit containing this file and must not silently remove failed or unsupported rows.

## Runtime and retained evidence

- Run Codex CLI 0.147.0 or newer against one explicitly selected local WebSocket App Server.
- Run `spotterd` from this revision with a fresh, isolated `SPOTTER_HOME`; no pre-existing Hook or
  App Server journals may enter the cohort.
- Use throwaway fixture repositories and an isolated `CODEX_HOME`. Authentication may be linked
  read-only from the local Codex installation, but user configuration and sessions are excluded.
- Retain Spotter journals, source-audit samples, daemon logs, bounded observer summaries, and the
  exact commands used. Commit only shape/timing/classification summaries and SHA-256 digests; raw
  values, model text, command output, secrets, and local absolute paths stay out of Git.
- Synthetic conformance fixtures and unit tests remain regression evidence only and never count as
  cohort samples.

## Cohort

Each scenario gets at most two attempts. An unavailable capability or second failed attempt remains
an explicit gap and forces the affected Hook decision to **NO-GO**.

| ID | Required trajectory | Minimum evidence |
| --- | --- | --- |
| `ASO-NORMAL-1` | new thread, initial user turn, successful command | thread/turn identity, input provenance, streamed command start/completion, exit status |
| `ASO-NORMAL-2` | later user turn on the same thread | later input distinguishable from initial input, history, replay, and Spotter steer |
| `ASO-FAIL-1` | command exits non-zero | failed outcome and exit status visible before turn completion |
| `ASO-PATCH-1` | file mutation followed by verification | patch/change identity and resulting diff or explicit source limitation |
| `ASO-MCP-1` | deterministic local MCP call | MCP start/completion, tool identity, success/failure outcome |
| `ASO-LONG-1` | long-running command interrupted with `turn/interrupt` | streaming lifecycle, interruption request, partial/unknown result, final turn status |
| `ASO-APPROVAL-1` | command reaches an approval boundary and is not approved | approval request, blocked/denied outcome, no invented execution result |
| `ASO-RECONNECT-1` | observer disconnects during an active command, then reconnects | explicit gap, connection epoch, stored/reconciled state, post-reconnect completion |
| `ASO-LIFECYCLE-1` | observer attaches late, reads stored state, resumes, and reconnects | new, stored, late-attached, resumed, and reconnected thread identity |

The minimum complete cohort is seven distinct App Server threads and nine turns, with every row
attempted and at least one classified sample for each required evidence item. Scenarios may share a
thread only where the table requires continuity (`ASO-NORMAL-1/2`) or disconnect state
(`ASO-RECONNECT-1/ASO-LIFECYCLE-1`).

## Inclusion and exclusion

Include every source notification and canonical record from the first declared scenario start
through the final scenario stop, including duplicates, unknown methods, uncorrelated evidence,
connection gaps, and failed attempts. Include a trajectory only when its scenario ID, thread ID,
attempt number, start/end time, and expected boundary were recorded before its first user turn.

Exclude readiness probes completed before the first scenario, daemon self-health traffic, and
threads whose working directory is outside the throwaway cohort root. Exclusions are reported by
reason and count. Missing identity, late attachment, loss, or unsupported protocol behavior is a
classification, never an exclusion.

## Measurement

For each required fact, classify source, Trace IR, and ThreadState independently using the existing
coverage vocabulary. For consequential failure boundaries, also classify earliest availability as
`VISIBLE_IN_TIME`, `VISIBLE_TOO_LATE`, `STRUCTURALLY_INVISIBLE`, `LOST_BY_ADAPTER`, `LOST_BY_GAP`,
or `UNJUDGEABLE`. Report opportunity denominators by responsibility and failure family rather than
using raw event counts as semantic coverage.

The report keeps these buckets separate:

- source-not-exposed / encrypted / uncorrelated;
- normalization or adapter loss;
- ThreadState loss;
- observation gaps and late evidence;
- not performed / not applicable;
- unknown or unjudgeable.

Overlap deduplication is exercised by attaching Hook and App Server observation to at least one
successful command and one file mutation, once in each arrival order when the runtime permits it.
The report compares canonical semantic actions and metric inputs, not notification counts.

## Labels

Every failed, degraded, interrupted, approval-blocked, and reconnect trajectory receives a session
visibility label. Each consequential boundary also receives a stable opportunity ID, semantic
window, observable window, and required evidence steps via `spotter label-opportunity`. A second
labeler is not required for this ceiling experiment, but the rater identity is recorded.

## Stop rule

Stop collection at the first of:

1. every cohort row and overlap order meets its minimum coverage;
2. a row reaches two attempts without its minimum evidence; or
3. a runtime or safety failure makes further collection unsafe.

Rules 2 and 3 do not permit substitution with a similar event. The report records the shortfall
and the affected responsibility remains **NO-GO**. Token cost, elapsed time, or a favorable interim
rate is not an early-stop condition.

## Per-Hook decisions

- `SessionStart` is **GO** only if every lifecycle variant has correlated identity and bootstrap
  state by its decision boundary, with no consequential source, adapter, state, or gap loss.
- `UserPromptSubmit` is **GO** only if all initial/later user inputs are correlated and remain
  distinguishable from Spotter steer, other clients, replay, and historical input.
- `PostToolUse` is **GO** only if command success/failure, patch, MCP, approval, interruption,
  reconnect, and partial/unknown outcomes are classified in time with no consequential unknown.
- Any unmet minimum, unjudgeable consequential boundary, or semantic-action duplicate produces
  **NO-GO** for that responsibility. Decisions are independent; there is no aggregate Hook verdict.

