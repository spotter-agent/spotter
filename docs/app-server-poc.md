# App Server lifecycle / attach PoC

This is the reproducible experiment harness for issue
[#78](https://github.com/spotter-agent/spotter/issues/78). It is deliberately not a production
App Server client.

## Requirements

- Codex CLI 0.147.0 or newer with `codex app-server --listen` and `codex --remote`
- Node.js 22 or newer; the harness uses Node's built-in WebSocket client

Run the self-check:

```bash
node scripts/app_server_poc.mjs --self-test
```

## Spotter-managed external server

Start an App Server and copy the printed WebSocket URL:

```bash
codex app-server --listen ws://127.0.0.1:0
```

Verify a second client can initialize and list threads:

```bash
node scripts/app_server_poc.mjs probe --endpoint ws://127.0.0.1:PORT
```

Start the observer before starting the TUI:

```bash
node scripts/app_server_poc.mjs observe \
  --endpoint ws://127.0.0.1:PORT \
  --cwd /absolute/test/directory \
  --steer "Reply with exactly: SPOTTER_STEER_OK" \
  --expect SPOTTER_STEER_OK \
  --out /tmp/spotter-app-server-poc.json
```

In another terminal, connect the ordinary TUI to the same endpoint:

```bash
codex --remote ws://127.0.0.1:PORT -C /absolute/test/directory
```

The report succeeds only after the observer joins the TUI-created thread, observes its active
turn, receives a successful `turn/steer` response, sees the expected text in an `agentMessage`,
and sees that turn complete.

## Codex-managed daemon

Test the managed path separately:

```bash
codex app-server daemon start
codex app-server daemon version
```

As of Codex CLI 0.147.0, a Homebrew Cask install cannot start this daemon: the command requires
the standalone installer's fixed `~/.codex/packages/standalone/current/codex` path. This path
must be retested on a standalone install before selecting it as Spotter's default lifecycle.

## Observed protocol constraint

A second initialized client receives global `thread/started` and `thread/status/changed`
notifications, but does not receive the new thread's turn/item stream until it calls
`thread/resume`. Immediately resuming from `thread/started` can race rollout creation and return
`no rollout found`; the harness retries this bounded race.

## First local result

On 2026-08-13, Codex CLI 0.147.0 passed the Spotter-managed path on macOS:

- an ordinary `codex --remote ...` TUI created thread
  `019ff8a9-e7f0-70b3-8e42-872dc6bcb0ed`;
- the observer joined it and tracked active turn
  `019ff8a9-e97a-7ed2-ba3b-870cbd9772cc`;
- `turn/steer` returned that same turn ID;
- the observer received tool lifecycle events and the final `agentMessage` was
  `SPOTTER_STEER_OK` instead of the original requested response;
- the TUI visibly rendered `SPOTTER_STEER_OK`.

This proves same-thread observation and real same-turn steering for the Spotter-managed path.
It does not yet select the production lifecycle: the Codex-managed standalone installation,
multiple concurrent TUIs, and reconnect behavior still need recorded real-session runs. Reconnect
and reconciliation are now implemented in `spotterd` and covered by repository tests; that later
implementation evidence does not retroactively extend this PoC's recorded scope.
