# Live App Server observation — first non-zero sample

**Measured:** 2026-08-26

**Issue:** [#304](https://github.com/spotter-agent/spotter/issues/304)

**Decision:** the configured launch path now reaches `ready`, and this store holds App Server
sessions for the first time. Hook removal (#86) remains **NO-GO** and is not addressed here.

## What was run

An external `codex app-server --listen ws://127.0.0.1:4500` on Codex 0.149.1, macOS 26.5.1 / arm64,
then `spotter setup codex --endpoint ws://127.0.0.1:4500`.

Two concurrent threads were then driven over that shared server with `thread/start` + `turn/start` —
the same protocol calls a TUI makes. `codex exec` was not usable: it has no `--remote`, so it selects
an embedded server Spotter cannot observe.

**This is not the two-real-TUI validation #304 asks for.** It exercises the App Server client path
and thread isolation, not `spotter codex`'s exec into an interactive TUI, and not a TUI's own
attachment behaviour. Those remain unvalidated.

## Setup outcome

| | |
| --- | --- |
| full connect-and-reconcile setup | 10.7s |
| idempotent re-run | 1.6s |
| shipped readiness budget | 30s |

```text
[ok] App Server runtime: ready epoch=30; codex=0.149.1;
     observation=available, thread_query=available,
     steer=unknown, interrupt=unknown, atomic_pre_tool_veto=unavailable
[ok] observation: ws://127.0.0.1:4500; daemon ready; observation available
```

`steer` and `interrupt` are reported `unknown`, not available: this run establishes observation, and
says nothing about live control.

## Sessions

```text
before   sessions: 215 (hook=215, app_server=0)
after    sessions: 269 (hook=215, app_server=54)
```

## Thread isolation

The two concurrent threads each received their own durable journal, and neither journal mentions the
other thread's id:

| Thread | Journal | Records | Foreign thread ids |
| --- | --- | ---: | ---: |
| `01a03ca7-…f95f4b5f6b35` | `app-server-013eb300…` | 23 | 0 |
| `01a03ca7-…f9743d7b432c` | `app-server-039a086c…` | 23 | 0 |

Both threads started, ran, and completed concurrently. Isolation holds at the journal boundary for
this case.

## Coverage of the live event stream

Classified across all App Server journals:

| Kind | Count |
| --- | ---: |
| `observation_gap` | 790 |
| `runtime_reconciled` | 790 |
| `token_usage` | 484 |
| `thread_status` | 99 |
| `user_prompt` | 4 |
| `agent_message` | 4 |
| `turn_completed` | 2 |
| `thread_started` | 2 |

Unclassified (`runtime_event_unknown`), by source method:

| Method | Count |
| --- | ---: |
| `mcpServer/startupStatus/updated` | 2376 |
| `thread/goal/cleared` | 848 |
| `remoteControl/status/changed` | 31 |
| `item/agentMessage/delta` | 2 |
| `account/rateLimits/updated` | 2 |

Two readings this does **not** support:

- *"61% of observed events are unmapped."* True by count, but 94% of the unclassified volume is one
  MCP startup notification. The unmapped surface is a handful of chatty lifecycle methods, not a
  broad protocol gap.
- *"There are 790 real observation gaps."* Reconciliation records a gap per thread whose epoch
  changed, and it walks every thread the server knows, so this number is dominated by reconnect
  bookkeeping over ~215 historical threads rather than by lost live coverage.

Both counts need a denominator before they mean anything, and this run does not provide one.

## What changed to make this possible

Seven defects, all found by running the documented path rather than by reading it. In order:

1. the host-version parser rejected the user-agent the server echoes back, which is Spotter's own
   originator;
2. the readiness wait treated a just-restarted daemon's `UNAVAILABLE` as terminal;
3. `restart()` unregistered the launchd service instead of restarting the process;
4. the service definition set no `ThrottleInterval`, so launchd throttled every managed restart to
   10s — longer than any readiness budget here;
5. `bootout` returns before launchd finishes unloading, and the `bootstrap` after it was denied
   without being surfaced;
6. the App Server transport inherited a 1 MiB message cap; a real server sent 2,607,959 bytes;
7. reconciliation raised on the first thread it could not `thread/resume`, abandoning the connection
   — measured as 11 epochs in 3 minutes, never reaching ready.

(7) was the blocker. `thread/resume` claims a writer, and each epoch collided with the claim its own
previous epoch had left behind.

## Open, and deliberately not decided here

Reconciliation `thread/resume`s **every** thread the server knows. The protocol documents resume as
rejoining a *running* thread, so sharing a live thread with a TUI is supported; claiming every
historical thread on disk is what produced the collision. Whether reconciliation should attach only
to running or supervised threads is a design change for #304, and it can now be argued from the
numbers above rather than from speculation.

## Required follow-ups

1. run the actual two-TUI validation through `spotter codex`;
2. establish denominators before either the gap count or the unknown-event count is quoted;
3. decide the reconciliation attachment rule;
4. `spotterd.log` stayed empty across every failure in this run, including the reconnect loop. A
   daemon that produces no diagnostic for a repeating failure is its own gap.
