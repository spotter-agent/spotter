# App Server observability v1 result

**Run date:** 2026-08-18  
**Issue:** [#303](https://github.com/spotter-agent/spotter/issues/303)  
**Predeclaration commit:** `b47eb611afaf61ddcc76e784426ccf7da5ef4e5f`  
**Decision:** **NO-GO for removing `SessionStart`, `UserPromptSubmit`, or `PostToolUse`**

The run stopped under rule 2 of the
[predeclared protocol](app-server-observability-v1-plan.md): `ASO-NORMAL-2` reached two attempts
without App Server evidence for the later user input. The second attempt did expose the turn,
streamed command start/completion, exit status, and terminal turn status, but Codex 0.147.0 emitted
no source notification containing the user input or its provenance. Unrun rows were not replaced
with easier trajectories and remain explicit missing minimums.

## Runtime and corrective findings

The isolated run used Codex CLI 0.147.0, one local WebSocket App Server, a fresh `SPOTTER_HOME`, a
fresh `CODEX_HOME`, and a throwaway Git repository. Raw prompt text and command output are not
committed.

Two runtime defects surfaced before the decisive attempt:

1. Codex Desktop identifies the same 0.147.0 App Server with a bounded desktop user-agent shape,
   rather than the CLI-only shape that setup previously accepted. Commit `267a36b` accepts that
   host identity without accepting arbitrary suffixes.
2. `spotterd` used `thread/read` while reconciling and therefore learned thread identity without
   subscribing to item/turn notifications. Commit `5a6160b` uses `thread/resume`, retries the
   expected pre-rollout race on later lifecycle notifications, and keeps `thread/read` only as the
   pre-rollout state fallback.

The first failed attempt remains in the result. After the subscription fix, the observer captured
the authoritative command lifecycle, proving that the remaining user-input gap is at the source
surface rather than the adapter subscription path.

## Cohort disposition

| Scenario | Attempt | Result | Disposition |
| --- | ---: | --- | --- |
| `ASO-NORMAL-1` | 1 | Hook captured initial input and command; App Server captured identity only before the subscription fix | failed minimum |
| `ASO-NORMAL-2` | 1 | Hook captured later input and command; App Server captured identity only before the subscription fix | failed minimum |
| `ASO-NORMAL-2` | 2 | App Server captured turn and successful command outcome, but no user-input source notification | failed minimum; stop rule 2 |
| remaining six rows | — | Not performed after the mandatory stop | missing minimum |

The retained cohort therefore contains one distinct App Server thread and three Hook turns, not the
planned seven threads and nine turns. This is an early-stop result, not a completed representative
cohort.

## Source → Trace IR → ThreadState

For the decisive post-fix turn, the App Server journal contains 45 events and three explicit
observation gaps. Its source audit classified 15 samples exact and 22 unknown within the selected
thread; no deduplicated source notifications were excluded.

| Required fact | Source | Trace IR | ThreadState | Timing classification |
| --- | --- | --- | --- | --- |
| later user input/content/provenance | not exposed | absent | absent | `STRUCTURALLY_INVISIBLE` |
| turn identity | exact | exact | exact/partial across lifecycle | visible in time |
| command start | exact | exact | exact | visible in time |
| command completion and exit status | exact | exact | exact | `VISIBLE_IN_TIME` before turn completion |
| runtime usage | exact | exact | adapter-dropped from state | not consequential to this Hook decision |

The source methods observed during the bounded run were thread start/status/token usage,
turn start/completion, item start/completion and agent-message deltas, hook start/completion,
goal clearing, rate limits, app-list updates, and remote-control status. There was no user-message
or input notification. Unknown methods remain in the source audit rather than being discarded.

Hook overlap captured the exact later prompt and successful result, while the App Server captured
one semantic command action and outcome. The metric still reported `cross_surface_overlap=0`, so
the two surfaces were not safely correlated into one global semantic action. This unresolved
identity/correlation gap is additional evidence against Hook removal.

## Labels and timing

- The App Server session is labeled `invisible` for the user-input responsibility.
- The overlapping Hook session is labeled `visible`.
- Opportunity `ASO-NORMAL-2-user-prompt-attempt-2` anchors the inferred semantic window from
  App Server turn start to command start and records the exact-input evidence as absent. The
  opportunity is consequently unjudgeable for evidence-linked detection rather than treated as a
  miss or success.

## Per-Hook decision

| Hook | Decision | Reason |
| --- | --- | --- |
| `SessionStart` | **NO-GO** | Lifecycle minimums were not completed before the mandatory stop, and identity correlation remained incomplete. |
| `UserPromptSubmit` | **NO-GO** | Two attempts did not expose the later user input or provenance through App Server notifications. |
| `PostToolUse` | **NO-GO** | Successful command outcome was exact and timely, but failure, patch, MCP, approval, interrupt, and reconnect minimums were not run after the mandatory stop. |

The command result is encouraging for future `PostToolUse` narrowing, but the predeclared rule does
not permit promoting a partial surface. All three observation Hooks remain required until a later
protocol/runtime surface closes the gaps and a new cohort is predeclared.

## Reproduction and retained evidence

The run used the repository's `spotter setup --portable --endpoint`, `spotter daemon restart`,
remote Codex TUI resume, `spotter observability`, `spotter metrics`, `spotter label`, and
`spotter label-opportunity` paths. The bounded machine-readable summary is
[app-server-observability-v1-result.json](app-server-observability-v1-result.json).

Raw isolated artifacts are retained outside Git with these SHA-256 digests:

| Artifact | SHA-256 |
| --- | --- |
| Hook journal | `7a28bc644e8bea2b3cca5742f86fdeb4532bb1332d39e670069fd51e4fe4bd65` |
| App Server thread journal | `54f722c00ebfd2a7371c73e7182acbad8f1d482686eee970a39309fb7b7c5be4` |
| App Server unscoped journal | `5e64af5e1becf24cef5fe9b26f17097b293d10909fddd17023462044b9e334d8` |
| Source-audit samples | `8b278bca4c989f0c1d309269f9d0862cd1c65f84ff1327a15fbb99707939724d` |

