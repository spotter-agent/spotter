import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.daemon import DaemonStatus, ManagedServiceManager, RuntimeHealth
from spotter.integration import (
    CodexInstall,
    IntegrationError,
    IntegrationManager,
    IntegrationManifest,
)


class FakeService:
    def __init__(self, registration_path: Path) -> None:
        self.registration_path = registration_path
        self.health = RuntimeHealth.UNAVAILABLE
        self.starts = 0
        self.stops = 0
        self.uninstalls = 0

    async def start(self) -> DaemonStatus:
        self.starts += 1
        self.health = RuntimeHealth.HEALTHY
        return await self.status()

    async def stop(self) -> DaemonStatus:
        self.stops += 1
        self.health = RuntimeHealth.UNAVAILABLE
        return await self.status()

    async def restart(self) -> DaemonStatus:
        await self.stop()
        return await self.start()

    async def status(self) -> DaemonStatus:
        return DaemonStatus(self.health)

    async def uninstall(self) -> DaemonStatus:
        self.uninstalls += 1
        return await self.stop()


class FailingUninstallService(FakeService):
    async def uninstall(self) -> DaemonStatus:
        raise OSError("service busy")


@pytest.fixture()
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    spotter_home = tmp_path / "spotter"
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("SPOTTER_HOME", str(spotter_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return spotter_home, codex_home


def _manager(
    homes: tuple[Path, Path], *, verifier: bool = True
) -> tuple[IntegrationManager, FakeService]:
    spotter_home, codex_home = homes
    service = FakeService(spotter_home / "service/spotterd")
    manager = IntegrationManager(
        codex_home=codex_home,
        codex=CodexInstall("/opt/homebrew/bin/codex", "codex 1.0", True, True),
        service=service,
        spotter_executable="/opt/homebrew/bin/spotter",
        verifier=lambda _: verifier,
    )
    return manager, service


def _legacy_hooks() -> dict[str, object]:
    other = {"type": "command", "command": "notify-user"}
    legacy = {"type": "command", "command": "/plugins/spotter/scripts/spotter-hook"}
    return {
        "custom": "preserved",
        "hooks": {
            "SessionStart": [{"hooks": [other]}],
            "UserPromptSubmit": [{"hooks": [legacy]}],
            "PreToolUse": [{"matcher": ".*", "hooks": [legacy]}],
            "PostToolUse": [{"matcher": ".*", "hooks": [legacy]}],
        },
    }


def _spotter_hooks(path: Path) -> list[tuple[str, dict[str, object]]]:
    raw = json.loads(path.read_text())
    return [
        (event, hook)
        for event, groups in raw["hooks"].items()
        for group in groups
        for hook in group["hooks"]
        if "spotter" in hook["command"]
    ]


def test_setup_is_idempotent_and_teardown_preserves_unowned_config(
    homes: tuple[Path, Path],
) -> None:
    _, codex_home = homes
    codex_home.mkdir()
    hooks_path = codex_home / "hooks.json"
    hooks_path.write_text(json.dumps(_legacy_hooks()))
    manager, service = _manager(homes)

    first = manager.setup()
    first_hooks = hooks_path.read_bytes()
    second = manager.setup()

    assert first.state == second.state == "ready"
    assert first.created_at == second.created_at
    assert len(second.legacy_hooks_removed) == 3
    assert [event for event, _ in _spotter_hooks(hooks_path)] == ["PreToolUse"]
    assert service.starts == 2
    assert hooks_path.read_bytes() == first_hooks

    assert manager.teardown()
    remaining = json.loads(hooks_path.read_text())
    assert remaining["custom"] == "preserved"
    assert remaining["hooks"] == {
        "SessionStart": [{"hooks": [{"command": "notify-user", "type": "command"}]}]
    }
    assert service.uninstalls == 1
    assert not manager.manifest_path.exists()

    manager.setup()
    assert [event for event, _ in _spotter_hooks(hooks_path)] == ["PreToolUse"]


def test_setup_and_teardown_remove_a_hooks_file_created_by_spotter(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)
    manifest = manager.setup()

    assert manifest.hooks_file_created
    assert manager.hooks_path.exists()
    assert manager.teardown()
    assert not manager.hooks_path.exists()


def test_setup_records_app_server_endpoint_as_pending(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)

    manifest = manager.setup()

    assert manifest.app_server_strategy == "pending-external"
    assert manifest.app_server_endpoint is None


def test_failed_verification_rolls_back_hooks_service_and_manifest(
    homes: tuple[Path, Path],
) -> None:
    _, codex_home = homes
    codex_home.mkdir()
    hooks_path = codex_home / "hooks.json"
    original = json.dumps(_legacy_hooks()).encode()
    hooks_path.write_bytes(original)
    manager, service = _manager(homes, verifier=False)

    with pytest.raises(IntegrationError, match="rolled back"):
        manager.setup()

    assert hooks_path.read_bytes() == original
    assert service.uninstalls == 1
    assert not manager.manifest_path.exists()


def test_manifest_commit_failure_rolls_back_applied_mutations(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, service = _manager(homes)

    def fail_save(self: IntegrationManifest, path: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(IntegrationManifest, "save", fail_save)
    with pytest.raises(IntegrationError, match="rolled back"):
        manager.setup()

    assert not manager.hooks_path.exists()
    assert service.uninstalls == 1
    assert not manager.manifest_path.exists()


def test_legacy_plugin_migration_is_recorded_and_rolled_back_on_failure(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, codex_home = homes
    codex_home.mkdir()
    config = codex_home / "config.toml"
    original = b'[plugins."spotter@spotter"]\nenabled = true\n'
    config.write_bytes(original)
    manager, _ = _manager(homes, verifier=False)

    def remove_plugin(self: CodexInstall, selector: str, home: Path) -> None:
        assert selector == "spotter@spotter"
        (home / "config.toml").write_text("")

    monkeypatch.setattr(CodexInstall, "remove_plugin", remove_plugin)
    with pytest.raises(IntegrationError):
        manager.setup()

    assert config.read_bytes() == original


def test_successful_legacy_plugin_migration_is_recorded(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, codex_home = homes
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text('[plugins."spotter@spotter"]\nenabled = true\n')
    manager, _ = _manager(homes)

    def remove_plugin(self: CodexInstall, selector: str, home: Path) -> None:
        (home / "config.toml").write_text("")

    monkeypatch.setattr(CodexInstall, "remove_plugin", remove_plugin)
    manifest = manager.setup()

    assert manifest.legacy_plugins_removed == ["spotter@spotter"]
    assert config.read_text() == ""


def test_teardown_keeps_a_user_modified_owned_hook(homes: tuple[Path, Path]) -> None:
    manager, _ = _manager(homes)
    manager.setup()
    raw = json.loads(manager.hooks_path.read_text())
    raw["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "user replacement"
    manager.hooks_path.write_text(json.dumps(raw))

    assert manager.teardown()
    assert "user replacement" in manager.hooks_path.read_text()


def test_setup_preserves_unrelated_commands_that_only_mention_spotter_hook(
    homes: tuple[Path, Path],
) -> None:
    _, codex_home = homes
    codex_home.mkdir()
    hooks = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": 'echo "run spotter hook later"'}]}
            ]
        }
    }
    (codex_home / "hooks.json").write_text(json.dumps(hooks))
    manager, _ = _manager(homes)

    manager.setup()

    assert "run spotter hook later" in manager.hooks_path.read_text()


def test_portable_setup_does_not_claim_a_preexisting_daemon(
    homes: tuple[Path, Path],
) -> None:
    spotter_home, codex_home = homes
    service = FakeService(spotter_home / "unused")
    service.health = RuntimeHealth.HEALTHY
    manager = IntegrationManager(
        codex_home=codex_home,
        codex=CodexInstall("/bin/codex", "codex 1.0", True, True),
        service=service,
        portable=True,
        spotter_executable="/bin/spotter",
        verifier=lambda _: True,
    )

    manifest = manager.setup()
    assert not manifest.service_owned
    assert manager.teardown()
    assert service.uninstalls == 0


def test_teardown_failure_restores_the_owned_hook_and_manifest(
    homes: tuple[Path, Path],
) -> None:
    spotter_home, codex_home = homes
    service = FailingUninstallService(spotter_home / "service")
    manager = IntegrationManager(
        codex_home=codex_home,
        codex=CodexInstall("/bin/codex", "codex 1.0", True, True),
        service=service,
        spotter_executable="/bin/spotter",
        verifier=lambda _: True,
    )
    manager.setup()
    before = manager.hooks_path.read_bytes()

    with pytest.raises(IntegrationError, match="teardown rolled back"):
        manager.teardown()

    assert manager.hooks_path.read_bytes() == before
    assert manager.manifest_path.exists()


def test_teardown_does_not_require_codex_to_still_be_installed(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, service = _manager(homes)
    manager.setup()

    def missing() -> CodexInstall:
        raise IntegrationError("Codex is not installed")

    monkeypatch.setattr(CodexInstall, "detect", missing)
    teardown = IntegrationManager(
        codex_home=homes[1],
        service=service,
        spotter_executable="/bin/spotter",
        verifier=lambda _: True,
    )

    assert teardown.teardown()
    assert not teardown.manifest_path.exists()


def test_newer_manifest_schema_is_refused(homes: tuple[Path, Path]) -> None:
    manager, _ = _manager(homes)
    manager.manifest_path.parent.mkdir(parents=True)
    manager.manifest_path.write_text('{"schema": 999}')

    with pytest.raises(IntegrationError, match="unsupported"):
        IntegrationManifest.load(manager.manifest_path)


def test_invalid_spotter_config_fails_before_external_mutation(
    homes: tuple[Path, Path],
) -> None:
    spotter_home, codex_home = homes
    spotter_home.mkdir()
    config = spotter_home / "spotter.toml"
    config.write_text("not valid toml")
    service = FakeService(spotter_home / "service")
    manager = IntegrationManager(
        codex_home=codex_home,
        codex=CodexInstall("/bin/codex", "codex 1.0", True, True),
        service=service,
        spotter_executable="/bin/spotter",
        config_path=config,
        verifier=lambda _: True,
    )

    with pytest.raises(IntegrationError, match="config is unusable"):
        manager.setup()
    assert not (codex_home / "hooks.json").exists()
    assert service.starts == 0


@pytest.mark.parametrize("platform,suffix", [("darwin", ".plist"), ("linux", ".service")])
def test_managed_service_definition_is_private_and_idempotent(
    homes: tuple[Path, Path], platform: str, suffix: str
) -> None:
    spotter_home, _ = homes
    path = spotter_home / f"service{suffix}"
    service = ManagedServiceManager(
        platform=platform,
        registration_path=path,
        executable="/opt/homebrew/bin/spotterd",
    )

    assert service._install_definition()  # noqa: SLF001 - verifies the service artifact contract
    assert not service._install_definition()  # noqa: SLF001
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"spotterd" in path.read_bytes()


def test_systemd_definition_escapes_percent_specifiers(homes: tuple[Path, Path]) -> None:
    spotter_home, _ = homes
    service = ManagedServiceManager(
        platform="linux",
        registration_path=spotter_home / "service",
        executable="/opt/100%/spotterd",
    )

    assert b"/opt/100%%/spotterd" in service._definition()  # noqa: SLF001


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("darwin", ["launchctl", "bootstrap"]),
        ("linux", ["systemctl", "--user", "enable", "--now"]),
    ],
)
def test_managed_service_start_registers_and_verifies(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    expected: list[str],
) -> None:
    spotter_home, _ = homes
    service = ManagedServiceManager(
        platform=platform,
        registration_path=spotter_home / "service",
        executable="/bin/spotterd",
    )
    commands: list[list[str]] = []
    statuses = iter([RuntimeHealth.UNAVAILABLE, RuntimeHealth.HEALTHY])

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        inactive = command[:2] == ["launchctl", "print"] or command[-2:] == [
            "is-active",
            "spotterd.service",
        ]
        return subprocess.CompletedProcess(command, 1 if inactive else 0, "", "")

    async def healthy() -> DaemonStatus:
        return DaemonStatus(next(statuses, RuntimeHealth.HEALTHY))

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service, "status", healthy)

    assert asyncio.run(service.start()).health == RuntimeHealth.HEALTHY
    assert any(command[: len(expected)] == expected for command in commands)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_managed_service_refuses_to_claim_an_unmanaged_running_daemon(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    spotter_home, _ = homes
    path = spotter_home / "service"
    service = ManagedServiceManager(
        platform=platform, registration_path=path, executable="/bin/spotterd"
    )

    def inactive(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "")

    async def healthy() -> DaemonStatus:
        return DaemonStatus(RuntimeHealth.HEALTHY, pid=123)

    monkeypatch.setattr(service, "_run", inactive)
    monkeypatch.setattr(service, "status", healthy)

    status = asyncio.run(service.start())
    assert status.health == RuntimeHealth.DEGRADED
    assert "outside the managed" in (status.detail or "")
    assert not path.exists()


def test_setup_dry_run_makes_no_external_changes(
    homes: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spotter_home, codex_home = homes
    manager, _ = _manager(homes)
    monkeypatch.setattr("spotter.cli.IntegrationManager", lambda **_: manager)
    assert main(["setup", "codex", "--dry-run"]) == 0
    assert "dry-run: no changes made" in capsys.readouterr().out
    assert not (codex_home / "hooks.json").exists()
    assert not (spotter_home / "integrations").exists()


def test_setup_cli_does_not_print_an_unverified_remote_command(
    homes: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _ = _manager(homes)
    monkeypatch.setattr("spotter.cli.IntegrationManager", lambda **_: manager)

    assert main(["setup", "codex"]) == 0

    output = capsys.readouterr().out
    assert "endpoint: pending" in output
    assert "codex --remote" not in output


def test_managed_manifest_routes_daemon_stop_through_the_service_manager(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager(homes)
    manager.setup()
    managed = FakeService(homes[0] / "service")
    managed.health = RuntimeHealth.HEALTHY
    monkeypatch.setattr("spotter.cli.ManagedServiceManager", lambda: managed)

    assert main(["daemon", "stop"]) == 0
    assert managed.stops == 1


def test_portable_is_rejected_for_teardown() -> None:
    with pytest.raises(SystemExit, match="2"):
        main(["teardown", "codex", "--portable"])
