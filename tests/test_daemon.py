import asyncio
import json
import shutil
import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from spotter.app_server import AppServerCapabilities, CapabilityStatus
from spotter.build_identity import current_build_identity
from spotter.cli import main
from spotter.config import (
    ActivationBoundary,
    ConfigChange,
    ConfigReloadResult,
    ConfigSnapshotStore,
    GatesConfig,
    ReloadDisposition,
    ResolvedConfig,
    resolve_config,
)
from spotter.daemon import (
    GATE_TIMEOUT,
    PROTOCOL_VERSION,
    DaemonAlreadyRunning,
    DaemonClient,
    DaemonProtocolError,
    DaemonProtocolMismatch,
    DaemonServer,
    DaemonTimeout,
    RuntimeCompatibility,
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
from spotter.runtime_connection import AppServerRecoveryLoop
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
            assert {status.compatibility for status in statuses} == {RuntimeCompatibility.MATCHED}
            assert len({status.runtime_generation for status in statuses}) == 1
            assert len({status.construction_fingerprint for status in statuses}) == 1
            assert all(status.construction_fingerprint for status in statuses)
            assert all(status.started_at is not None for status in statuses)
            assert all(status.capabilities is not None for status in statuses)
            assert socket_path.stat().st_mode & 0o777 == 0o600

            server.recovery = cast(
                Any,
                SimpleNamespace(
                    state=SimpleNamespace(value="ready"),
                    connection=SimpleNamespace(
                        connection_epoch=7,
                        server_changed=True,
                        capabilities_changed=True,
                        capabilities=AppServerCapabilities(
                            observation=CapabilityStatus.AVAILABLE,
                            thread_query=CapabilityStatus.AVAILABLE,
                            steer=CapabilityStatus.UNAVAILABLE,
                            interrupt=CapabilityStatus.UNKNOWN,
                            atomic_pre_tool_veto=CapabilityStatus.UNAVAILABLE,
                        ),
                    ),
                ),
            )
            app_status = await DaemonClient(socket_path).status()
            assert app_status.app_server_state == "ready"
            assert app_status.app_server_connection_epoch == 7
            assert dict(app_status.app_server_capabilities or ()) == {
                "atomic_pre_tool_veto": "unavailable",
                "interrupt": "unknown",
                "observation": "available",
                "steer": "unavailable",
                "thread_query": "available",
            }
            assert app_status.app_server_server_changed is True
            assert app_status.app_server_capabilities_changed is True
            server.recovery = None

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


def test_daemon_reload_applies_hot_config_and_stages_next_turn(
    socket_path: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "runtime.toml"
    config_path.write_text(
        'snapshot_on_patch = true\n[main_agent]\nadapter = "codex"\n'
        "[reviewer]\non_signals = true\nmax_per_session = 2\n"
    )
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home")

    def load() -> ResolvedConfig:
        return resolve_config(layout=layout, explicit_path=config_path)

    initial = load()

    async def scenario() -> None:
        server = DaemonServer(
            socket_path,
            config_store=ConfigSnapshotStore(initial),
            config_loader=load,
        )
        runtime = AppServerRecoveryLoop("ws://unused", tmp_path / "sessions", server.thread_states)
        server.recovery = runtime
        await server.start()
        client = DaemonClient(socket_path)
        try:
            config_path.write_text(
                'snapshot_on_patch = false\n[main_agent]\nadapter = "codex"\n'
                "[reviewer]\non_signals = true\nmax_per_session = 7\n"
            )
            applied = await client.reload_config()

            assert applied.disposition == ReloadDisposition.APPLIED
            assert server.reviewer_config.max_per_session == 7
            assert server.snapshot_on_patch is False
            assert server.config_generation == applied.active_generation

            config_path.write_text(
                'snapshot_on_patch = false\n[main_agent]\nadapter = "codex"\n'
                '[reviewer]\nmodel = "next-model"\non_signals = true\n'
                "max_per_session = 7\n"
                '[mcp_semantics."inventory"."lookup"]\n'
                'operation = "read"\nreversibility = "A"\n'
            )
            staged = await client.reload_config()
            status = await client.status()

            assert staged.disposition == ReloadDisposition.STAGED_NEXT_TURN
            assert status.config_generation == applied.active_generation
            assert status.pending_config_generation == staged.candidate_generation
            assert server.reviewer_config.model != "next-model"

            config_path.write_text("[broken")
            rejected = await client.reload_config()
            status = await client.status()

            assert rejected.disposition == ReloadDisposition.REJECTED_INVALID
            assert status.config_generation == applied.active_generation
            assert status.pending_config_generation == staged.candidate_generation
            assert status.config_reload_error

            identity = RuntimeIdentity(
                ThreadId("thread-config"),
                TurnId("turn-old"),
                None,
                IdentityProvenance("codex", "thread-config", "turn-old"),
            )
            server.observe_trace(
                TraceEvent("turn_started", event_id="turn-old-start", identity=identity)
            )
            assert server.activate_pending_config_at_turn_boundary() is None
            assert server.config_generation == applied.active_generation

            server.observe_trace(
                TraceEvent("turn_completed", event_id="turn-old-done", identity=identity)
            )
            activated = server.activate_pending_config_at_turn_boundary()
            status = await client.status()

            assert activated is not None
            assert activated.disposition == ReloadDisposition.APPLIED
            assert status.config_generation == staged.candidate_generation
            assert status.pending_config_generation is None
            assert status.config_reload_error is None
            assert server.reviewer_config.model == "next-model"
            assert [
                (rule.server, rule.tool, rule.reversibility)
                for rule in runtime.ingestor.normalizer.mcp_semantics
            ] == [("inventory", "lookup", "A")]
        finally:
            await server.close()

    asyncio.run(scenario())


def test_daemon_automatically_reloads_config_file_changes(
    socket_path: Path, tmp_path: Path
) -> None:
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home")
    config_path = layout.user_config_dir / "spotter.toml"
    initial = resolve_config(layout=layout)

    async def wait_until(predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.01)

    async def scenario() -> None:
        server = DaemonServer(
            socket_path,
            config_store=ConfigSnapshotStore(initial),
            config_loader=lambda: resolve_config(layout=layout),
            config_watch_interval=0.01,
        )
        await server.start()
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("[reviewer]\nmax_per_day = 7\n")
            await wait_until(lambda: server.reviewer_config.max_per_day == 7)
            applied_generation = server.config_generation

            config_path.write_text("[reviewer\n")
            await wait_until(
                lambda: (
                    server.config_store is not None and server.config_store.last_error() is not None
                )
            )
            assert server.config_generation == applied_generation
            assert server.reviewer_config.max_per_day == 7

            config_path.write_text("[reviewer]\nmax_per_day = 9\n")
            await wait_until(lambda: server.reviewer_config.max_per_day == 9)
            assert server.config_generation != applied_generation

            config_path.unlink()
            await wait_until(lambda: server.reviewer_config.max_per_day == 100)
            assert server.config_store is not None
            assert server.config_store.last_error() is None
        finally:
            await server.close()

    asyncio.run(scenario())


def test_config_file_monitor_survives_an_unexpected_reload_failure(
    socket_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home")
    config_path = layout.user_config_dir / "spotter.toml"
    initial = resolve_config(layout=layout)
    calls = 0

    def load() -> ResolvedConfig:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("loader exploded")
        return resolve_config(layout=layout)

    async def wait_until(predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(0.01)

    async def scenario() -> None:
        server = DaemonServer(
            socket_path,
            config_store=ConfigSnapshotStore(initial),
            config_loader=load,
            config_watch_interval=0.01,
        )
        await server.start()
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text("[reviewer]\nmax_per_day = 7\n")
            await wait_until(lambda: server.health == RuntimeHealth.DEGRADED)
            assert server._config_watch_task is not None
            assert not server._config_watch_task.done()

            config_path.write_text("[reviewer]\nmax_per_day = 9\n")
            await wait_until(lambda: server.reviewer_config.max_per_day == 9)
        finally:
            await server.close()

    asyncio.run(scenario())

    assert "automatic config reload failed unexpectedly: loader exploded" in capsys.readouterr().err


def test_daemon_reload_cli_reports_value_free_plan(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Client:
        async def reload_config(self) -> ConfigReloadResult:
            return ConfigReloadResult(
                ReloadDisposition.STAGED_NEXT_TURN,
                "cfg-active",
                "cfg-candidate",
                changes=(ConfigChange("reviewer.model", ActivationBoundary.NEXT_TURN),),
            )

    monkeypatch.setattr("spotter.cli.DaemonClient", Client)

    assert main(["daemon", "reload"]) == 0
    output = capsys.readouterr().out
    assert "staged_next_turn" in output
    assert "active=cfg-active" in output
    assert "candidate=cfg-candidate" in output
    assert "changes=reviewer.model" in output


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

            assert response["protocol"] == PROTOCOL_VERSION
            assert response["ok"] is False
            assert response["error_code"] == "incompatible_protocol"
            assert "daemon supports 1..1" in response["error"]
            assert response["min_peer_protocol"] == 1
            assert (await DaemonClient(socket_path).status()).health == RuntimeHealth.HEALTHY
        finally:
            await server.close()

    asyncio.run(scenario())


def test_daemon_rejects_an_incompatible_peer_range_without_breaking_later_clients(
    socket_path: Path,
) -> None:
    async def scenario() -> None:
        server = DaemonServer(socket_path)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.write(
                json.dumps(
                    {
                        "protocol": PROTOCOL_VERSION,
                        "method": "ping",
                        "peer": {
                            "ipc_protocol_version": PROTOCOL_VERSION,
                            "min_peer_protocol": 2,
                            "max_peer_protocol": 3,
                        },
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()

            assert response["ok"] is False
            assert response["error_code"] == "incompatible_protocol"
            assert "peer supports control protocol 2..3" in response["error"]
            assert (await DaemonClient(socket_path).status()).compatibility == (
                RuntimeCompatibility.MATCHED
            )
        finally:
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "response,expected",
    [
        (
            {
                "protocol": PROTOCOL_VERSION,
                "ok": True,
                "health": "healthy",
                "pid": 42,
                "spotter_version": "0.0",
                "build_id": "retired-build",
                "ipc_protocol_version": PROTOCOL_VERSION,
                "min_peer_protocol": PROTOCOL_VERSION,
                "max_peer_protocol": PROTOCOL_VERSION,
                "capabilities": ["status"],
            },
            RuntimeCompatibility.COMPATIBLE_STALE,
        ),
        (
            {
                "protocol": PROTOCOL_VERSION,
                "ok": True,
                "health": "healthy",
                "pid": 42,
                "spotter_version": "legacy",
                "build_id": "retired-build",
                "ipc_protocol_version": PROTOCOL_VERSION,
            },
            RuntimeCompatibility.UNKNOWN,
        ),
    ],
)
def test_client_classifies_compatible_and_unknown_daemon_metadata(
    socket_path: Path,
    response: dict[str, object],
    expected: RuntimeCompatibility,
) -> None:
    async def scenario() -> None:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handler, path=socket_path)
        async with server:
            assert (await DaemonClient(socket_path).status()).compatibility == expected

    asyncio.run(scenario())


def test_client_reports_incompatible_daemon_protocol_as_stale(socket_path: Path) -> None:
    async def scenario() -> None:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(b'{"protocol":999,"ok":false,"error":"old"}\n')
            await writer.drain()
            writer.close()

        server = await asyncio.start_unix_server(handler, path=socket_path)
        async with server:
            status = await DaemonClient(socket_path).status()
            assert status.health == RuntimeHealth.DEGRADED
            assert status.compatibility == RuntimeCompatibility.INCOMPATIBLE_STALE
            assert "spotter daemon restart" in (status.detail or "")
            with pytest.raises(DaemonProtocolMismatch):
                await DaemonClient(socket_path).shutdown()

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
            await DaemonClient(socket_path).gate(event, GatesConfig(), "/repo", "cfg-gate-test")
            async with asyncio.timeout(1):
                while not recovery.observations:
                    await asyncio.sleep(0)
        finally:
            await server.close()

        params, decision = recovery.observations[0]
        assert decision["rule"] == "git_reset_hard"
        assert params["config_generation"] == "cfg-gate-test"
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
