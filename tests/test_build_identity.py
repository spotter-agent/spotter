import json
import subprocess
import sys
from typing import Any

from spotter.build import build_identity, version_string
from spotter.daemon import main as daemon_main


def test_build_identity_keeps_contract_versions_explicit() -> None:
    identity = build_identity().as_dict()

    assert identity["spotter_version"]
    assert identity["ipc_protocol_version"] == 1
    assert identity["journal_schema_version"] == 1
    # It is deliberately suitable for handshakes and diagnostic serialization.
    json.dumps(identity)


def test_cli_reports_packaged_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "spotter", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == version_string("spotter")


def test_daemon_reports_the_same_packaged_version(capsys: Any) -> None:
    try:
        daemon_main(["--version"])
    except SystemExit as error:
        assert error.code == 0
    output = capsys.readouterr()
    assert output.out.strip() == version_string("spotterd")
