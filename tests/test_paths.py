import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from spotter.paths import RuntimeLayout, RuntimeLayoutError
from spotter.snapshot import StepJournal


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_layout_separates_ownership_without_creating_mutable_state(tmp_path: Path) -> None:
    root = tmp_path / "state"
    assets = tmp_path / "package" / "spotter"

    layout = RuntimeLayout.discover(
        cli_executable="/stable/bin/spotter",
        package_assets_dir=assets,
        spotter_root=root,
        user_home=tmp_path / "user",
        environ={},
    )

    assert layout.cli_executable == Path("/stable/bin/spotter")
    assert layout.daemon_executable == Path("/stable/bin/spotterd")
    assert layout.bridge_command == ("/stable/bin/spotter", "hook")
    assert layout.package_assets_dir == assets.absolute()
    assert layout.user_config_dir == root
    assert layout.user_data_dir == root
    assert layout.integration_dir == root / "integrations"
    assert layout.runtime_dir == root / "runtime"
    assert layout.log_dir == root / "logs"
    assert layout.repository_registry == root / "repos.json"
    assert "package_assets_dir" not in layout.integration_record()
    assert not root.exists(), "layout discovery must not mutate user state"


def test_source_fallback_is_not_accepted_as_a_persistent_package_bridge() -> None:
    layout = RuntimeLayout.discover(argv0="python", environ={"PATH": ""})

    assert layout.cli_executable is None
    assert layout.daemon_executable is None
    assert layout.bridge_command[-2:] == ("spotter", "hook")
    with pytest.raises(RuntimeLayoutError, match="stable executable"):
        layout.validate_persistent()


def test_module_invocation_does_not_adopt_an_older_path_install(tmp_path: Path) -> None:
    old = _executable(tmp_path / "old/bin/spotter")
    _executable(old.with_name("spotterd"))

    layout = RuntimeLayout.discover(
        argv0="/current/package/spotter/__main__.py",
        environ={"PATH": str(old.parent)},
    )

    assert layout.cli_executable is None
    assert layout.daemon_executable is None
    assert layout.cli_command[-2:] == ("-m", "spotter")


@pytest.mark.parametrize(
    "prefix",
    [
        "/opt/homebrew",
        "/usr/local",
        "/home/linuxbrew/.linuxbrew",
    ],
)
def test_homebrew_adapters_can_supply_stable_opt_entry_points(prefix: str) -> None:
    cli = Path(prefix) / "opt/spotter/bin/spotter"

    layout = RuntimeLayout.discover(cli_executable=cli, environ={})

    layout.validate_persistent()
    assert layout.daemon_executable == cli.with_name("spotterd")
    assert layout.integration_record()["cli_executable"] == str(cli)


def test_versioned_cellar_paths_are_rejected_for_persistent_state() -> None:
    layout = RuntimeLayout.discover(
        cli_executable="/prefix/Cellar/spotter/1.2.3/bin/spotter", environ={}
    )

    with pytest.raises(RuntimeLayoutError, match="versioned Cellar"):
        layout.validate_persistent()


def test_explicit_package_entry_point_wins_over_an_older_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _executable(tmp_path / "old/bin/spotter")
    _executable(old.with_name("spotterd"))
    current = _executable(tmp_path / "current/bin/spotter")
    current_daemon = _executable(current.with_name("spotterd"))
    monkeypatch.setenv("PATH", str(old.parent))

    layout = RuntimeLayout.discover(cli_executable=current)

    assert layout.cli_executable == current
    assert layout.daemon_executable == current_daemon


def test_explicit_invocation_wins_over_path_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _executable(tmp_path / "old/bin/spotter")
    invoked = _executable(tmp_path / "new/bin/spotter")
    monkeypatch.setenv("PATH", str(old.parent))

    layout = RuntimeLayout.discover(argv0=str(invoked))

    assert layout.cli_executable == invoked
    assert layout.daemon_executable == invoked.with_name("spotterd")


def test_managed_daemon_invocation_preserves_the_stable_package_boundary(
    tmp_path: Path,
) -> None:
    cli = _executable(tmp_path / "stable/opt/spotter/bin/spotter")
    daemon = _executable(cli.with_name("spotterd"))

    layout = RuntimeLayout.discover(argv0=str(daemon), environ={"PATH": ""})

    assert layout.daemon_executable == daemon
    assert layout.cli_executable == cli
    assert layout.daemon_command == (str(daemon),)


def test_long_state_root_uses_a_short_private_runtime_address(tmp_path: Path) -> None:
    root = tmp_path / ("long" * 40)
    layout = RuntimeLayout.discover(spotter_root=root, environ={})

    assert layout.control_socket.parent.parent == Path("/tmp")
    assert layout.control_socket.name == "spotterd.sock"
    assert str(os.getuid()) in layout.control_socket.parent.name


def test_installed_entry_points_run_outside_a_source_checkout(tmp_path: Path) -> None:
    cli = Path(sys.executable).with_name("spotter")
    daemon = cli.with_name("spotterd")
    assert os.access(cli, os.X_OK) and os.access(daemon, os.X_OK)
    state = tmp_path / "state"
    environment = {
        **os.environ,
        "PATH": "/usr/bin:/bin",
        "SPOTTER_HOME": str(state),
    }
    environment.pop("PYTHONPATH", None)

    started = subprocess.run(
        [str(cli), "daemon", "start"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    try:
        hook = subprocess.run(
            [str(cli), "hook"],
            cwd=tmp_path,
            input=json.dumps(
                {"hook_event_name": "SessionStart", "session_id": "installed-entry-point"}
            ),
            capture_output=True,
            text=True,
            env=environment,
            timeout=3,
            check=False,
        )
        identity = subprocess.run(
            [str(daemon), "--version"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=environment,
            timeout=3,
            check=False,
        )
    finally:
        subprocess.run(
            [str(cli), "daemon", "stop"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
            check=False,
        )

    assert hook.returncode == 0
    assert StepJournal.load(state / "sessions/installed-entry-point.jsonl")
    assert identity.returncode == 0
    assert "build" in identity.stdout
