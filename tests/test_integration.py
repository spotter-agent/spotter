import asyncio
import concurrent.futures
import io
import json
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from spotter.build_identity import BuildIdentity, current_build_identity
from spotter.cli import main
from spotter.daemon import DaemonStatus, ManagedServiceManager, RuntimeHealth
from spotter.integration import (
    MANIFEST_SCHEMA,
    CodexInstall,
    IntegrationError,
    IntegrationManager,
    IntegrationManifest,
)
from spotter.paths import RuntimeLayout


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
        return DaemonStatus(self.health, build_id=current_build_identity().build_id)

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
    assert {event for event, _ in _spotter_hooks(hooks_path)} == {
        "PostToolUse",
        "PreToolUse",
        "SessionStart",
        "UserPromptSubmit",
    }
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
    assert {event for event, _ in _spotter_hooks(hooks_path)} == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
    }


def test_setup_waits_for_an_inflight_lifecycle_mutation(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)
    manager.lock_path.parent.mkdir(parents=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys\n"
                "with pathlib.Path(sys.argv[1]).open('a+') as lock:\n"
                "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
                "    print('locked', flush=True)\n"
                "    sys.stdin.readline()\n"
            ),
            str(manager.lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None
    assert holder.stdin is not None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        assert holder.stdout.readline().strip() == "locked"
        setup = executor.submit(manager.setup)
        time.sleep(0.1)
        waited = not setup.done()
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert setup.result(timeout=2).state == "ready"
        assert waited
    finally:
        if holder.poll() is None:
            holder.terminate()
        holder.wait(timeout=2)
        executor.shutdown(wait=True, cancel_futures=True)


def test_setup_and_teardown_remove_a_hooks_file_created_by_spotter(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)
    manifest = manager.setup()

    assert manifest.hooks_file_created
    assert manager.hooks_path.exists()
    assert manager.teardown()
    assert not manager.hooks_path.exists()


def test_setup_verification_ignores_owned_hook_order(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager(homes)
    owned = list(reversed(manager._owned_hooks()))  # noqa: SLF001
    monkeypatch.setattr(manager, "_owned_hooks", lambda: owned)

    manifest = manager.setup()

    assert manifest.owned_hooks == owned


def test_setup_records_app_server_endpoint_as_pending(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)

    manifest = manager.setup()

    assert manifest.app_server_strategy == "pending-external"
    assert manifest.app_server_endpoint is None


def test_setup_records_stable_layout_build_and_integration_generation(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)

    manifest = manager.setup()

    assert manifest.schema == MANIFEST_SCHEMA == 4
    assert manifest.setup_build_id == current_build_identity().build_id
    assert len(manifest.integration_generation) == 64
    assert manifest.runtime_layout["cli_executable"] == "/opt/homebrew/bin/spotter"
    assert manifest.runtime_layout["daemon_executable"] == "/opt/homebrew/bin/spotterd"
    commands = [str(entry[1]["command"]) for entry in _spotter_hooks(manager.hooks_path)]
    assert commands
    assert all(manifest.integration_generation in command for command in commands)
    assert all("SPOTTER_HOME=" in command and "|| true" in command for command in commands)


def test_stale_cached_hook_generation_fails_open(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manager, _ = _manager(homes)
    manifest = manager.setup()
    payload = '{"hook_event_name":"SessionStart","session_id":"stale"}'
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))

    assert main(["hook", "--integration-generation", "retired-generation"]) == 0

    assert "stale integration generation" in capsys.readouterr().err
    assert not (homes[0] / "sessions/stale.jsonl").exists()
    assert manifest.integration_generation != "retired-generation"


def test_cached_hook_from_the_installed_old_build_fails_open_before_reconcile(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = BuildIdentity("1.0.0", "v1.0.0@old", "v1.0.0", "old")
    new = BuildIdentity("1.0.1", "v1.0.1@new", "v1.0.1", "new")
    monkeypatch.setattr("spotter.integration.current_build_identity", lambda: old)
    manager, _ = _manager(homes)
    manifest = manager.setup()
    monkeypatch.setattr("spotter.cli.current_build_identity", lambda: new)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"hook_event_name":"SessionStart","session_id":"mixed"}'),
    )

    assert main(["hook", "--integration-generation", manifest.integration_generation]) == 0

    assert "integration package build is stale" in capsys.readouterr().err
    assert not (homes[0] / "sessions/mixed.jsonl").exists()


def test_missing_packaged_bridge_command_is_bounded_and_fail_open(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)

    result = subprocess.run(
        ["sh", "-c", manager._hook_command()],  # noqa: SLF001 - generated host contract
        input="{}",
        capture_output=True,
        text=True,
        timeout=1,
        check=False,
    )

    assert result.returncode == 0


def test_reinstall_at_a_different_stable_prefix_reconciles_owned_state(
    homes: tuple[Path, Path],
) -> None:
    spotter_home, codex_home = homes
    retained = spotter_home / "sessions/retained.jsonl"
    retained.parent.mkdir(parents=True)
    retained.write_text("durable\n")
    first_service = FakeService(spotter_home / "service/spotterd")
    first = IntegrationManager(
        codex_home=codex_home,
        codex=CodexInstall("/bin/codex", "codex 1.0", True, True),
        service=first_service,
        spotter_executable="/old-prefix/opt/spotter/bin/spotter",
        verifier=lambda _: True,
    )
    old = first.setup()

    second = IntegrationManager(
        codex_home=codex_home,
        codex=CodexInstall("/bin/codex", "codex 1.0", True, True),
        service=FakeService(spotter_home / "service/spotterd"),
        spotter_executable="/new-prefix/opt/spotter/bin/spotter",
        verifier=lambda _: True,
    )
    new = second.setup()

    hooks = second.hooks_path.read_text()
    assert old.integration_generation != new.integration_generation
    assert "/new-prefix/opt/spotter/bin/spotter" in hooks
    assert "/old-prefix/opt/spotter/bin/spotter" not in hooks
    assert retained.read_text() == "durable\n"


def test_package_build_change_rotates_generated_integration_identity(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first_identity = BuildIdentity("1.0.0", "v1.0.0@one", "v1.0.0", "one")
    second_identity = BuildIdentity("1.0.1", "v1.0.1@two", "v1.0.1", "two")
    monkeypatch.setattr("spotter.integration.current_build_identity", lambda: first_identity)
    first, _ = _manager(homes)
    old = first.setup()

    monkeypatch.setattr("spotter.integration.current_build_identity", lambda: second_identity)
    second, _ = _manager(homes)
    new = second.setup()

    assert old.setup_build_id == first_identity.build_id
    assert new.setup_build_id == second_identity.build_id
    assert old.integration_generation != new.integration_generation


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


def test_setup_refuses_to_replace_a_user_modified_owned_hook(
    homes: tuple[Path, Path],
) -> None:
    manager, service = _manager(homes)
    manager.setup()
    raw = json.loads(manager.hooks_path.read_text())
    raw["hooks"]["PreToolUse"][0]["hooks"][0]["command"] += " --user-edit"
    manager.hooks_path.write_text(json.dumps(raw))
    before = manager.hooks_path.read_bytes()

    with pytest.raises(IntegrationError, match="ownership is ambiguous"):
        manager.setup()

    assert manager.hooks_path.read_bytes() == before
    assert service.starts == 1


def test_setup_refuses_generated_hooks_when_the_ownership_manifest_is_missing(
    homes: tuple[Path, Path],
) -> None:
    manager, service = _manager(homes)
    manager.setup()
    manager.manifest_path.unlink()
    before = manager.hooks_path.read_bytes()

    with pytest.raises(IntegrationError, match="ownership is ambiguous"):
        manager.setup()

    assert manager.hooks_path.read_bytes() == before
    assert service.starts == 1


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


def test_setup_does_not_treat_a_composite_legacy_hook_command_as_owned(
    homes: tuple[Path, Path],
) -> None:
    _, codex_home = homes
    codex_home.mkdir()
    command = "echo /plugins/spotter/scripts/spotter-hook"
    hooks = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    (codex_home / "hooks.json").write_text(json.dumps(hooks))
    manager, service = _manager(homes)

    with pytest.raises(IntegrationError, match="ownership is ambiguous"):
        manager.setup()

    assert command in manager.hooks_path.read_text()
    assert service.starts == 0


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


def test_schema_one_manifest_is_reconciled_with_observation_hooks(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)
    manager.setup()
    manifest = json.loads(manager.manifest_path.read_text())
    manifest["schema"] = 1
    manifest["owned_hook"] = manifest.pop("owned_hooks")[0]
    manager.manifest_path.write_text(json.dumps(manifest))
    hooks = json.loads(manager.hooks_path.read_text())
    hooks["hooks"].pop("SessionStart")
    manager.hooks_path.write_text(json.dumps(hooks))

    upgraded = manager.setup()

    assert upgraded.schema == MANIFEST_SCHEMA
    assert {event for event, _ in _spotter_hooks(manager.hooks_path)} == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
    }


def test_schema_two_manifest_is_reconciled_with_observation_hooks(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)
    manager.setup()
    manifest = json.loads(manager.manifest_path.read_text())
    manifest["schema"] = 2
    manifest["owned_hooks"] = manifest["owned_hooks"][:2]
    manager.manifest_path.write_text(json.dumps(manifest))
    hooks = json.loads(manager.hooks_path.read_text())
    hooks["hooks"].pop("UserPromptSubmit")
    hooks["hooks"].pop("PostToolUse")
    manager.hooks_path.write_text(json.dumps(hooks))

    upgraded = manager.setup()

    assert upgraded.schema == MANIFEST_SCHEMA
    assert {event for event, _ in _spotter_hooks(manager.hooks_path)} == {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
    }


def test_schema_three_manifest_is_upgraded_with_layout_and_generation(
    homes: tuple[Path, Path],
) -> None:
    manager, _ = _manager(homes)
    manager.setup()
    raw = json.loads(manager.manifest_path.read_text())
    raw["schema"] = 3
    raw.pop("setup_build_id")
    raw.pop("integration_generation")
    raw.pop("runtime_layout")
    manager.manifest_path.write_text(json.dumps(raw))

    upgraded = manager.setup()

    assert upgraded.schema == MANIFEST_SCHEMA
    assert upgraded.setup_build_id == current_build_identity().build_id
    assert len(upgraded.integration_generation) == 64
    assert upgraded.runtime_layout["bridge_command"] == ["/opt/homebrew/bin/spotter", "hook"]


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


def test_setup_records_custom_config_for_daemon_review_execution(
    homes: tuple[Path, Path],
) -> None:
    spotter_home, codex_home = homes
    spotter_home.mkdir()
    config = spotter_home / "custom.toml"
    config.write_text('[main_agent]\nadapter = "codex"\n[reviewer]\non_signals = true\n')
    manager = IntegrationManager(
        codex_home=codex_home,
        codex=CodexInstall("/bin/codex", "codex 1.0", True, True),
        service=FakeService(spotter_home / "service"),
        spotter_executable="/bin/spotter",
        config_path=config,
        verifier=lambda _: True,
    )

    manifest = manager.setup()

    assert manifest.config_path == str(config)


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


@pytest.mark.parametrize(
    "platform,prefix",
    [
        ("darwin", "/opt/homebrew"),
        ("darwin", "/usr/local"),
        ("linux", "/home/linuxbrew/.linuxbrew"),
    ],
)
def test_managed_service_uses_stable_package_and_user_layout(
    homes: tuple[Path, Path], platform: str, prefix: str
) -> None:
    spotter_home, _ = homes
    daemon = Path(prefix) / "opt/spotter/bin/spotterd"
    layout = RuntimeLayout.discover(
        cli_executable=daemon.with_name("spotter"),
        daemon_executable=daemon,
        spotter_root=spotter_home,
        environ={},
    )
    service = ManagedServiceManager(
        platform=platform,
        registration_path=spotter_home / "service",
        layout=layout,
    )

    definition = service._definition()  # noqa: SLF001 - service artifact contract

    if platform == "darwin":
        parsed = plistlib.loads(definition)
        assert parsed["ProgramArguments"] == [str(daemon)]
        assert parsed["KeepAlive"] == {"PathState": {str(daemon): True}}
        assert parsed["WorkingDirectory"] == str(spotter_home)
        assert parsed["StandardOutPath"] == str(spotter_home / "logs/spotterd.log")
        assert parsed["EnvironmentVariables"] == {"SPOTTER_HOME": str(spotter_home)}
    else:
        text = definition.decode()
        assert f'ExecStart="{daemon}"' in text
        assert f'WorkingDirectory="{spotter_home}"' in text
        assert f'Environment="SPOTTER_HOME={spotter_home}"' in text
        assert f'StandardOutput="append:{spotter_home}/logs/spotterd.log"' in text
    assert "Cellar" not in definition.decode()


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
        return DaemonStatus(
            next(statuses, RuntimeHealth.HEALTHY),
            build_id=current_build_identity().build_id,
        )

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service, "status", healthy)

    assert asyncio.run(service.start()).health == RuntimeHealth.HEALTHY
    assert any(command[: len(expected)] == expected for command in commands)


def test_managed_service_reloads_an_unavailable_but_loaded_launchd_job(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    spotter_home, _ = homes
    service = ManagedServiceManager(
        platform="darwin",
        registration_path=spotter_home / "service.plist",
        executable="/stable/bin/spotterd",
    )
    commands: list[list[str]] = []
    statuses = iter(
        [
            DaemonStatus(RuntimeHealth.UNAVAILABLE),
            DaemonStatus(
                RuntimeHealth.HEALTHY,
                build_id=current_build_identity().build_id,
            ),
        ]
    )

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    async def status() -> DaemonStatus:
        return next(statuses, DaemonStatus(RuntimeHealth.HEALTHY))

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service, "status", status)

    assert asyncio.run(service.start()).health == RuntimeHealth.HEALTHY
    assert any(command[:2] == ["launchctl", "bootout"] for command in commands)
    assert any(command[:2] == ["launchctl", "bootstrap"] for command in commands)
    assert not any(command[:2] == ["launchctl", "kickstart"] for command in commands)


def test_managed_service_reports_and_recovers_from_a_failed_rebootstrap(
    homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    spotter_home, _ = homes
    service = ManagedServiceManager(
        platform="darwin",
        registration_path=spotter_home / "service.plist",
        executable="/stable/bin/spotterd",
    )
    statuses = iter(
        [
            DaemonStatus(RuntimeHealth.UNAVAILABLE),
            DaemonStatus(RuntimeHealth.UNAVAILABLE),
            DaemonStatus(
                RuntimeHealth.HEALTHY,
                build_id=current_build_identity().build_id,
            ),
        ]
    )
    booted_out = False
    bootstrap_attempts = 0

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal booted_out, bootstrap_attempts
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(command, 1 if booted_out else 0, "", "")
        if command[:2] == ["launchctl", "bootout"]:
            booted_out = True
        if command[:2] == ["launchctl", "bootstrap"]:
            bootstrap_attempts += 1
            if bootstrap_attempts == 1:
                return subprocess.CompletedProcess(command, 5, "", "bootstrap denied")
        return subprocess.CompletedProcess(command, 0, "", "")

    async def status() -> DaemonStatus:
        return next(statuses, DaemonStatus(RuntimeHealth.HEALTHY))

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service, "status", status)

    failed = asyncio.run(service.start())
    recovered = asyncio.run(service.start())

    assert failed.health == RuntimeHealth.UNAVAILABLE
    assert failed.detail == "bootstrap denied"
    assert recovered.health == RuntimeHealth.HEALTHY
    assert bootstrap_attempts == 2


def test_managed_service_commands_have_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["launchctl", "print"], 10)

    monkeypatch.setattr(subprocess, "run", timeout)

    result = ManagedServiceManager._run(["launchctl", "print"])  # noqa: SLF001

    assert result.returncode == 124
    assert "timed out" in result.stderr


@pytest.mark.parametrize(
    "platform,restart",
    [
        ("darwin", ["launchctl", "kickstart", "-k"]),
        ("linux", ["systemctl", "--user", "restart"]),
    ],
)
def test_managed_service_restarts_an_old_build_behind_the_same_stable_link(
    homes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    restart: list[str],
) -> None:
    spotter_home, _ = homes
    service = ManagedServiceManager(
        platform=platform,
        registration_path=spotter_home / "service",
        executable="/stable/opt/spotter/bin/spotterd",
    )
    service._install_definition()  # noqa: SLF001 - keep the stable path unchanged
    commands: list[list[str]] = []
    statuses = iter(
        [
            DaemonStatus(RuntimeHealth.HEALTHY, build_id="retired-build"),
            DaemonStatus(RuntimeHealth.HEALTHY, build_id=current_build_identity().build_id),
        ]
    )

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    async def status() -> DaemonStatus:
        return next(statuses, DaemonStatus(RuntimeHealth.HEALTHY))

    monkeypatch.setattr(service, "_run", run)
    monkeypatch.setattr(service, "status", status)

    assert asyncio.run(service.start()).health == RuntimeHealth.HEALTHY
    assert any(command[: len(restart)] == restart for command in commands)


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
