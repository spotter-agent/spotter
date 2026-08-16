import json
from dataclasses import replace
from pathlib import Path

from spotter.config import SpotterConfig
from spotter.paths import RuntimeLayout
from spotter.runtime_fingerprint import (
    expected_runtime_construction_fingerprint,
    runtime_construction_fingerprint,
)


def _config(**reviewer: object) -> SpotterConfig:
    return SpotterConfig.from_mapping(
        {
            "main_agent": {"adapter": "codex"},
            "reviewer": reviewer,
        }
    )


def test_runtime_fingerprint_excludes_hot_reviewer_settings(tmp_path: Path) -> None:
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home", environ={})
    manifest = {
        "integration_generation": "generation-1",
        "runtime_mode": "managed",
        "app_server_endpoint": "ws://127.0.0.1:4321?token=secret",
    }

    first = runtime_construction_fingerprint(
        layout,
        _config(every_steps=1, max_per_day=10),
        manifest,
    )
    second = runtime_construction_fingerprint(
        layout,
        _config(every_steps=99, max_per_day=999),
        manifest,
    )

    assert first == second
    assert "secret" not in first


def test_runtime_fingerprint_changes_with_construction_inputs(tmp_path: Path) -> None:
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home", environ={})
    config = _config()
    manifest = {
        "integration_generation": "generation-1",
        "runtime_mode": "managed",
        "app_server_endpoint": "ws://127.0.0.1:4321",
    }
    baseline = runtime_construction_fingerprint(layout, config, manifest)

    assert (
        runtime_construction_fingerprint(
            layout,
            config,
            {**manifest, "integration_generation": "generation-2"},
        )
        != baseline
    )
    assert (
        runtime_construction_fingerprint(
            layout,
            config,
            {**manifest, "app_server_endpoint": "ws://127.0.0.1:9876"},
        )
        != baseline
    )
    assert (
        runtime_construction_fingerprint(
            layout,
            config,
            {**manifest, "runtime_layout": {"daemon_executable": "/new/bin/spotterd"}},
        )
        != baseline
    )
    assert (
        runtime_construction_fingerprint(
            layout,
            replace(config, main_agent=replace(config.main_agent, adapter="claude")),
            manifest,
        )
        != baseline
    )
    assert (
        runtime_construction_fingerprint(
            layout,
            config,
            manifest,
            control_socket=tmp_path / "other.sock",
        )
        != baseline
    )


def test_expected_runtime_fingerprint_reads_the_staged_manifest(tmp_path: Path) -> None:
    layout = RuntimeLayout.discover(spotter_root=tmp_path / "home", environ={})
    layout.integration_manifest.parent.mkdir(parents=True)
    layout.integration_manifest.write_text(
        json.dumps(
            {
                "integration_generation": "generation-1",
                "runtime_mode": "managed",
            }
        )
    )
    first = expected_runtime_construction_fingerprint(layout)

    raw = json.loads(layout.integration_manifest.read_text())
    raw["integration_generation"] = "generation-2"
    layout.integration_manifest.write_text(json.dumps(raw))

    assert expected_runtime_construction_fingerprint(layout) != first
