from pathlib import Path

import pytest

from spotter.config import (
    CONFIG_SCHEMA,
    CONFIG_SCHEMA_VERSION,
    LEGACY_CONFIG_SCHEMA_VERSION,
    ConfigurationError,
    SpotterConfig,
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
