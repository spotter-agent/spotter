"""Configuration loading and validation."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """Raised when a Spotter configuration is malformed."""


# "default" delegates to the codex account's own model. A pinned id is an
# auth-dependent liability: ChatGPT-account auth rejects several ids with a
# multi-minute retry loop, and the reviewer model is an experimental variable
# (plan Q6/P5), not a constant. Pin one explicitly only if your auth allows it.
DEFAULT_REVIEWER_MODEL = "default"


@dataclass(frozen=True)
class MainAgentConfig:
    adapter: str


@dataclass(frozen=True)
class ReviewerConfig:
    model: str = DEFAULT_REVIEWER_MODEL
    # Signal-triggered reviews spend tokens too, so they require an explicit opt-in.
    on_signals: bool = False
    # Live steer delivery is a separate explicit opt-in while intervention
    # benefit/harm evidence is still being collected.
    deliver_on_signals: bool = False
    # Auto-run the SHADOW reviewer every N tool proposals (0 = off). Off by
    # default: even shadow judgments spend the user's model tokens, and silent
    # spending is not "safe" just because nothing is injected.
    every_steps: int = 0
    # Ceilings on that spend. Enabling a cadence without a ceiling offers the
    # user an unbounded background bill they cannot see (issue #52).
    max_per_session: int = 20
    max_per_day: int = 100


@dataclass(frozen=True)
class GatesConfig:
    forbidden_paths: tuple[str, ...] = ()
    block_dependency_changes: bool = False


@dataclass(frozen=True)
class SpotterConfig:
    main_agent: MainAgentConfig
    reviewer: ReviewerConfig
    gates: GatesConfig = GatesConfig()
    observation_only: bool = True
    snapshot_on_patch: bool = True

    @classmethod
    def from_toml(cls, path: Path) -> "SpotterConfig":
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "SpotterConfig":
        main_agent = _table(raw, "main_agent")
        reviewer = _optional_table(raw, "reviewer")
        gates = _optional_table(raw, "gates")
        observation_only = raw.get("observation_only", True)
        if not isinstance(observation_only, bool):
            raise ConfigurationError("observation_only must be a boolean")
        on_signals = _bool(reviewer, "on_signals", False)
        deliver_on_signals = _bool(reviewer, "deliver_on_signals", False)
        if deliver_on_signals and not on_signals:
            raise ConfigurationError("reviewer.deliver_on_signals requires reviewer.on_signals")
        if deliver_on_signals and observation_only:
            raise ConfigurationError(
                "reviewer.deliver_on_signals requires observation_only = false"
            )
        return cls(
            main_agent=MainAgentConfig(adapter=_string(main_agent, "adapter")),
            reviewer=ReviewerConfig(
                model=_optional_string(reviewer, "model", DEFAULT_REVIEWER_MODEL),
                on_signals=on_signals,
                deliver_on_signals=deliver_on_signals,
                every_steps=_int(reviewer, "every_steps", 0),
                max_per_session=_int(reviewer, "max_per_session", 20),
                max_per_day=_int(reviewer, "max_per_day", 100),
            ),
            gates=GatesConfig(
                forbidden_paths=_string_tuple(gates, "forbidden_paths"),
                block_dependency_changes=_bool(gates, "block_dependency_changes", False),
            ),
            observation_only=observation_only,
            snapshot_on_patch=_bool(raw, "snapshot_on_patch", True),
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


def _string_tuple(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{key} must be a list of strings")
    return tuple(value)


def _int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigurationError(f"{key} must be a non-negative integer")
    return value


def _bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigurationError(f"{key} must be a boolean")
    return value
