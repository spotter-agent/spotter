from pathlib import Path

import pytest

from spotter.config import ConfigurationError, SpotterConfig


def test_loads_example_configuration() -> None:
    config = SpotterConfig.from_toml(Path("spotter.example.toml"))

    assert config.main_agent.adapter == "codex"
    assert config.reviewer.model == "gpt-5.3-spark"
    assert config.observation_only is True


def test_uses_default_reviewer_model_when_reviewer_is_omitted() -> None:
    config = SpotterConfig.from_mapping({"main_agent": {"adapter": "codex"}})

    assert config.reviewer.model == "gpt-5.3-spark"


def test_rejects_invalid_reviewer_table() -> None:
    with pytest.raises(ConfigurationError, match="reviewer must be a table"):
        SpotterConfig.from_mapping({"main_agent": {"adapter": "codex"}, "reviewer": "invalid"})
