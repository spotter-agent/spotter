from pathlib import Path

import pytest

from spotter.config import (
    CONFIG_SCHEMA,
    CONFIG_SCHEMA_VERSION,
    LEGACY_CONFIG_SCHEMA_VERSION,
    ConfigurationError,
    SpotterConfig,
    resolve_config,
)
from spotter.paths import RuntimeLayout


def _layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout.discover(
        spotter_root=tmp_path / "global",
        user_home=tmp_path / "home",
        argv0="__main__.py",
        environ={},
    )


def test_loads_example_configuration() -> None:
    config = SpotterConfig.from_toml(Path("spotter.example.toml"))

    assert config.main_agent.adapter == "codex"
    assert config.reviewer.model == "default"
    assert config.reviewer.on_signals is False
    assert config.reviewer.deliver_on_signals is False
    assert config.observation_only is True
    assert config.config_schema_version == CONFIG_SCHEMA_VERSION


def test_uses_default_reviewer_model_when_reviewer_is_omitted() -> None:
    config = SpotterConfig.from_mapping({"main_agent": {"adapter": "codex"}})

    assert config.reviewer.model == "default"
    assert config.config_schema_version == LEGACY_CONFIG_SCHEMA_VERSION


def test_resolves_canonical_config_precedence_and_provenance(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.user_config_dir.mkdir(parents=True)
    (layout.user_config_dir / "spotter.toml").write_text(
        "[main_agent]\nadapter = 'global'\n[reviewer]\nmax_per_day = 70\n"
    )
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    (repository / "nested").mkdir()
    (repository / "spotter.toml").write_text(
        "[main_agent]\nadapter = 'repository'\n[reviewer]\nmax_per_session = 7\n"
    )
    explicit = tmp_path / "invocation.toml"
    explicit.write_text("[reviewer]\nmax_per_session = 3\n")

    resolved = resolve_config(
        layout=layout,
        repository=repository / "nested",
        explicit_path=explicit,
        overrides={"reviewer": {"max_per_day": 9}},
    )

    assert resolved.config.main_agent.adapter == "repository"
    assert resolved.config.reviewer.max_per_session == 3
    assert resolved.config.reviewer.max_per_day == 9
    assert resolved.config.snapshot_on_patch is True
    assert [layer.name for layer in resolved.source_layers] == [
        "built_in",
        "global",
        "repository",
        "explicit",
        "runtime_override",
    ]
    assert all(
        layer.content_sha256 and "global" not in layer.content_sha256
        for layer in resolved.source_layers
    )
    assert resolved.resolved_config_generation.startswith("cfg-")
    assert len(resolved.resolved_config_hash) == 64


def test_resolved_identity_is_stable_for_unchanged_sources(tmp_path: Path) -> None:
    layout = _layout(tmp_path)

    first = resolve_config(layout=layout, repository=tmp_path)
    second = resolve_config(layout=layout, repository=tmp_path)

    assert first.config == second.config
    assert first.resolved_config_hash == second.resolved_config_hash
    assert first.resolved_config_generation == second.resolved_config_generation
    assert first.loaded_at <= second.loaded_at


def test_explicit_config_is_required_and_not_loaded_twice(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.user_config_dir.mkdir(parents=True)
    global_path = layout.user_config_dir / "spotter.toml"
    global_path.write_text("[main_agent]\nadapter = 'codex'\n")

    resolved = resolve_config(layout=layout, explicit_path=global_path)

    assert [layer.name for layer in resolved.source_layers] == ["built_in", "global"]
    with pytest.raises(FileNotFoundError):
        resolve_config(layout=layout, explicit_path=tmp_path / "missing.toml")


def test_invalid_repository_layer_preserves_valid_operator_config(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.user_config_dir.mkdir(parents=True)
    (layout.user_config_dir / "spotter.toml").write_text(
        "[main_agent]\nadapter = 'codex'\n[reviewer]\nmax_per_day = 10\n"
    )
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    (repository / "spotter.toml").write_text("[reviewer]\nmax_per_day = -1\n")

    resolved = resolve_config(layout=layout, repository=repository)

    assert resolved.config.reviewer.max_per_day == 10
    assert resolved.source_layers[-1].ignored_fields == ("*",)
    assert "ignored invalid repository config" in resolved.diagnostics[0]


def test_explicit_invalid_layer_still_refuses_activation(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.toml"
    explicit.write_text("[reviewer]\nmax_per_day = -1\n")

    with pytest.raises(ConfigurationError, match="non-negative integer"):
        resolve_config(layout=_layout(tmp_path), explicit_path=explicit)


def test_repository_policy_can_only_tighten_operator_gates(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout.user_config_dir.mkdir(parents=True)
    (layout.user_config_dir / "spotter.toml").write_text(
        """observation_only = false
[gates]
forbidden_paths = ["secrets/*"]
block_dependency_changes = true
[mcp_semantics.admin.destroy]
operation = "delete"
reversibility = "C"
"""
    )
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    (repository / "spotter.toml").write_text(
        """observation_only = true
[gates]
forbidden_paths = ["private/*"]
block_dependency_changes = false
[mcp_semantics.admin.destroy]
operation = "read"
reversibility = "A"
"""
    )

    resolved = resolve_config(layout=layout, repository=repository)

    assert resolved.config.observation_only is False
    assert resolved.config.gates.forbidden_paths == ("secrets/*", "private/*")
    assert resolved.config.gates.block_dependency_changes is True
    assert resolved.config.mcp_semantics[0].operation == "delete"
    assert resolved.source_layers[-1].ignored_fields == (
        "mcp_semantics",
        "observation_only",
    )
    assert "cannot override operator policy" in resolved.diagnostics[0]


def test_non_repository_directory_does_not_load_a_local_spotter_file(tmp_path: Path) -> None:
    (tmp_path / "spotter.toml").write_text("[main_agent]\nadapter = 'not-a-repository'\n")

    resolved = resolve_config(layout=_layout(tmp_path), repository=tmp_path)

    assert resolved.config.main_agent.adapter == "codex"
    assert [layer.name for layer in resolved.source_layers] == ["built_in"]


@pytest.mark.parametrize(
    ("schema", "version", "message"),
    [
        (CONFIG_SCHEMA, CONFIG_SCHEMA_VERSION + 1, "newer config schema"),
        (CONFIG_SCHEMA, 0, "unsupported config schema v0"),
        ("future.config", CONFIG_SCHEMA_VERSION, "unsupported config schema"),
        (CONFIG_SCHEMA, "one", "config_schema_version must be an integer"),
        (CONFIG_SCHEMA, True, "config_schema_version must be an integer"),
    ],
)
def test_refuses_unsupported_config_schema_before_activation(
    schema: str, version: object, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        SpotterConfig.from_mapping(
            {
                "config_schema": schema,
                "config_schema_version": version,
                "main_agent": {"adapter": "codex"},
            }
        )


@pytest.mark.parametrize(
    "partial",
    [
        {"config_schema": CONFIG_SCHEMA},
        {"config_schema_version": CONFIG_SCHEMA_VERSION},
    ],
)
def test_refuses_partial_config_schema_identity(partial: dict[str, object]) -> None:
    with pytest.raises(ConfigurationError, match="config schema|config_schema_version"):
        SpotterConfig.from_mapping({**partial, "main_agent": {"adapter": "codex"}})


def test_loads_exact_mcp_semantics_by_server_and_tool() -> None:
    config = SpotterConfig.from_mapping(
        {
            "main_agent": {"adapter": "codex"},
            "mcp_semantics": {
                "inventory": {
                    "lookup": {
                        "operation": "read",
                        "reversibility": "A",
                        "resource_fields": ["item_id"],
                    }
                },
                "admin": {
                    "lookup": {
                        "operation": "write",
                        "reversibility": "C",
                    }
                },
            },
        }
    )

    assert [(rule.server, rule.tool, rule.reversibility) for rule in config.mcp_semantics] == [
        ("inventory", "lookup", "A"),
        ("admin", "lookup", "C"),
    ]


@pytest.mark.parametrize(
    ("semantics", "message"),
    [
        ({"operation": "read", "reversibility": "C"}, "read operations must use"),
        ({"operation": "write", "reversibility": "A"}, "mutations cannot use"),
        ({"operation": "unknown", "reversibility": "B"}, "unknown operations must use"),
        (
            {"operation": "read", "reversibility": "A", "resource_fields": ["access_token"]},
            "secret-bearing names",
        ),
        ({"operation": "observe", "reversibility": "A"}, "operation must be one of"),
    ],
)
def test_rejects_unsafe_or_malformed_mcp_semantics(
    semantics: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        SpotterConfig.from_mapping(
            {
                "main_agent": {"adapter": "codex"},
                "mcp_semantics": {"server": {"tool": semantics}},
            }
        )


def test_rejects_invalid_reviewer_table() -> None:
    with pytest.raises(ConfigurationError, match="reviewer must be a table"):
        SpotterConfig.from_mapping({"main_agent": {"adapter": "codex"}, "reviewer": "invalid"})


def test_signal_reviews_require_an_explicit_boolean_opt_in() -> None:
    config = SpotterConfig.from_mapping(
        {"main_agent": {"adapter": "codex"}, "reviewer": {"on_signals": True}}
    )

    assert config.reviewer.on_signals is True
    with pytest.raises(ConfigurationError, match="on_signals must be a boolean"):
        SpotterConfig.from_mapping(
            {"main_agent": {"adapter": "codex"}, "reviewer": {"on_signals": "yes"}}
        )


def test_live_delivery_requires_signal_review_opt_in() -> None:
    config = SpotterConfig.from_mapping(
        {
            "observation_only": False,
            "main_agent": {"adapter": "codex"},
            "reviewer": {"on_signals": True, "deliver_on_signals": True},
        }
    )

    assert config.reviewer.deliver_on_signals is True
    with pytest.raises(ConfigurationError, match="requires reviewer.on_signals"):
        SpotterConfig.from_mapping(
            {"main_agent": {"adapter": "codex"}, "reviewer": {"deliver_on_signals": True}}
        )
    with pytest.raises(ConfigurationError, match="requires observation_only = false"):
        SpotterConfig.from_mapping(
            {
                "main_agent": {"adapter": "codex"},
                "reviewer": {"on_signals": True, "deliver_on_signals": True},
            }
        )
