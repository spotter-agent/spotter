import json
from dataclasses import replace
from pathlib import Path

import pytest

from spotter.app_server import AppServerTransportError, CodexAppServerClient
from spotter.build_identity import current_build_identity
from spotter.cli import main
from spotter.daemon import DaemonClient, DaemonStatus, RuntimeCompatibility, RuntimeHealth
from spotter.doctor import FAIL, INFO, OK, WARN, Check, check_integration, check_runtime, worst
from spotter.integration import MANIFEST_SCHEMA, IntegrationManifest
from spotter.paths import RuntimeLayout
from spotter.runtime_fingerprint import expected_runtime_construction_fingerprint
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
            "UserPromptSubmit": [{"hooks": [owned["hook"]]}],
            "PostToolUse": [{"matcher": ".*", "hooks": [owned["hook"]]}],
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
        agent_version="codex-cli 1.0.0",
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
            {"event": "UserPromptSubmit", "matcher": None, "hook": owned["hook"]},
            {"event": "PostToolUse", "matcher": ".*", "hook": owned["hook"]},
        ],
    )
    manifest.save(spotter / "integrations/codex.json")
    return manifest


def _daemon_status(monkeypatch: pytest.MonkeyPatch, health: RuntimeHealth) -> None:
    async def status(self: DaemonClient) -> DaemonStatus:
        return DaemonStatus(
            health,
            pid=123 if health != RuntimeHealth.UNAVAILABLE else None,
            protocol=1,
            build_id=current_build_identity().build_id,
            compatibility=(
                RuntimeCompatibility.MATCHED
                if health != RuntimeHealth.UNAVAILABLE
                else RuntimeCompatibility.UNKNOWN
            ),
            construction_fingerprint=(
                expected_runtime_construction_fingerprint(RuntimeLayout.discover())
                if health != RuntimeHealth.UNAVAILABLE
                else None
            ),
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


def test_configured_endpoint_reports_the_supported_codex_launch(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = replace(_ready_manifest(homes), app_server_endpoint="ws://127.0.0.1:4500")
    manifest.save(homes[0] / "integrations/codex.json")
    _daemon_status(monkeypatch, RuntimeHealth.HEALTHY)

    check = {item.name: item for item in check_runtime()}["Codex launch"]

    assert check.status == INFO
    assert "use `spotter codex`" in check.detail
    assert "plain `codex` selects an embedded App Server" in check.detail


def test_integration_check_detects_duplicate_owned_hooks(homes: tuple[Path, Path]) -> None:
    manifest = _ready_manifest(homes)
    hooks = json.loads(Path(manifest.hooks_file).read_text())
    hooks["hooks"]["PreToolUse"].append(hooks["hooks"]["PreToolUse"][0])
    Path(manifest.hooks_file).write_text(json.dumps(hooks))

    check = check_integration().check

    assert check.status == FAIL
    assert "duplicated" in check.detail


def test_integration_check_rejects_an_unsupported_recorded_codex(
    homes: tuple[Path, Path],
) -> None:
    manifest = replace(_ready_manifest(homes), agent_version="codex-cli 0.146.9")
    manifest.save(homes[0] / "integrations/codex.json")

    check = check_integration().check

    assert check.status == FAIL
    assert "too old" in check.detail
    assert "spotter setup codex" in check.detail


def test_integration_check_ignores_owned_hook_order(homes: tuple[Path, Path]) -> None:
    _ready_manifest(homes)
    path = homes[0] / "integrations/codex.json"
    raw = json.loads(path.read_text())
    raw["owned_hooks"].reverse()
    path.write_text(json.dumps(raw))

    assert check_integration().check.status == OK


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


def test_integration_check_diagnoses_a_removed_package_after_reinstall(
    homes: tuple[Path, Path],
) -> None:
    manifest = replace(
        _ready_manifest(homes),
        setup_build_id=current_build_identity().build_id,
        integration_generation="generation",
        runtime_layout={
            "cli_executable": str(homes[0] / "removed/bin/spotter"),
            "daemon_executable": str(homes[0] / "removed/bin/spotterd"),
        },
    )
    manifest.save(homes[0] / "integrations/codex.json")

    check = check_integration().check

    assert check.status == FAIL
    assert "owned Hooks fail open" in check.detail
    assert "after reinstall" in check.detail


def test_runtime_check_distinguishes_a_running_old_daemon_build(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_manifest(homes)

    async def old_status(self: DaemonClient) -> DaemonStatus:
        return DaemonStatus(
            RuntimeHealth.HEALTHY,
            pid=123,
            protocol=1,
            build_id="retired-build",
            compatibility=RuntimeCompatibility.COMPATIBLE_STALE,
        )

    monkeypatch.setattr(DaemonClient, "status", old_status)

    check = {item.name: item for item in check_runtime()}["daemon"]

    assert check.status == WARN
    assert "retired-build" in check.detail
    assert "restart required" in check.detail


def test_runtime_check_distinguishes_stale_construction(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_manifest(homes)

    async def stale_status(self: DaemonClient) -> DaemonStatus:
        return DaemonStatus(
            RuntimeHealth.HEALTHY,
            pid=123,
            protocol=1,
            build_id=current_build_identity().build_id,
            compatibility=RuntimeCompatibility.MATCHED,
            construction_fingerprint="runtime-stale",
        )

    monkeypatch.setattr(DaemonClient, "status", stale_status)

    check = {item.name: item for item in check_runtime()}["runtime construction"]

    assert check.status == WARN
    assert "restart required" in check.detail


def test_runtime_check_fails_an_incompatible_running_daemon(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_manifest(homes)

    async def incompatible_status(self: DaemonClient) -> DaemonStatus:
        return DaemonStatus(
            RuntimeHealth.DEGRADED,
            detail="running daemon protocol 2 is incompatible",
            compatibility=RuntimeCompatibility.INCOMPATIBLE_STALE,
        )

    monkeypatch.setattr(DaemonClient, "status", incompatible_status)

    check = {item.name: item for item in check_runtime()}["daemon"]

    assert check.status == FAIL
    assert "spotter daemon restart" in check.detail


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


def test_status_redacts_configured_app_server_query_values(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "ws://127.0.0.1:4321?token=diagnostic-secret"
    manifest = replace(_ready_manifest(homes), app_server_endpoint=endpoint)
    manifest.save(homes[0] / "integrations/codex.json")
    _daemon_status(monkeypatch, RuntimeHealth.HEALTHY)

    checks = check_runtime()
    rendered = "\n".join(check.detail for check in checks)

    assert "diagnostic-secret" not in rendered
    assert "ws://127.0.0.1:4321?<redacted>" in rendered


def test_status_uses_the_daemons_ready_app_server_capabilities(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "ws://127.0.0.1:4321"
    manifest = replace(_ready_manifest(homes), app_server_endpoint=endpoint)
    manifest.save(homes[0] / "integrations/codex.json")

    async def ready_status(self: DaemonClient) -> DaemonStatus:
        return DaemonStatus(
            RuntimeHealth.HEALTHY,
            build_id=current_build_identity().build_id,
            compatibility=RuntimeCompatibility.MATCHED,
            construction_fingerprint=expected_runtime_construction_fingerprint(
                RuntimeLayout.discover()
            ),
            app_server_state="ready",
            app_server_version="0.147.0",
            app_server_connection_epoch=2,
            app_server_capabilities=(
                ("observation", "available"),
                ("thread_query", "available"),
                ("steer", "unknown"),
                ("interrupt", "unknown"),
            ),
        )

    monkeypatch.setattr(DaemonClient, "status", ready_status)

    by_name = {check.name: check for check in check_runtime()}

    assert by_name["App Server runtime"].status == OK
    assert by_name["observation"].status == OK
    assert "observation available" in by_name["observation"].detail
    assert by_name["live control"].status == INFO
    assert "unknown/unknown" in by_name["live control"].detail


def test_status_does_not_report_retained_controls_as_live_while_disconnected(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "ws://127.0.0.1:4321"
    manifest = replace(_ready_manifest(homes), app_server_endpoint=endpoint)
    manifest.save(homes[0] / "integrations/codex.json")

    async def backing_off_status(self: DaemonClient) -> DaemonStatus:
        return DaemonStatus(
            RuntimeHealth.DEGRADED,
            build_id=current_build_identity().build_id,
            compatibility=RuntimeCompatibility.MATCHED,
            construction_fingerprint=expected_runtime_construction_fingerprint(
                RuntimeLayout.discover()
            ),
            app_server_state="backing_off",
            app_server_connection_epoch=2,
            app_server_capabilities=(
                ("observation", "available"),
                ("steer", "available"),
                ("interrupt", "available"),
            ),
        )

    monkeypatch.setattr(DaemonClient, "status", backing_off_status)

    by_name = {check.name: check for check in check_runtime()}

    assert by_name["App Server runtime"].status == WARN
    assert by_name["observation"].status == WARN
    assert by_name["live control"].status == WARN
    assert "unavailable while daemon is backing_off" in by_name["live control"].detail
    assert "last negotiated steer/interrupt available/available" in by_name["live control"].detail


def test_runtime_check_reports_reconciled_capability_change(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = replace(_ready_manifest(homes), app_server_endpoint="ws://127.0.0.1:4321")
    manifest.save(homes[0] / "integrations/codex.json")

    async def changed_status(self: DaemonClient) -> DaemonStatus:
        return DaemonStatus(
            RuntimeHealth.HEALTHY,
            pid=123,
            protocol=1,
            build_id=current_build_identity().build_id,
            compatibility=RuntimeCompatibility.MATCHED,
            construction_fingerprint=expected_runtime_construction_fingerprint(
                RuntimeLayout.discover()
            ),
            app_server_state="ready",
            app_server_connection_epoch=3,
            app_server_capabilities=(
                ("observation", "available"),
                ("steer", "unavailable"),
            ),
            app_server_capabilities_changed=True,
        )

    monkeypatch.setattr(DaemonClient, "status", changed_status)

    check = {item.name: item for item in check_runtime()}["App Server runtime"]

    assert check.status == WARN
    assert "changed at epoch 3" in check.detail
    assert "steer=unavailable" in check.detail


@pytest.mark.parametrize(
    ("configured", "connected", "expected", "detail"),
    [
        ("codex-cli 0.147.0", "0.147.0", OK, "matched 0.147.0"),
        ("codex-cli 0.147.0", "0.150.0", WARN, "spotter setup codex"),
        ("codex-cli 0.150.0", "0.147.0", WARN, "spotter setup codex"),
    ],
)
def test_runtime_check_classifies_mixed_codex_upgrade_versions(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    connected: str,
    expected: str,
    detail: str,
) -> None:
    manifest = replace(
        _ready_manifest(homes),
        agent_version=configured,
        app_server_endpoint="ws://127.0.0.1:4321",
    )
    manifest.save(homes[0] / "integrations/codex.json")

    async def status(self: DaemonClient) -> DaemonStatus:
        return DaemonStatus(
            RuntimeHealth.HEALTHY,
            pid=123,
            protocol=1,
            build_id=current_build_identity().build_id,
            compatibility=RuntimeCompatibility.MATCHED,
            construction_fingerprint=expected_runtime_construction_fingerprint(
                RuntimeLayout.discover()
            ),
            app_server_state="ready",
            app_server_version=connected,
            app_server_connection_epoch=4,
            app_server_capabilities=(("observation", "available"),),
        )

    monkeypatch.setattr(DaemonClient, "status", status)

    check = {item.name: item for item in check_runtime()}["Codex host version"]

    assert check.status == expected
    assert detail in check.detail


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
