import sys
from pathlib import Path

import pytest

from spotter.cli import _update_main, main
from spotter.paths import (
    InstallationMethod,
    InstallationProvenance,
    _classify_installation,
)


@pytest.mark.parametrize(
    "executable,prefix,direct_url,distribution_present,expected",
    [
        (
            Path("/opt/homebrew/bin/spotter"),
            Path("/opt/homebrew/Cellar/spotter/1.0/libexec"),
            None,
            True,
            InstallationMethod.HOMEBREW,
        ),
        (
            Path("/home/me/.local/bin/spotter"),
            Path("/home/me/.local/pipx/venvs/spotter-agent"),
            None,
            True,
            InstallationMethod.PIPX,
        ),
        (
            Path("/home/me/.local/bin/spotter"),
            Path("/home/me/.local/share/uv/tools/spotter-agent"),
            None,
            True,
            InstallationMethod.UV_TOOL,
        ),
        (
            None,
            Path("/workspace/.venv"),
            {"dir_info": {"editable": True}},
            True,
            InstallationMethod.EDITABLE,
        ),
        (
            None,
            Path("/workspace/.venv"),
            None,
            True,
            InstallationMethod.PIP,
        ),
        (
            None,
            Path("/usr"),
            None,
            False,
            InstallationMethod.SOURCE,
        ),
    ],
)
def test_installation_classification_uses_package_boundary_evidence(
    executable: Path | None,
    prefix: Path,
    direct_url: dict[str, object] | None,
    distribution_present: bool,
    expected: InstallationMethod,
) -> None:
    assert (
        _classify_installation(
            executable,
            prefix,
            direct_url,
            distribution_present=distribution_present,
        ).method
        == expected
    )


@pytest.mark.parametrize(
    "method,expected",
    [
        (InstallationMethod.HOMEBREW, "brew upgrade spotter-agent/spotter/spotter"),
        (InstallationMethod.PIPX, "pipx upgrade spotter-agent"),
        (InstallationMethod.UV_TOOL, "uv tool upgrade spotter-agent"),
        (InstallationMethod.PIP, f"{sys.executable} -m pip install --upgrade spotter-agent"),
    ],
)
def test_update_advises_the_detected_package_owner_without_running_it(
    method: InstallationMethod,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _update_main(InstallationProvenance(method, "test evidence")) == 0

    output = capsys.readouterr().out
    assert f"installation: {method.value}" in output
    assert expected in output
    assert "spotter setup codex && spotter doctor" in output


@pytest.mark.parametrize("method", [InstallationMethod.EDITABLE, InstallationMethod.SOURCE])
def test_update_refuses_to_self_replace_development_installations(
    method: InstallationMethod, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _update_main(InstallationProvenance(method, "test evidence")) == 0

    output = capsys.readouterr().out
    assert "self-update: unsupported" in output
    assert "upgrade with package owner" not in output


def test_update_command_routes_to_detected_package_owner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "spotter.cli.installation_provenance",
        lambda: InstallationProvenance(InstallationMethod.HOMEBREW, "test evidence"),
    )

    assert main(["update"]) == 0
    assert "brew upgrade spotter-agent/spotter/spotter" in capsys.readouterr().out


def test_update_rejects_a_target() -> None:
    with pytest.raises(SystemExit) as error:
        main(["update", "status"])

    assert error.value.code == 2
