"""Is supervision actually working? (issue #41)

Every failure path in the hook fails open, which is correct — a supervision
bug must never break the supervised session. The cost of that choice is that
total failure looks exactly like a quiet session: no journal, no error, no
difference. Silence is Spotter's designed normal state, so silence cannot also
be its failure state.

This module answers the one question the rest of the tool cannot: if nothing
was recorded, was there nothing to record, or is nothing running?
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from spotter.app_server_endpoint import display_app_server_endpoint, redact_app_server_error
from spotter.budget import LedgerCorrupt, spend_totals
from spotter.build_identity import current_build_identity
from spotter.codex_host import CodexHostVersionError, validate_codex_host_version
from spotter.daemon import (
    DaemonClient,
    DaemonStatus,
    RuntimeCompatibility,
    RuntimeHealth,
)
from spotter.paths import RuntimeLayout, spotter_home
from spotter.runtime_fingerprint import expected_runtime_construction_fingerprint

if TYPE_CHECKING:
    from spotter.integration import IntegrationManifest

OK = "ok"
INFO = "info"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class IntegrationInspection:
    manifest: "IntegrationManifest | None"
    check: Check
    hook_ready: bool


def _codex_hooks_files() -> list[Path]:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return [home / "hooks.json", home / "config.toml"]


def _claude_settings_files() -> list[Path]:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return [home / "settings.json", home / "settings.local.json"]


def check_registration() -> list[Check]:
    """Is a hook actually wired into a runtime?

    Registration is checked by reading each runtime's config for a command
    mentioning spotter. This is deliberately shallow — the alternative is
    modelling every runtime's config schema and staying correct as they change
    — but shallow beats the current situation, which is no check at all.
    """
    checks: list[Check] = []
    any_wired = False
    for label, files in (("codex", _codex_hooks_files()), ("claude", _claude_settings_files())):
        present = [p for p in files if p.exists()]
        if not present:
            checks.append(Check(f"{label} config", INFO, "no config found; runtime not configured"))
            continue
        wired = [p for p in present if "spotter" in p.read_text(errors="replace")]
        if wired:
            any_wired = True
            checks.append(Check(f"{label} hook", OK, f"registered in {wired[0].name}"))
        else:
            checks.append(
                Check(
                    f"{label} hook",
                    INFO,
                    f"not registered in {', '.join(p.name for p in present)}",
                )
            )
    if not any_wired:
        checks.append(
            Check("runtime registration", WARN, "Spotter is not registered in any runtime")
        )
    return checks


def check_interpreter() -> Check:
    """Report the interpreter; do not pretend to gate on it.

    A version gate here would be dead code: this module uses syntax that older
    interpreters cannot import, so reaching this line already proves the
    version. What actually matters is the interpreter the *hook* resolves,
    which may differ from this one — and that is what the round-trip check
    exercises end to end.
    """
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return Check("interpreter", OK, f"python {version} (hook's own is proven by round-trip)")


def check_storage() -> list[Check]:
    home = spotter_home()
    checks: list[Check] = []
    if not home.exists():
        return [Check("storage", WARN, f"{home} does not exist yet")]
    mode = home.stat().st_mode & 0o777
    checks.append(
        Check("permissions", OK if mode & 0o077 == 0 else WARN, f"{home} mode {oct(mode)}")
    )
    probe = home / ".doctor-probe"
    try:
        probe.write_text("probe")
        probe.unlink()
        checks.append(Check("writable", OK, str(home)))
    except OSError as error:
        checks.append(Check("writable", FAIL, f"{home}: {error}"))
    free = shutil.disk_usage(home).free
    checks.append(Check("disk", OK if free > 100e6 else FAIL, f"{free / 1e9:.1f} GB free"))
    return checks


def check_roundtrip(
    config_path: Path | None,
    *,
    command: Sequence[str] | None = None,
    capture_only: bool = False,
) -> Check:
    """Feed the real CLI a synthetic payload and confirm a record appears.

    Nothing else in the tool proves the whole path works; every component
    could be healthy while the wiring between them is not.
    """
    session = f"doctor-probe-{int(time.time())}"
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "cwd": str(Path.cwd()),
        "tool_name": "Bash",
        "tool_use_id": "doctor",
        "tool_input": {"command": "true"},
    }
    args = list(command or RuntimeLayout.discover().bridge_command)
    if config_path is not None:
        args += ["--config", str(config_path)]
    environment = {**os.environ}
    if capture_only:
        environment.pop("SPOTTER_DISABLE", None)
        environment["SPOTTER_CAPTURE_ONLY"] = "1"
    else:
        environment.pop("SPOTTER_CAPTURE_ONLY", None)
        environment["SPOTTER_DISABLE"] = "1"
    try:
        result = subprocess.run(
            args,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Check("round-trip", FAIL, f"hook could not run: {error}")
    journal = spotter_home() / "sessions" / f"{session}.jsonl"
    if not journal.exists():
        stderr = result.stderr.strip()[:200]
        return Check("round-trip", FAIL, f"no journal written{': ' + stderr if stderr else ''}")
    journal.unlink(missing_ok=True)
    for suffix in (".state", ".lock"):
        journal.with_suffix(journal.suffix + suffix).unlink(missing_ok=True)
    return Check("round-trip", OK, "synthetic payload recorded and cleaned up")


def check_ledger() -> Check:
    """A corrupt ledger silently refuses every review.

    Without this, doctor reports healthy supervision while the reviewer is
    declining to run — the same class of invisible failure this command
    exists to end.
    """
    try:
        totals = spend_totals()
    except LedgerCorrupt as error:
        return Check("spend ledger", FAIL, f"{error}; every review is being refused")
    if totals is None:
        return Check("spend ledger", OK, "no spend recorded yet")
    return Check("spend ledger", OK, f"{totals['day']} reviews today, {totals['tokens']} tokens")


def check_freshness(max_idle_hours: float = 24.0) -> Check:
    sessions = spotter_home() / "sessions"
    journals = sorted(sessions.glob("*.jsonl")) if sessions.exists() else []
    real = [p for p in journals if not p.stem.startswith("doctor-probe")]
    if not real:
        return Check("observations", INFO, "no session has been recorded yet")
    age = (time.time() - max(p.stat().st_mtime for p in real)) / 3600
    status = OK if age <= max_idle_hours else WARN
    return Check("observations", status, f"last recorded {age:.1f}h ago")


def _hook_entries(path: Path) -> list[tuple[str, object, dict[str, Any]]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        raise ValueError(f"{path} is unreadable: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("hooks", {}), dict):
        raise ValueError(f"{path} has an unsupported shape")
    entries: list[tuple[str, object, dict[str, Any]]] = []
    for event, groups in cast(dict[str, object], raw.get("hooks", {})).items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise ValueError(f"{path} has an unsupported event shape")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"{path} has an unsupported hook group")
            for hook in cast(list[object], group["hooks"]):
                if isinstance(hook, dict):
                    entries.append((event, group.get("matcher"), cast(dict[str, Any], hook)))
    return entries


def _legacy_plugins(codex_home: Path) -> tuple[str, ...]:
    config = codex_home / "config.toml"
    if not config.exists():
        return ()
    try:
        raw = tomllib.loads(config.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{config} is unreadable: {error}") from error
    plugins = raw.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError(f"{config} has an unsupported plugin shape")
    return tuple(name for name in plugins if name == "spotter" or name.startswith("spotter@"))


def check_integration() -> IntegrationInspection:
    """Validate the recorded ownership contract without mutating Codex config."""
    # Local import avoids integration -> doctor -> integration during setup imports.
    from spotter.integration import IntegrationError, IntegrationManifest, is_spotter_hook

    manifest_path = RuntimeLayout.discover().integration_manifest
    try:
        manifest = IntegrationManifest.load(manifest_path)
    except IntegrationError as error:
        return IntegrationInspection(None, Check("Codex integration", FAIL, str(error)), False)

    try:
        codex_home = Path(
            manifest.codex_home
            if manifest is not None
            else os.environ.get("CODEX_HOME", Path.home() / ".codex")
        )
        hooks_path = (
            Path(manifest.hooks_file) if manifest is not None else codex_home / "hooks.json"
        )
        spotter_hooks = [entry for entry in _hook_entries(hooks_path) if is_spotter_hook(entry[2])]
        legacy_plugins = _legacy_plugins(codex_home)
    except (TypeError, ValueError) as error:
        return IntegrationInspection(manifest, Check("Codex integration", FAIL, str(error)), False)

    if manifest is None:
        leftovers = len(spotter_hooks) + len(legacy_plugins)
        return IntegrationInspection(
            None,
            Check(
                "Codex integration",
                INFO if leftovers else WARN,
                (
                    f"managed setup absent; found {leftovers} legacy Spotter registration(s)"
                    if leftovers
                    else "Spotter is not configured for Codex"
                ),
            ),
            bool(spotter_hooks),
        )

    if manifest.state != "ready":
        return IntegrationInspection(
            manifest,
            Check("Codex integration", FAIL, f"manifest state is {manifest.state!r}"),
            False,
        )
    try:
        validate_codex_host_version(manifest.agent_version)
    except CodexHostVersionError as error:
        return IntegrationInspection(
            manifest,
            Check(
                "Codex integration",
                FAIL,
                f"recorded Codex host is unsupported: {error}; upgrade Codex and run "
                "`spotter setup codex`",
            ),
            False,
        )
    owned = manifest.owned_hooks
    if not isinstance(owned, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("hook"), dict) for entry in owned
    ):
        return IntegrationInspection(
            manifest,
            Check("Codex integration", FAIL, "manifest owned Hooks are invalid"),
            False,
        )
    expected = [(entry.get("event"), entry.get("matcher"), entry.get("hook")) for entry in owned]
    if sorted(json.dumps(entry, sort_keys=True) for entry in spotter_hooks) != sorted(
        json.dumps(entry, sort_keys=True) for entry in expected
    ):
        return IntegrationInspection(
            manifest,
            Check(
                "Codex integration",
                FAIL,
                "owned Hooks are missing, user-modified, or duplicated "
                f"({len(spotter_hooks)} found); run `spotter setup codex` to reconcile",
            ),
            False,
        )
    if legacy_plugins:
        return IntegrationInspection(
            manifest,
            Check(
                "Codex integration",
                FAIL,
                f"stale legacy plugin registration: {', '.join(legacy_plugins)}",
            ),
            True,
        )
    identity = current_build_identity()
    if manifest.setup_build_id not in {"legacy", identity.build_id}:
        return IntegrationInspection(
            manifest,
            Check(
                "Codex integration",
                FAIL,
                f"generated by build {manifest.setup_build_id}, current package is "
                f"{identity.build_id}; run `spotter setup codex` to reconcile",
            ),
            False,
        )
    recorded_layout = manifest.runtime_layout
    if not isinstance(recorded_layout, dict):
        return IntegrationInspection(
            manifest,
            Check("Codex integration", FAIL, "manifest runtime layout is invalid"),
            False,
        )
    recorded_cli = recorded_layout.get("cli_executable")
    recorded_daemon = recorded_layout.get("daemon_executable")
    for name, raw_path in (("spotter", recorded_cli), ("spotterd", recorded_daemon)):
        if raw_path is None:
            continue  # schema 1-3 compatibility; setup upgrades the record
        if not isinstance(raw_path, str) or not raw_path:
            return IntegrationInspection(
                manifest,
                Check("Codex integration", FAIL, f"manifest {name} path is invalid"),
                False,
            )
        if not os.access(raw_path, os.X_OK):
            return IntegrationInspection(
                manifest,
                Check(
                    "Codex integration",
                    FAIL,
                    f"package executable is missing: {raw_path}; owned Hooks fail open; "
                    "run `spotter setup codex` after reinstall",
                ),
                False,
            )
    try:
        registration_missing = (
            manifest.service_registration is None
            or not Path(manifest.service_registration).exists()
        )
    except TypeError:
        registration_missing = True
    if manifest.runtime_mode == "managed" and manifest.service_owned and registration_missing:
        return IntegrationInspection(
            manifest,
            Check("Codex integration", FAIL, "managed service registration is missing"),
            True,
        )
    return IntegrationInspection(
        manifest,
        Check("Codex integration", OK, f"ready ({manifest.runtime_mode})"),
        True,
    )


def _daemon_check(status: DaemonStatus, configured: bool) -> Check:
    if status.compatibility == RuntimeCompatibility.INCOMPATIBLE_STALE:
        return Check(
            "daemon",
            FAIL,
            f"incompatible running daemon: {status.detail or 'IPC negotiation failed'}; "
            "run `spotter daemon restart` after upgrading",
        )
    if status.health == RuntimeHealth.HEALTHY:
        detail = (
            f"healthy pid={status.pid} protocol={status.protocol} "
            f"runtime={status.runtime_generation or 'unknown'}"
        )
        installed = current_build_identity().build_id
        if status.build_id is None:
            return Check(
                "daemon",
                WARN,
                "running daemon does not report a build identity; "
                f"installed package is {installed}; restart required",
            )
        if (
            status.compatibility == RuntimeCompatibility.COMPATIBLE_STALE
            or status.build_id != installed
        ):
            return Check(
                "daemon",
                WARN,
                f"running build {status.build_id}, installed package is {installed}; "
                "restart required",
            )
        if status.compatibility == RuntimeCompatibility.UNKNOWN:
            return Check(
                "daemon",
                WARN,
                "running daemon compatibility is unknown; restart required before mutable "
                "control operations",
            )
        return Check("daemon", OK, detail)
    if status.health == RuntimeHealth.UNAVAILABLE:
        consequence = (
            "configured enforcement is using local Hook fallback" if configured else "not running"
        )
        return Check("daemon", FAIL if configured else INFO, f"unavailable; {consequence}")
    return Check("daemon", WARN, f"{status.health.value}: {status.detail or 'reduced health'}")


async def _probe_app_server(endpoint: str) -> tuple[Check, Check]:
    client = None
    try:
        # Keep the optional WebSocket dependency off the bundled Hook import path.
        from spotter.app_server import CapabilityStatus, CodexAppServerClient

        client = CodexAppServerClient(endpoint, request_timeout=2)
        await client.connect()
        capabilities = client.capabilities
        observation = Check(
            "observation",
            OK if capabilities.observation == CapabilityStatus.AVAILABLE else WARN,
            f"App Server {client.state.value}; observation {capabilities.observation.value}",
        )
        controls = (capabilities.steer, capabilities.interrupt)
        control = Check(
            "live control",
            OK if all(item == CapabilityStatus.AVAILABLE for item in controls) else WARN,
            "steer/interrupt " + "/".join(item.value for item in controls),
        )
        return observation, control
    except Exception as error:
        detail = redact_app_server_error(error, endpoint)
        return (
            Check("observation", WARN, f"App Server disconnected: {detail}"),
            Check("live control", WARN, "unavailable while App Server is disconnected"),
        )
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()


def check_runtime(*, deep: bool = False) -> list[Check]:
    """Summarize process, integration, capability, and enforcement consequences."""
    integration = check_integration()
    daemon = asyncio.run(DaemonClient().status())
    configured = integration.manifest is not None
    checks = [integration.check, _daemon_check(daemon, configured)]
    expected_construction = expected_runtime_construction_fingerprint(RuntimeLayout.discover())
    if daemon.available and daemon.construction_fingerprint is None:
        checks.append(
            Check(
                "runtime construction",
                WARN,
                "running daemon does not report a construction fingerprint; restart required",
            )
        )
    elif daemon.available and daemon.construction_fingerprint != expected_construction:
        checks.append(
            Check(
                "runtime construction",
                WARN,
                "running daemon construction differs from the current integration; "
                "restart required",
            )
        )
    elif daemon.available:
        checks.append(
            Check(
                "runtime construction",
                OK,
                f"matched {expected_construction}",
            )
        )

    if integration.manifest is not None and daemon.available and daemon.app_server_version:
        try:
            configured_codex = str(validate_codex_host_version(integration.manifest.agent_version))
        except CodexHostVersionError:
            pass  # The integration check already reports the invalid recorded host.
        else:
            runtime_codex = daemon.app_server_version
            if runtime_codex == configured_codex:
                checks.append(
                    Check("Codex host version", OK, f"setup/runtime matched {runtime_codex}")
                )
            else:
                checks.append(
                    Check(
                        "Codex host version",
                        WARN,
                        f"setup recorded Codex {configured_codex}, connected App Server is "
                        f"{runtime_codex}; run `spotter setup codex` to reconcile after a "
                        "Codex upgrade",
                    )
                )

    endpoint = (
        integration.manifest.app_server_endpoint if integration.manifest is not None else None
    )
    if endpoint is not None and daemon.available:
        capability_summary = ", ".join(
            f"{name}={value}" for name, value in (daemon.app_server_capabilities or ())
        )
        epoch = daemon.app_server_connection_epoch
        version = daemon.app_server_version or "unknown"
        if daemon.app_server_state is None:
            checks.append(
                Check(
                    "App Server runtime",
                    WARN,
                    "running daemon does not report App Server connection identity; "
                    "restart required",
                )
            )
        elif daemon.app_server_state != "ready":
            checks.append(
                Check(
                    "App Server runtime",
                    WARN,
                    f"{daemon.app_server_state}; epoch={epoch or 'unknown'}; codex={version}",
                )
            )
        elif daemon.app_server_capabilities_changed:
            checks.append(
                Check(
                    "App Server runtime",
                    WARN,
                    f"capabilities changed at epoch {epoch}; codex={version}; {capability_summary}",
                )
            )
        elif daemon.app_server_server_changed:
            checks.append(
                Check(
                    "App Server runtime",
                    INFO,
                    f"server identity changed and reconciled at epoch {epoch}; codex={version}; "
                    f"{capability_summary}",
                )
            )
        else:
            checks.append(
                Check(
                    "App Server runtime",
                    OK,
                    f"ready epoch={epoch}; codex={version}; {capability_summary}",
                )
            )
    if endpoint is None:
        capability_status = WARN if configured else INFO
        checks.extend(
            [
                Check(
                    "observation",
                    capability_status,
                    "unavailable: App Server endpoint is not configured; "
                    "Hook enforcement is independent",
                ),
                Check(
                    "live control",
                    capability_status,
                    "unavailable: App Server endpoint is not configured",
                ),
            ]
        )
    elif deep:
        checks.extend(asyncio.run(_probe_app_server(endpoint)))
    else:
        capabilities = dict(daemon.app_server_capabilities or ())
        observation = capabilities.get("observation", "unknown")
        controls = tuple(capabilities.get(name, "unknown") for name in ("steer", "interrupt"))
        display_endpoint = display_app_server_endpoint(endpoint)
        runtime_ready = (
            daemon.health == RuntimeHealth.HEALTHY and daemon.app_server_state == "ready"
        )
        observation_ready = runtime_ready and observation == "available"
        if not runtime_ready:
            control_status = WARN
            control_detail = (
                f"unavailable while daemon is {daemon.app_server_state or daemon.health.value}; "
                f"last negotiated steer/interrupt {'/'.join(controls)}"
            )
        elif all(control == "available" for control in controls):
            control_status = OK
            control_detail = f"daemon reports steer/interrupt {'/'.join(controls)}"
        elif "unavailable" in controls:
            control_status = WARN
            control_detail = f"daemon reports steer/interrupt {'/'.join(controls)}"
        else:
            control_status = INFO
            control_detail = f"daemon reports steer/interrupt {'/'.join(controls)}"
        checks.extend(
            [
                Check(
                    "observation",
                    OK if observation_ready else WARN,
                    f"{display_endpoint}; daemon {daemon.app_server_state or 'unknown'}; "
                    f"observation {observation}",
                ),
                Check(
                    "live control",
                    control_status,
                    control_detail,
                ),
            ]
        )

    if integration.hook_ready and daemon.health == RuntimeHealth.HEALTHY:
        checks.append(Check("enforcement", OK, "PreToolUse Hook and daemon gate RPC available"))
    elif integration.hook_ready and configured:
        checks.append(
            Check(
                "enforcement",
                WARN,
                "PreToolUse Hook active; daemon RPC unavailable, using bounded local fallback",
            )
        )
    elif integration.hook_ready:
        checks.append(Check("enforcement", INFO, "legacy Hook local enforcement available"))
    else:
        checks.append(
            Check(
                "enforcement",
                FAIL if configured else INFO,
                "managed owned Hook unavailable",
            )
        )

    checks.extend(
        [
            Check(
                "runtime state",
                INFO,
                "active/dormant thread counts depend on live App Server ingestion",
            ),
            Check(
                "review queue",
                INFO,
                "signal-driven jobs are durable; execution requires reviewer.on_signals opt-in",
            ),
        ]
    )
    return checks


def run(config_path: Path | None = None) -> list[Check]:
    checks = [check_interpreter()]
    checks.extend(check_runtime(deep=True))
    checks.extend(check_registration())
    checks.extend(check_storage())
    checks.append(check_roundtrip(config_path))
    checks.append(check_ledger())
    checks.append(check_freshness())
    return checks


def worst(checks: list[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    if any(c.status == WARN for c in checks):
        return WARN
    return OK
