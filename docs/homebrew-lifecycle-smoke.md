# Homebrew lifecycle smoke

> **Status:** implemented in the official
> [`spotter-agent/homebrew-spotter`](https://github.com/spotter-agent/homebrew-spotter) tap and
> exercised on macOS CI. Fast ownership, path, protocol, and failure fixtures remain in this
> repository.

Issue [#108](https://github.com/spotter-agent/spotter/issues/108) varies package and Codex
integration state independently. The real Homebrew gate builds two immutable local release
artifacts from one Spotter source revision, publishes them through an isolated fixture tap, and
executes:

```text
package absent / integration absent
  ↓
install G1 (Codex files unchanged)
  ↓
setup Codex + start G1 daemon + invoke packaged Hook bridge
  ↓
upgrade Formula to G2 while G1 remains live
  ↓
G2 CLI identifies the running process as G1
cached G1 Hook fails open against the G2 package
  ↓
setup reconciles exactly once: G1 stops, G2 starts, generation rotates
  ↓
uninstall without teardown
  ↓
executables disappear, G2 exits, launchd does not retry, Hook fails open
user data and dangling integration ownership remain
  ↓
reinstall G2 + doctor/setup repair without duplicate Hooks
  ↓
teardown + uninstall
  ↓
unrelated Codex state and durable Spotter data remain
```

The fixture removes the old keg during upgrade; no assertion relies on a versioned Cellar path
remaining present. It checks the CLI, daemon, running PID/build, Integration Manifest generation,
Hook command, service definition, and representative user config/journal/label/experiment/registry
files at the relevant transitions. A long-lived fake Codex App Server sentinel must retain the same
PID through setup, upgrade/reconcile, teardown-less uninstall, reinstall, teardown, and final
uninstall.

## Reproduce locally

Clone Spotter and its tap as siblings on a macOS host with Homebrew, then run:

```bash
cd homebrew-spotter
python3 -m venv /tmp/spotter-lifecycle-venv
/tmp/spotter-lifecycle-venv/bin/python -m pip install 'build>=1.2,<2'
/tmp/spotter-lifecycle-venv/bin/python scripts/lifecycle_smoke.py \
  --spotter-source ../spotter \
  --formula-template Formula/spotter.rb
```

The harness refuses to replace an existing Homebrew `spotter` executable or loaded
`dev.spotter.runtime` service. Its temporary Formula, service, trust entry, and tap are cleaned on
success and ordinary fixture failure.

## Fast fixture coverage

The expensive macOS transition is complemented by deterministic tests in this repository:

| Lifecycle risk | Evidence |
| --- | --- |
| Apple Silicon, Intel, and Linuxbrew stable paths; no Cellar persistence | `tests/test_paths.py`, `tests/test_integration.py` |
| daemon build differs after a stable link moves | `tests/test_runtime_diagnostics.py` |
| old build is gracefully replaced exactly once | `tests/test_integration.py` managed-service restart fixtures |
| transient upgrade relink vs sustained uninstall absence | package-boundary monitor tests in `tests/test_daemon.py` |
| loaded-but-unavailable launchd generation after reinstall | launchd bootout/bootstrap recovery fixture in `tests/test_integration.py` |
| missing bridge, stale integration generation, protocol mismatch | `tests/test_integration.py`, `tests/test_daemon.py`, `tests/test_hook.py` |
| missing/duplicate/user-modified Hook ownership | `tests/test_integration.py`, `tests/test_runtime_diagnostics.py` |
| unsupported newer manifest and schema 1–3 repair | `tests/test_integration.py` |
| mutation before manifest commit and service teardown failure | transactional rollback fixtures in `tests/test_integration.py` |
| foreign process or stale socket endpoint | managed-service ownership and daemon socket fixtures |
| lifecycle mutation serialization | a competing process holds the Integration Manifest `flock` while setup proves it waits |

The cached-host check executes the generated G1 command directly rather than depending on
undocumented Codex internals. Mixed generations must be detectable and non-blocking; after a host
reload only the reconciled G2 Hook remains.

The fixture starts and later cleans up its own shared App Server sentinel, but Spotter is never
configured as that process's owner. Supported setup can attach a user-supplied endpoint, but this
packaging smoke deliberately leaves its sentinel outside the integration transaction; claiming
process ownership here would violate the runtime boundary. Full configuration-generation,
protocol-range, schema-migration, retention, and repository-aware purge behavior remains owned by
#90, #47, and #89 rather than being inferred from a green packaging gate.
