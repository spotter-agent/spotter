from pathlib import Path

import pytest

from spotter.cli import main
from spotter.config import ConfigurationError, resolve_config
from spotter.modes import save_mode, selected_mode
from spotter.paths import RuntimeLayout


@pytest.fixture()
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeLayout:
    root = tmp_path / "spotter"
    monkeypatch.setenv("SPOTTER_HOME", str(root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    return RuntimeLayout.discover(spotter_root=root, environ={})


def test_mode_presets_override_only_activation_settings(
    layout: RuntimeLayout, tmp_path: Path
) -> None:
    global_config = layout.user_config_dir / "spotter.toml"
    global_config.parent.mkdir(parents=True)
    global_config.write_text(
        '[main_agent]\nadapter = "codex"\n[reviewer]\nmodel = "chosen-model"\n'
        "max_per_session = 3\nmax_per_day = 7\n"
    )

    save_mode("advisory", layout=layout)
    config = resolve_config(layout=layout, repository=tmp_path).config

    assert config.observation_only is False
    assert config.reviewer.on_signals is True
    assert config.reviewer.deliver_on_signals is True
    assert config.reviewer.model == "chosen-model"
    assert (config.reviewer.max_per_session, config.reviewer.max_per_day) == (3, 7)
    assert selected_mode(layout.user_config_dir / "mode.toml") == "advisory"


def test_custom_removes_only_managed_mode_file(layout: RuntimeLayout) -> None:
    config = layout.user_config_dir / "spotter.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[main_agent]\nadapter = "codex"\n')
    original = config.read_bytes()
    save_mode("protect", layout=layout)

    save_mode("custom", layout=layout)

    assert not (layout.user_config_dir / "mode.toml").exists()
    assert config.read_bytes() == original


def test_edited_or_symlinked_mode_file_is_preserved(layout: RuntimeLayout, tmp_path: Path) -> None:
    path = layout.user_config_dir / "mode.toml"
    path.parent.mkdir(parents=True)
    path.write_text("observation_only = false\n")
    with pytest.raises(ConfigurationError, match="preserved"):
        save_mode("observe", layout=layout)
    with pytest.raises(ConfigurationError, match="preserved"):
        resolve_config(layout=layout)
    assert path.read_text() == "observation_only = false\n"

    path.unlink()
    target = tmp_path / "target.toml"
    target.write_text("keep")
    path.symlink_to(target)
    with pytest.raises(ConfigurationError, match="symlink"):
        save_mode("observe", layout=layout)
    with pytest.raises(ConfigurationError, match="symlink"):
        resolve_config(layout=layout)
    assert target.read_text() == "keep"


def test_mode_cli_is_noninteractive_in_pipes_and_dry_run_is_read_only(
    layout: RuntimeLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["mode"]) == 0
    assert "Choose with:" in capsys.readouterr().out
    assert main(["mode", "protect", "--dry-run"]) == 0
    assert not (layout.user_config_dir / "mode.toml").exists()


def test_mode_cli_selects_and_clears_preset(
    layout: RuntimeLayout, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["mode", "protect"]) == 0
    assert selected_mode(layout.user_config_dir / "mode.toml") == "protect"
    assert "other configuration is preserved" in capsys.readouterr().out

    assert main(["mode", "custom"]) == 0
    assert not (layout.user_config_dir / "mode.toml").exists()
