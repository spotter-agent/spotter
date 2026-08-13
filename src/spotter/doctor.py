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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from spotter.budget import LedgerCorrupt, spend_totals
from spotter.daemon import DaemonClient, DaemonStatus, RuntimeHealth
from spotter.paths import spotter_home

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


def check_roundtrip(config_path: Path | None) -> Check:
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
    args = [sys.executable, "-m", "spotter", "hook"]
    if config_path is not None:
        args += ["--config", str(config_path)]
    try:
        result = subprocess.run(
            args,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "SPOTTER_DISABLE": "1"},
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

    manifest_path = spotter_home() / "integrations" / "codex.json"
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
    if status.health == RuntimeHealth.HEALTHY:
        detail = f"healthy pid={status.pid} protocol={status.protocol}"
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
        return (
            Check("observation", WARN, f"App Server disconnected: {error}"),
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

    endpoint = (
        integration.manifest.app_server_endpoint if integration.manifest is not None else None
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
        checks.extend(
            [
                Check("observation", WARN, f"endpoint configured; run doctor to probe {endpoint}"),
                Check("live control", WARN, "not probed by status"),
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
                "active/dormant thread counts unknown until App Server ingestion (#85)",
            ),
            Check("review queue", INFO, "no durable reviewer queue is implemented"),
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
