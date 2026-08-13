import json
from dataclasses import replace
from pathlib import Path

import pytest

from spotter.app_server import AppServerTransportError, CodexAppServerClient
from spotter.cli import main
from spotter.daemon import DaemonClient, DaemonStatus, RuntimeHealth
from spotter.doctor import FAIL, INFO, OK, WARN, Check, check_integration, check_runtime, worst
from spotter.integration import MANIFEST_SCHEMA, IntegrationManifest
from spotter.snapshot import StepJournal
from spotter.trace import TraceEvent


@pytest.fixture()
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    spotter = tmp_path / "spotter"
    codex = tmp_path / "codex"
    monkeypatch.setenv("SPOTTER_HOME", str(spotter))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return spotter, codex


def _ready_manifest(homes: tuple[Path, Path]) -> IntegrationManifest:
    spotter, codex = homes
    codex.mkdir(parents=True)
    owned = {
        "event": "PreToolUse",
        "matcher": ".*",
        "hook": {"type": "command", "command": "/bin/spotter hook || true"},
    }
    hooks = {
        "hooks": {
            "PreToolUse": [{"matcher": owned["matcher"], "hooks": [owned["hook"]]}],
            "SessionStart": [{"hooks": [owned["hook"]]}],
        }
    }
    hooks_path = codex / "hooks.json"
    hooks_path.write_text(json.dumps(hooks))
    registration = spotter / "service/spotterd"
    registration.parent.mkdir(parents=True)
    registration.write_text("managed")
    manifest = IntegrationManifest(
        schema=MANIFEST_SCHEMA,
        state="ready",
        agent="codex",
        setup_by="test",
        agent_path="/bin/codex",
        agent_version="test",
        codex_home=str(codex),
        app_server_strategy="pending-external",
        app_server_endpoint=None,
        runtime_mode="managed",
        service_registration=str(registration),
        service_owned=True,
        hooks_file=str(hooks_path),
        hooks_file_created=True,
        owned_hooks=[
            owned,
            {"event": "SessionStart", "matcher": None, "hook": owned["hook"]},
        ],
    )
    manifest.save(spotter / "integrations/codex.json")
    return manifest


def _daemon_status(monkeypatch: pytest.MonkeyPatch, health: RuntimeHealth) -> None:
    async def status(self: DaemonClient) -> DaemonStatus:
        return DaemonStatus(
            health, pid=123 if health != RuntimeHealth.UNAVAILABLE else None, protocol=1
        )

    monkeypatch.setattr(DaemonClient, "status", status)


def test_running_daemon_without_app_server_is_degraded_but_enforcement_remains(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_manifest(homes)
    _daemon_status(monkeypatch, RuntimeHealth.HEALTHY)

    checks = check_runtime()
    by_name = {check.name: check for check in checks}

    assert by_name["daemon"].status == OK
    assert by_name["observation"].status == WARN
    assert "Hook enforcement is independent" in by_name["observation"].detail
    assert by_name["enforcement"].status == OK
    assert by_name["runtime state"].status == INFO
    assert by_name["review queue"].status == INFO
    assert worst(checks) == WARN


def test_configured_integration_with_dead_daemon_is_broken_with_local_fallback(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_manifest(homes)
    _daemon_status(monkeypatch, RuntimeHealth.UNAVAILABLE)

    by_name = {check.name: check for check in check_runtime()}

    assert by_name["daemon"].status == FAIL
    assert by_name["enforcement"].status == WARN
    assert "local fallback" in by_name["enforcement"].detail


def test_integration_check_detects_duplicate_owned_hooks(homes: tuple[Path, Path]) -> None:
    manifest = _ready_manifest(homes)
    hooks = json.loads(Path(manifest.hooks_file).read_text())
    hooks["hooks"]["PreToolUse"].append(hooks["hooks"]["PreToolUse"][0])
    Path(manifest.hooks_file).write_text(json.dumps(hooks))

    check = check_integration().check

    assert check.status == FAIL
    assert "duplicated" in check.detail


def test_integration_check_detects_a_stale_legacy_plugin(homes: tuple[Path, Path]) -> None:
    manifest = _ready_manifest(homes)
    (Path(manifest.codex_home) / "config.toml").write_text(
        '[plugins."spotter@spotter"]\nenabled = true\n'
    )

    check = check_integration().check

    assert check.status == FAIL
    assert "stale legacy plugin" in check.detail


def test_integration_check_reports_a_malformed_owned_hook(homes: tuple[Path, Path]) -> None:
    _ready_manifest(homes)
    path = homes[0] / "integrations/codex.json"
    raw = json.loads(path.read_text())
    raw["owned_hooks"] = "broken"
    path.write_text(json.dumps(raw))

    check = check_integration().check

    assert check.status == FAIL
    assert "owned Hooks are invalid" in check.detail


def test_doctor_probe_reports_an_unreachable_configured_app_server(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = replace(_ready_manifest(homes), app_server_endpoint="ws://127.0.0.1:1")
    manifest.save(homes[0] / "integrations/codex.json")
    _daemon_status(monkeypatch, RuntimeHealth.HEALTHY)

    async def fail(self: CodexAppServerClient) -> None:
        raise AppServerTransportError("connection refused")

    monkeypatch.setattr(CodexAppServerClient, "connect", fail)

    by_name = {check.name: check for check in check_runtime(deep=True)}

    assert by_name["observation"].status == WARN
    assert "disconnected" in by_name["observation"].detail
    assert by_name["live control"].status == WARN


@pytest.mark.parametrize(
    "health,expected",
    [(OK, 0), (INFO, 0), (WARN, 1), (FAIL, 2)],
)
def test_status_exit_code_matches_runtime_health(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    health: str,
    expected: int,
) -> None:
    sessions = homes[0] / "sessions"
    sessions.mkdir(parents=True)
    StepJournal(sessions / "current.jsonl").record(TraceEvent("x"))
    monkeypatch.setattr("spotter.cli.check_runtime", lambda: [Check("runtime", health, "test")])

    assert main(["status"]) == expected


def test_healthy_legacy_hook_without_observations_can_exit_zero(
    homes: tuple[Path, Path],
) -> None:
    spotter, codex = homes
    spotter.mkdir()
    codex.mkdir()
    (codex / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [{"type": "command", "command": "/bin/spotter hook || true"}],
                        }
                    ]
                }
            }
        )
    )
    assert main(["status"]) == 0
