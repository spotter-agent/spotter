import threading
from pathlib import Path

from spotter.config import (
    ActivationBoundary,
    ConfigSnapshotStore,
    ReloadDisposition,
    ResolvedConfig,
    resolve_config,
)
from spotter.paths import RuntimeLayout


def _layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout.discover(
        spotter_root=tmp_path / "home",
        user_home=tmp_path / "account",
        argv0="__main__.py",
        environ={},
    )


def test_hot_reload_atomically_replaces_the_active_snapshot(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    initial = resolve_config(layout=layout)
    candidate = resolve_config(layout=layout, overrides={"reviewer": {"max_per_day": 7}})
    store = ConfigSnapshotStore(initial)

    result = store.reload(lambda: candidate)

    assert result.disposition == ReloadDisposition.APPLIED
    assert result.active_generation == candidate.resolved_config_generation
    assert [(change.path, change.activation_boundary) for change in result.changes] == [
        ("reviewer.max_per_day", ActivationBoundary.HOT)
    ]
    assert store.snapshot() is candidate
    assert store.pending_generation() is None


def test_next_turn_reload_stages_the_whole_snapshot_until_boundary(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    initial = resolve_config(layout=layout)
    candidate = resolve_config(
        layout=layout,
        overrides={
            "reviewer": {"max_per_day": 7},
            "gates": {"forbidden_paths": ["private/*"]},
        },
    )
    store = ConfigSnapshotStore(initial)

    result = store.reload(lambda: candidate)

    assert result.disposition == ReloadDisposition.STAGED_NEXT_TURN
    assert store.snapshot() is initial
    assert store.pending_generation() == candidate.resolved_config_generation

    activated = store.activate_next_turn()

    assert activated.disposition == ReloadDisposition.APPLIED
    assert store.snapshot() is candidate
    assert store.pending_generation() is None


def test_disruptive_reload_reports_required_action_without_activation(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    initial = resolve_config(layout=layout)
    candidate = resolve_config(
        layout=layout, overrides={"main_agent": {"adapter": "future-adapter"}}
    )
    store = ConfigSnapshotStore(initial)

    result = store.reload(lambda: candidate)

    assert result.disposition == ReloadDisposition.ACTION_REQUIRED
    assert result.required_boundaries == (ActivationBoundary.INTEGRATION_RECONFIGURE,)
    assert store.snapshot() is initial
    assert store.pending_generation() is None


def test_invalid_reload_preserves_active_and_valid_pending_snapshots(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    initial = resolve_config(layout=layout)
    pending = resolve_config(layout=layout, overrides={"gates": {"block_dependency_changes": True}})
    store = ConfigSnapshotStore(initial)
    store.reload(lambda: pending)
    layout.user_config_dir.mkdir(parents=True)
    (layout.user_config_dir / "spotter.toml").write_text("[reviewer]\nmax_per_day = -1\n")

    result = store.reload(lambda: resolve_config(layout=layout))

    assert result.disposition == ReloadDisposition.REJECTED_INVALID
    assert result.candidate_generation is None
    assert result.error is not None
    assert store.snapshot() is initial
    assert store.pending_generation() == pending.resolved_config_generation
    assert store.last_error() == result.error


def test_racing_reloads_publish_only_the_latest_staged_snapshot(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    initial = resolve_config(layout=layout)
    first = resolve_config(layout=layout, overrides={"reviewer": {"model": "first"}})
    second = resolve_config(layout=layout, overrides={"gates": {"forbidden_paths": ["second/*"]}})
    store = ConfigSnapshotStore(initial)
    first_loading = threading.Event()
    release_first = threading.Event()

    def load_first() -> ResolvedConfig:
        first_loading.set()
        assert release_first.wait(timeout=2)
        return first

    first_thread = threading.Thread(target=lambda: store.reload(load_first))
    second_thread = threading.Thread(target=lambda: store.reload(lambda: second))
    first_thread.start()
    assert first_loading.wait(timeout=2)
    second_thread.start()

    assert store.snapshot() is initial, "readers must not wait for parsing to finish"
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert store.pending_generation() == second.resolved_config_generation
    store.activate_next_turn()
    assert store.snapshot() is second
