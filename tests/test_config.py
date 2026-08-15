from pathlib import Path

import pytest

from spotter.config import ConfigurationError, SpotterConfig


def test_loads_example_configuration() -> None:
    config = SpotterConfig.from_toml(Path("spotter.example.toml"))

    assert config.main_agent.adapter == "codex"
    assert config.reviewer.model == "default"
    assert config.reviewer.on_signals is False
    assert config.reviewer.deliver_on_signals is False
    assert config.observation_only is True


def test_uses_default_reviewer_model_when_reviewer_is_omitted() -> None:
    config = SpotterConfig.from_mapping({"main_agent": {"adapter": "codex"}})

    assert config.reviewer.model == "default"


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
