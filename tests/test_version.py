"""Release identity is exposed by every packaged executable."""

import re

import pytest

from spotter.build import package_version
from spotter.cli import main as cli_main
from spotter.daemon import main as daemon_main


@pytest.mark.parametrize(
    ("entrypoint", "program"),
    [(cli_main, "spotter"), (daemon_main, "spotterd")],
)
def test_version_banner(
    entrypoint: object, program: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit, match="0"):
        entrypoint(["--version"])  # type: ignore[operator]

    assert capsys.readouterr().out == f"{program} {package_version()}\n"
    assert re.fullmatch(r"[0-9]+(?:\.[0-9]+)+(?:[^\s]*)?", package_version())
