"""Configuration loading and validation."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """Raised when a Spotter configuration is malformed."""


DEFAULT_REVIEWER_MODEL = "gpt-5.3-spark"


@dataclass(frozen=True)
class MainAgentConfig:
    adapter: str


@dataclass(frozen=True)
class ReviewerConfig:
    model: str = DEFAULT_REVIEWER_MODEL


@dataclass(frozen=True)
class SpotterConfig:
    main_agent: MainAgentConfig
    reviewer: ReviewerConfig
    observation_only: bool = True

    @classmethod
    def from_toml(cls, path: Path) -> "SpotterConfig":
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "SpotterConfig":
        main_agent = _table(raw, "main_agent")
        reviewer = _optional_table(raw, "reviewer")
        observation_only = raw.get("observation_only", True)
        if not isinstance(observation_only, bool):
            raise ConfigurationError("observation_only must be a boolean")
        return cls(
            main_agent=MainAgentConfig(adapter=_string(main_agent, "adapter")),
            reviewer=ReviewerConfig(
                model=_optional_string(reviewer, "model", DEFAULT_REVIEWER_MODEL)
            ),
            observation_only=observation_only,
        )


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a table")
    return value


def _optional_table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a table")
    return value


def _string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value


def _optional_string(raw: dict[str, Any], key: str, default: str) -> str:
    if key not in raw:
        return default
    return _string(raw, key)
