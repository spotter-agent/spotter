# Codex App Server attach PoC

Run the server and the independent Spotter client in separate terminals:

```bash
codex app-server --listen unix://spotter-poc.sock
uv run python -m spotter.app_server_poc --socket spotter-poc.sock
```

The client performs the WebSocket upgrade required by the Unix transport, initializes a second
JSON-RPC connection, and calls `thread/list`. It never sends input unless `--steer` is explicitly
provided together with `--thread-id` and `--turn-id`.

```bash
uv run python -m spotter.app_server_poc \
  --socket spotter-poc.sock \
  --thread-id THREAD --turn-id TURN --steer "[Spotter] Verify the failing assumption."
```

## Result on Codex 0.147.0

- A second client can initialize and list threads over the external Unix-socket App Server.
- `turn/steer` is present in the generated protocol and requires both thread and active-turn IDs.
- `codex app-server daemon start` rejected the Homebrew/Cask install because managed daemon mode
  requires Codex's standalone installer.
- A TUI started before the external server remained on its embedded server: the same thread was
  visible from durable history but reported `notLoaded`, so this is not a shared live control plane.

The Tier-0 premise is therefore only partially proven. Do not build `spotterd` or automatic
intervention on this yet. The remaining E2E check is to start plain `codex` after a supported managed
daemon is available and confirm that this client sees its thread as loaded before testing steering.
