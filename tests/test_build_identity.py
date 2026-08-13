import pytest

from spotter.build_identity import current_build_identity, version_line
from spotter.cli import main as cli_main
from spotter.daemon import main as daemon_main
from spotter.protocol import CONTROL_PROTOCOL_VERSION


def test_component_identity_separates_build_from_protocol() -> None:
    identity = current_build_identity()

    peer = identity.peer_metadata("hook_bridge")

    assert peer["component"] == "hook_bridge"
    assert peer["spotter_version"] == identity.version
    assert peer["build_id"] == identity.build_id
    assert peer["ipc_protocol_version"] == CONTROL_PROTOCOL_VERSION


def test_version_line_reports_packaged_identity_and_protocol() -> None:
    identity = current_build_identity()

    assert version_line("spotter") == (
        f"spotter {identity.version} (build {identity.build_id}; ipc {CONTROL_PROTOCOL_VERSION})"
    )


@pytest.mark.parametrize(
    "executable",
    ["spotter", "spotterd"],
)
def test_entrypoints_expose_version(executable: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        if executable == "spotter":
            cli_main(["--version"])
        else:
            daemon_main(["--version"])

    assert stopped.value.code == 0
    assert capsys.readouterr().out == version_line(executable) + "\n"
