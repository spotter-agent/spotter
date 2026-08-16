import asyncio
import json
import shutil
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from spotter.build_identity import current_build_identity
from spotter.cli import main
from spotter.config import GatesConfig
from spotter.daemon import (
    GATE_TIMEOUT,
    PROTOCOL_VERSION,
    DaemonAlreadyRunning,
    DaemonClient,
    DaemonProtocolError,
    DaemonServer,
    DaemonTimeout,
    RuntimeHealth,
    _configured_mcp_semantics,
    _configured_reviewer,
    _configured_snapshot_on_patch,
    _PackageBoundaryMonitor,
    _resolved_daemon_config,
    runtime_socket,
)
from spotter.gates import Gate
from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.paths import RuntimeLayout
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent


@pytest.fixture()
def socket_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))
    path = runtime_socket()
    path.parent.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path.parent, ignore_errors=True)


def test_control_socket_handles_concurrent_clients_and_health_states(socket_path: Path) -> None:
    async def scenario() -> None:
        server = DaemonServer(socket_path, config_generation="cfg-test")
        await server.start()
        try:
            statuses = await asyncio.gather(
                *(DaemonClient(socket_path).status() for _ in range(20))
            )
            assert {status.health for status in statuses} == {RuntimeHealth.HEALTHY}
            assert len({status.pid for status in statuses}) == 1
            assert {status.version for status in statuses} == {current_build_identity().version}
            assert {status.build_id for status in statuses} == {current_build_identity().build_id}
            assert {status.config_generation for status in statuses} == {"cfg-test"}
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


def test_package_boundary_monitor_requires_continuous_absence() -> None:
    monitor = _PackageBoundaryMonitor(missing_grace=2.0)

    assert not monitor.observe(available=False, now=10.0)
    assert not monitor.observe(available=False, now=11.9)
    assert not monitor.observe(available=True, now=12.0), "an upgrade relink resets the fence"
    assert not monitor.observe(available=False, now=20.0)
    assert monitor.observe(available=False, now=22.0)


def test_daemon_loads_signal_review_opt_in_from_manifest_config(tmp_path: Path) -> None:
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home")
    config = tmp_path / "custom.toml"
    config.write_text(
        '[main_agent]\nadapter = "codex"\n[reviewer]\nmodel = "review-model"\non_signals = true\n'
    )
    layout.integration_manifest.parent.mkdir(parents=True)
    layout.integration_manifest.write_text(json.dumps({"config_path": str(config)}))

    reviewer = _configured_reviewer(layout)

    assert reviewer.model == "review-model"
    assert reviewer.on_signals is True


def test_daemon_resolves_manifest_config_generation(tmp_path: Path) -> None:
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home")
    config = tmp_path / "custom.toml"
    config.write_text('[main_agent]\nadapter = "codex"\n[reviewer]\nmodel = "review-model"\n')
    layout.integration_manifest.parent.mkdir(parents=True)
    layout.integration_manifest.write_text(json.dumps({"config_path": str(config)}))

    resolved = _resolved_daemon_config(layout)

    assert resolved.config.reviewer.model == "review-model"
    assert resolved.resolved_config_generation


def test_daemon_loads_mcp_semantics_from_manifest_config(tmp_path: Path) -> None:
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home")
    config = tmp_path / "custom.toml"
    config.write_text(
        '[main_agent]\nadapter = "codex"\n'
        '[mcp_semantics."inventory"."lookup"]\n'
        'operation = "read"\nreversibility = "A"\nresource_fields = ["item_id"]\n'
    )
    layout.integration_manifest.parent.mkdir(parents=True)
    layout.integration_manifest.write_text(json.dumps({"config_path": str(config)}))

    semantics = _configured_mcp_semantics(layout)

    assert len(semantics) == 1
    assert (semantics[0].server, semantics[0].tool, semantics[0].reversibility) == (
        "inventory",
        "lookup",
        "A",
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_daemon_loads_snapshot_setting_from_manifest_config(tmp_path: Path, enabled: bool) -> None:
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home")
    config = tmp_path / "custom.toml"
    config.write_text(
        f'snapshot_on_patch = {str(enabled).lower()}\n[main_agent]\nadapter = "codex"\n'
    )
    layout.integration_manifest.parent.mkdir(parents=True)
    layout.integration_manifest.write_text(json.dumps({"config_path": str(config)}))

    assert _configured_snapshot_on_patch(layout) is enabled


def test_daemon_stops_cleanly_after_the_stable_package_is_removed(
    socket_path: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        package_bin = tmp_path / "package/bin"
        package_bin.mkdir(parents=True)
        cli = package_bin / "spotter"
        daemon = package_bin / "spotterd"
        for executable in (cli, daemon):
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o755)
        retained = tmp_path / "state/sessions/retained.jsonl"
        retained.parent.mkdir(parents=True)
        retained.write_text("durable\n")
        layout = RuntimeLayout.discover(
            cli_executable=cli,
            daemon_executable=daemon,
            spotter_root=tmp_path / "state",
            environ={},
        )
        server = DaemonServer(
            socket_path,
            layout=layout,
            package_watch_interval=0.01,
            package_missing_grace=0,
        )
        serving = asyncio.create_task(server.serve())
        for _ in range(50):
            if (await DaemonClient(socket_path).status()).available:
                break
            await asyncio.sleep(0.01)
        daemon.unlink()

        await asyncio.wait_for(serving, 1)

        assert not socket_path.exists()
        assert retained.read_text() == "durable\n"

    asyncio.run(scenario())


def test_daemon_owns_incremental_thread_state_and_conservative_hydration(
    socket_path: Path,
) -> None:
    identity = RuntimeIdentity(
        ThreadId("thread-1"),
        TurnId("turn-1"),
        None,
        IdentityProvenance("codex", "external-thread", "external-turn"),
    )
    events = [
        TraceEvent("thread_started", event_id="thread", identity=identity),
        TraceEvent("turn_started", event_id="turn", identity=identity),
        TraceEvent("runtime_reconciled", event_id="ready", identity=identity),
    ]
    server = DaemonServer(socket_path)

    for event in events:
        server.observe_trace(event)
    assert identity.thread_id is not None
    live = server.thread_state(identity.thread_id)

    recovered = DaemonServer(socket_path)
    hydrated = recovered.hydrate_thread_state(
        [StepRecord(index, event, None) for index, event in enumerate(events)]
    )[0]

    assert live.version == 3
    assert live.active_turn_id == TurnId("turn-1")
    assert live.control_ready is True
    assert hydrated.version == live.version
    assert hydrated.active_turn_id is None
    assert hydrated.control_ready is False


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


def test_gate_roundtrip_preserves_policy_and_concurrency(socket_path: Path) -> None:
    async def scenario() -> None:
        server = DaemonServer(socket_path)
        await server.start()
        try:
            cases = [
                (TraceEvent("tool_proposal", {"command": "pytest", "files": []}), GatesConfig()),
                (
                    TraceEvent("tool_proposal", {"command": "rm -rf /", "files": []}),
                    GatesConfig(),
                ),
                (
                    TraceEvent("tool_proposal", {"command": None, "files": ["pyproject.toml"]}),
                    GatesConfig(block_dependency_changes=True),
                ),
                (
                    TraceEvent("tool_proposal", {"command": None, "files": ["secrets/key"]}),
                    GatesConfig(forbidden_paths=("secrets/*",)),
                ),
            ]
            results = await asyncio.gather(
                *(DaemonClient(socket_path).gate(event, gates, "/repo") for event, gates in cases)
            )
            for (event, gates), (decision, evaluation_ms, _sample) in zip(
                cases, results, strict=True
            ):
                assert decision == Gate(
                    gates.forbidden_paths, gates.block_dependency_changes, "/repo"
                ).check(event)
                assert evaluation_ms >= 0
            assert sum(sample is not None for _, _, sample in results) == 1
        finally:
            await server.close()

    asyncio.run(scenario())


def test_gate_request_carries_hook_identity_for_live_signal_correlation(
    socket_path: Path,
) -> None:
    class Recovery:
        def __init__(self) -> None:
            self.observations: list[tuple[dict[str, object], dict[str, object]]] = []

        async def start(self) -> None:
            pass

        async def close(self) -> None:
            pass

        def record_gate_decision(
            self, params: dict[str, object], decision: dict[str, object]
        ) -> None:
            self.observations.append((params, decision))

    async def scenario() -> None:
        recovery = Recovery()
        server = DaemonServer(socket_path)
        server.recovery = recovery
        await server.start()
        event = TraceEvent(
            "tool_proposal",
            {
                "command": "git reset --hard",
                "files": [],
                "tool": "Bash",
                "tool_use_id": "call-1",
                "turn_id": "turn-1",
                "resource": "workspace",
            },
            identity=RuntimeIdentity.legacy_hook("codex", "thread-1"),
        )
        try:
            await DaemonClient(socket_path).gate(event, GatesConfig(), "/repo")
            async with asyncio.timeout(1):
                while not recovery.observations:
                    await asyncio.sleep(0)
        finally:
            await server.close()

        params, decision = recovery.observations[0]
        assert decision["rule"] == "git_reset_hard"
        assert params["identity"] == {"thread_id": "thread-1", "turn_id": "turn-1"}
        assert params["proposal"] == {
            "command": "git reset --hard",
            "files": [],
            "tool": "Bash",
            "tool_use_id": "call-1",
            "resource": "workspace",
        }

    asyncio.run(scenario())


def test_resource_sampling_is_bounded_and_runtime_addressable(socket_path: Path) -> None:
    server = DaemonServer(socket_path)

    samples = [server._maybe_sample_resources() for _ in range(65)]
    observed = [sample for sample in samples if sample is not None]

    assert len(observed) == 2
    assert [sample["sample_seq"] for sample in observed] == [1, 2]
    assert len({sample["runtime_id"] for sample in observed}) == 1
    first_cpu, last_cpu = observed[0]["cpu_seconds"], observed[-1]["cpu_seconds"]
    assert isinstance(first_cpu, int | float) and isinstance(last_cpu, int | float)
    assert last_cpu >= first_cpu


@pytest.mark.parametrize(
    "response",
    [
        b"not-json\n",
        b'{"protocol":999,"ok":true,"decision":{},"evaluation_ms":0}\n',
    ],
)
def test_gate_rejects_malformed_and_mismatched_responses(
    socket_path: Path, response: bytes
) -> None:
    async def scenario() -> None:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(response)
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handler, path=socket_path)
        async with server:
            with pytest.raises(DaemonProtocolError):
                await DaemonClient(socket_path).gate(
                    TraceEvent("tool_proposal", {"command": "true", "files": []}),
                    GatesConfig(),
                    "/repo",
                )

    asyncio.run(scenario())


def test_gate_timeout_is_bounded_across_the_request(socket_path: Path) -> None:
    async def scenario() -> None:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            await asyncio.sleep(GATE_TIMEOUT * 2)
            writer.close()

        server = await asyncio.start_unix_server(handler, path=socket_path)
        async with server:
            started = time.perf_counter()
            with pytest.raises(DaemonTimeout):
                await DaemonClient(socket_path, timeout=0.01).gate(
                    TraceEvent("tool_proposal", {"command": "true", "files": []}),
                    GatesConfig(),
                    "/repo",
                )
            assert time.perf_counter() - started < 0.1

    asyncio.run(scenario())


def test_gate_fails_open_for_an_unsupported_proposal_shape(socket_path: Path) -> None:
    async def scenario() -> None:
        server = DaemonServer(socket_path)
        await server.start()
        try:
            decision, _, _ = await DaemonClient(socket_path).gate(
                TraceEvent("tool_proposal", {"command": 42, "files": "not-a-list"}),
                GatesConfig(),
                "/repo",
            )
            assert decision.allowed
            assert decision.rule == "unsupported_proposal"
        finally:
            await server.close()

    asyncio.run(scenario())


def test_gate_normalizes_missing_files_without_skipping_the_command(socket_path: Path) -> None:
    async def scenario() -> None:
        server = DaemonServer(socket_path)
        await server.start()
        try:
            decision, _, _ = await DaemonClient(socket_path).gate(
                TraceEvent("tool_proposal", {"command": "rm -rf /", "files": None}),
                GatesConfig(),
                "/repo",
            )
            assert not decision.allowed
            assert decision.rule == "rm_root"
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


def test_daemon_reclaims_a_stale_socket_without_a_live_process(
    socket_path: Path,
) -> None:
    async def scenario() -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(socket_path))
        stale.close()
        assert (await DaemonClient(socket_path).status()).health == RuntimeHealth.UNAVAILABLE

        server = DaemonServer(socket_path)
        await server.start()
        try:
            assert (await DaemonClient(socket_path).status()).health == RuntimeHealth.HEALTHY
        finally:
            await server.close()

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
