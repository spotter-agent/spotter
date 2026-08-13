import asyncio
import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.daemon import (
    PROTOCOL_VERSION,
    DaemonAlreadyRunning,
    DaemonClient,
    DaemonServer,
    RuntimeHealth,
    runtime_socket,
)


@pytest.fixture()
def socket_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    path = runtime_socket()
    yield path
    shutil.rmtree(path.parent, ignore_errors=True)


def test_control_socket_handles_concurrent_clients_and_health_states(socket_path: Path) -> None:
    async def scenario() -> None:
        server = DaemonServer(socket_path)
        await server.start()
        try:
            statuses = await asyncio.gather(
                *(DaemonClient(socket_path).status() for _ in range(20))
            )
            assert {status.health for status in statuses} == {RuntimeHealth.HEALTHY}
            assert len({status.pid for status in statuses}) == 1
            assert socket_path.stat().st_mode & 0o777 == 0o600

            server.set_health(RuntimeHealth.DEGRADED)
            assert (await DaemonClient(socket_path).status()).health == RuntimeHealth.DEGRADED
            server.set_health(RuntimeHealth.RECOVERING)
            assert (await DaemonClient(socket_path).status()).health == RuntimeHealth.RECOVERING

            await DaemonClient(socket_path).shutdown()
            await asyncio.wait_for(server.wait_for_shutdown(), 1)
        finally:
            await server.close()
        assert not socket_path.exists()

    asyncio.run(scenario())


def test_bad_protocol_does_not_break_later_clients(socket_path: Path) -> None:
    async def scenario() -> None:
        server = DaemonServer(socket_path)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.write(b'{"protocol":999,"method":"ping"}\n')
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()

            assert response == {
                "protocol": PROTOCOL_VERSION,
                "ok": False,
                "error": "incompatible control protocol",
            }
            assert (await DaemonClient(socket_path).status()).health == RuntimeHealth.HEALTHY
        finally:
            await server.close()

    asyncio.run(scenario())


def test_second_daemon_cannot_take_the_live_socket(socket_path: Path) -> None:
    async def scenario() -> None:
        first = DaemonServer(socket_path)
        second = DaemonServer(socket_path)
        await first.start()
        try:
            with pytest.raises(DaemonAlreadyRunning):
                await second.start()
            assert (await DaemonClient(socket_path).status()).health == RuntimeHealth.HEALTHY
        finally:
            await second.close()
            await first.close()

    asyncio.run(scenario())


def test_missing_daemon_is_explicitly_unavailable(socket_path: Path) -> None:
    status = asyncio.run(DaemonClient(socket_path).status())

    assert status.health == RuntimeHealth.UNAVAILABLE
    assert status.pid is None
    assert status.detail


def test_manual_cli_lifecycle(socket_path: Path) -> None:
    try:
        assert main(["daemon", "status"]) == 1
        assert main(["daemon", "start"]) == 0
        assert main(["daemon", "start"]) == 0
        assert runtime_socket().exists()
        assert main(["daemon", "status"]) == 0
        assert main(["daemon", "restart"]) == 0
        assert main(["daemon", "stop"]) == 0
        assert main(["daemon", "status"]) == 1
    finally:
        main(["daemon", "stop"])
