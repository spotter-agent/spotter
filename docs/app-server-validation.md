# Codex App Server connection validation

> **Validated:** 2026-08-13  
> **Upstream snapshot:** [`openai/codex@b1373b7`](https://github.com/openai/codex/commit/b1373b74a27d1d9b65074a873202683355cae772)  
> **Scope:** whether starting an external App Server makes an otherwise plain `codex` TUI attach to it automatically.
>
> **Follow-up:** The same-day runtime experiment in [App Server lifecycle / attach PoC](app-server-poc.md#first-local-result)
> subsequently proved same-thread observation and same-turn steering for an explicitly connected,
> Spotter-managed external server. This document remains the evidence for the narrower automatic-discovery
> question; its original environment limitation and experiment plan below are historical context.

## Conclusion

**No. The current interface requires the TUI endpoint to be selected explicitly.** Starting
`codex app-server` separately and then running plain `codex` does not, by itself, connect
that TUI to the external process.

The documented external-server flow is:

```bash
# terminal A
codex app-server --listen ws://127.0.0.1:4500

# terminal B
codex --remote ws://127.0.0.1:4500
```

The same explicit rule applies to the default Unix socket:

```bash
codex app-server --listen unix://
codex --remote unix://
```

Therefore Spotter must not model “an external App Server is running” as sufficient
startup configuration. A launcher, alias, wrapper, or another upstream-supported way of
supplying `--remote` is required if the product wants a TUI and Spotter to share one
server. That changes the earlier `spotter setup codex` → plain `codex` expectation.

## Evidence

### Official Codex documentation

The [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server.md#connect-the-cli-terminal-ui)
specifies two separate commands: start a listener with `codex app-server --listen ...`,
then connect the terminal UI with `codex --remote ...`. It documents `stdio://` as the
server default, while a reusable listener must be selected explicitly with a WebSocket
or Unix-socket `--listen` endpoint.

The same page labels the `app-server` command and WebSocket transport experimental and
unsupported for production workloads. Local WebSocket or Unix-socket experiments can
validate the design, but they are not yet a stable production contract.

### Upstream CLI source

The upstream CLI defines `--remote` as an optional interactive-TUI argument. In
[`resolve_remote_endpoint`](https://github.com/openai/codex/blob/b1373b74a27d1d9b65074a873202683355cae772/codex-rs/cli/src/main.rs#L2452-L2485),
an absent option resolves to `None`; there is no lookup of a running listener or default
socket in this path. The TUI receives that optional endpoint in
[`run_interactive_tui`](https://github.com/openai/codex/blob/b1373b74a27d1d9b65074a873202683355cae772/codex-rs/cli/src/main.rs#L2374-L2416).
This supports the documentation-level conclusion: plain `codex` does not discover and
reuse an arbitrary external App Server.

### Environment limitation

The validation environment did not contain a `codex` executable, so this pass could not
perform an authenticated, interactive end-to-end turn. The conclusion above is a
documentation-and-source validation, not a claim that same-turn steering has already
passed. A runtime PoC is still required for subscription, active-turn identity,
multi-client steering, reconnect, and concurrency behavior.

## What is and is not established

| Claim | Result | Basis |
| --- | --- | --- |
| An already-running external server is enough for plain `codex` | **Rejected** | Explicit `--remote` is documented and implemented |
| A TUI can target an external server | **Supported interface** | `codex --remote ws://...` or `unix://...` |
| Spotter can open a second connection | **Supported at protocol level** | Each connection initializes independently |
| A second connection automatically observes a TUI-created thread | **Rejected** | The PoC had to discover and explicitly resume the thread |
| `turn/steer` can target an active turn | **Supported interface** | Requires the correct `threadId` and `expectedTurnId` |
| Steering changes the real user-visible TUI turn | **Established for the tested explicit path** | See the [PoC result](app-server-poc.md#first-local-result) |
| Plain `codex` UX can be preserved unchanged | **Rejected for the current interface** | The external endpoint must be selected explicitly |

The protocol is multi-connection, but “two clients connected to one server” is not the
same as “both clients observe the same thread.” `thread/start` subscribes its connection;
Spotter still needs a reliable discovery and `thread/resume`/subscription strategy. For
steering, it must also track the exact active turn because `turn/steer` requires
`expectedTurnId` and fails when no turn is active.

## Revised Spotter decision

1. **Drop automatic discovery as an architectural assumption.** Do not continue with a
   Codex-managed-daemon design that depends on plain `codex` finding it.
2. **Keep the external App Server PoC, but launch explicitly.** Test a Spotter-managed
   listener with `codex --remote <endpoint>`, preferably over a local Unix socket.
3. **Treat transparent plain-command UX as a separate product problem.** Before managed lifecycle
   integration,
   choose and validate a supported launcher/alias/wrapper approach, including argument
   forwarding, `resume`, upgrades, failures, and teardown.
4. **Retain degraded mode.** A user who runs unmodified plain `codex` is not attached to
   Spotter's external control plane; hooks may still provide deterministic enforcement,
   but live observation, steer, and interrupt must report unavailable.

## Original follow-up experiment (completed for the explicit single-TUI path)

Use an environment with a current Codex CLI and authentication:

1. Start `codex app-server --listen unix://PATH` and wait for readiness.
2. Start the TUI with `codex --remote unix://PATH`.
3. Connect Spotter as client B and complete `initialize` / `initialized`.
4. Discover the TUI thread, subscribe/resume it, and compare thread and turn IDs.
5. During an active turn, send `turn/steer` with the observed `expectedTurnId` and verify
   the change in both the event stream and the TUI.
6. Repeat for two simultaneous TUIs; then disconnect/reconnect client B.
7. Run plain `codex` as the negative control and verify Spotter reports degraded rather
   than silently claiming attachment.

The recorded PoC passed steps 1–5 for one TUI. Multi-TUI concurrency, reconnect behavior, and the
Codex-managed daemon path remain unproven in a recorded real session. Reconnect/reconciliation is
implemented in `spotterd` and exercised by repository tests, but that is separate implementation
evidence; it does not expand what this historical validation run established.
