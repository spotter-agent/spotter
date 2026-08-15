"""Configuration loading, precedence, provenance, and validation."""

import hashlib
import json
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spotter.paths import RuntimeLayout


class ConfigurationError(ValueError):
    """Raised when a Spotter configuration is malformed."""


# "default" delegates to the codex account's own model. A pinned id is an
# auth-dependent liability: ChatGPT-account auth rejects several ids with a
# multi-minute retry loop, and the reviewer model is an experimental variable
# (plan Q6/P5), not a constant. Pin one explicitly only if your auth allows it.
DEFAULT_REVIEWER_MODEL = "default"
CONFIG_SCHEMA = "spotter.config"
CONFIG_SCHEMA_VERSION = 1
LEGACY_CONFIG_SCHEMA_VERSION = 0

DEFAULT_CONFIG: dict[str, Any] = {
    "config_schema": CONFIG_SCHEMA,
    "config_schema_version": CONFIG_SCHEMA_VERSION,
    "observation_only": True,
    "snapshot_on_patch": True,
    "main_agent": {"adapter": "codex"},
    "reviewer": {
        "model": DEFAULT_REVIEWER_MODEL,
        "on_signals": False,
        "deliver_on_signals": False,
        "every_steps": 0,
        "max_per_session": 20,
        "max_per_day": 100,
    },
    "gates": {"forbidden_paths": [], "block_dependency_changes": False},
    "mcp_semantics": {},
}


@dataclass(frozen=True)
class ConfigSourceLayer:
    """Non-secret identity of one layer used to build an effective config."""

    name: str
    path: str | None
    modified_ns: int | None
    content_sha256: str


@dataclass(frozen=True)
class ResolvedConfig:
    """One validated, immutable effective configuration snapshot."""

    config: "SpotterConfig"
    resolved_config_generation: str
    resolved_config_hash: str
    source_layers: tuple[ConfigSourceLayer, ...]
    loaded_at: str


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
class McpToolSemantics:
    """Trusted, exact semantics for one MCP server/tool pair."""

    server: str
    tool: str
    operation: str
    reversibility: str
    resource_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpotterConfig:
    main_agent: MainAgentConfig
    reviewer: ReviewerConfig
    gates: GatesConfig = GatesConfig()
    observation_only: bool = True
    snapshot_on_patch: bool = True
    mcp_semantics: tuple[McpToolSemantics, ...] = ()
    config_schema_version: int = CONFIG_SCHEMA_VERSION

    @classmethod
    def from_toml(cls, path: Path) -> "SpotterConfig":
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)
        return cls.from_mapping(raw)

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "SpotterConfig":
        config_schema_version = _config_schema_version(raw)
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
            mcp_semantics=_mcp_semantics(raw),
            observation_only=observation_only,
            snapshot_on_patch=_bool(raw, "snapshot_on_patch", True),
            config_schema_version=config_schema_version,
        )


def resolve_config(
    *,
    layout: RuntimeLayout | None = None,
    repository: Path | None = None,
    explicit_path: Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> ResolvedConfig:
    """Resolve the canonical defaults < global < repository < invocation order.

    Missing optional file layers are skipped. An explicitly selected file is
    required to exist, because silently ignoring a misspelled ``--config`` is
    less safe than refusing activation.
    """

    runtime_layout = layout or RuntimeLayout.discover()
    effective = _copy_mapping(DEFAULT_CONFIG)
    sources = [
        ConfigSourceLayer(
            name="built_in",
            path=None,
            modified_ns=None,
            content_sha256=_mapping_hash(DEFAULT_CONFIG),
        )
    ]
    loaded_paths: set[Path] = set()

    global_path = runtime_layout.user_config_dir / "spotter.toml"
    _merge_file_layer(effective, sources, loaded_paths, "global", global_path, required=False)

    if repository is not None:
        repository_path = _repository_root(repository) / "spotter.toml"
        _merge_file_layer(
            effective,
            sources,
            loaded_paths,
            "repository",
            repository_path,
            required=False,
        )

    if explicit_path is not None:
        _merge_file_layer(
            effective,
            sources,
            loaded_paths,
            "explicit",
            explicit_path,
            required=True,
        )

    if overrides:
        override_mapping = _copy_mapping(overrides)
        _validate_layer_schema(override_mapping)
        _deep_merge(effective, override_mapping)
        sources.append(
            ConfigSourceLayer(
                name="runtime_override",
                path=None,
                modified_ns=None,
                content_sha256=_mapping_hash(override_mapping),
            )
        )

    config = SpotterConfig.from_mapping(effective)
    config_hash = _effective_config_hash(config)
    generation_material = json.dumps(
        {
            "config_hash": config_hash,
            "sources": [asdict(source) for source in sources],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    generation = "cfg-" + hashlib.sha256(generation_material).hexdigest()[:20]
    return ResolvedConfig(
        config=config,
        resolved_config_generation=generation,
        resolved_config_hash=config_hash,
        source_layers=tuple(sources),
        loaded_at=datetime.now(UTC).isoformat(),
    )


def _merge_file_layer(
    effective: dict[str, Any],
    sources: list[ConfigSourceLayer],
    loaded_paths: set[Path],
    name: str,
    path: Path,
    *,
    required: bool,
) -> None:
    normalized = path.expanduser().resolve()
    if normalized in loaded_paths:
        return
    try:
        contents = normalized.read_bytes()
    except FileNotFoundError:
        if required:
            raise
        return
    try:
        raw = tomllib.loads(contents.decode())
    except UnicodeDecodeError as error:
        raise ConfigurationError(f"{normalized} must be UTF-8 TOML") from error
    if not isinstance(raw, dict):  # pragma: no cover - tomllib currently always returns a dict
        raise ConfigurationError(f"{normalized} must contain a TOML table")
    _validate_layer_schema(raw)
    _deep_merge(effective, raw)
    stat = normalized.stat()
    sources.append(
        ConfigSourceLayer(
            name=name,
            path=str(normalized),
            modified_ns=stat.st_mtime_ns,
            content_sha256=hashlib.sha256(contents).hexdigest(),
        )
    )
    loaded_paths.add(normalized)


def _repository_root(start: Path) -> Path:
    candidate = start.expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    for directory in (candidate, *candidate.parents):
        if (directory / ".git").exists():
            return directory
    return candidate


def _validate_layer_schema(raw: Mapping[str, Any]) -> None:
    if "config_schema" in raw or "config_schema_version" in raw:
        _config_schema_version(dict(raw))


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _deep_merge(current, value)
        else:
            base[key] = _copy_value(value)


def _copy_mapping(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _copy_value(value) for key, value in raw.items()}


def _copy_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _copy_mapping(value)
    if isinstance(value, list):
        return [_copy_value(item) for item in value]
    return value


def _mapping_hash(raw: Mapping[str, Any]) -> str:
    normalized = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(normalized).hexdigest()


def _effective_config_hash(config: SpotterConfig) -> str:
    normalized = _redact_sensitive(asdict(config))
    return _mapping_hash(normalized)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _sensitive_field(key) else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive(item) for item in value]
    return value


def _config_schema_version(raw: dict[str, Any]) -> int:
    schema = raw.get("config_schema")
    version = raw.get("config_schema_version")
    if schema is None and version is None:
        return LEGACY_CONFIG_SCHEMA_VERSION
    if schema != CONFIG_SCHEMA:
        raise ConfigurationError(f"unsupported config schema {schema!r}")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ConfigurationError("config_schema_version must be an integer")
    if version != CONFIG_SCHEMA_VERSION:
        direction = "newer" if version > CONFIG_SCHEMA_VERSION else "unsupported"
        raise ConfigurationError(
            f"{direction} config schema v{version}; this build understands v{CONFIG_SCHEMA_VERSION}"
        )
    return version


def _mcp_semantics(raw: dict[str, Any]) -> tuple[McpToolSemantics, ...]:
    table = _optional_table(raw, "mcp_semantics")
    entries: list[McpToolSemantics] = []
    identities: set[tuple[str, str]] = set()
    for server, tools in table.items():
        if not isinstance(server, str) or not server.strip() or not isinstance(tools, dict):
            raise ConfigurationError("mcp_semantics entries must be server tables")
        for tool, semantics in tools.items():
            path = f'mcp_semantics."{server}"."{tool}"'
            if not isinstance(tool, str) or not tool.strip() or not isinstance(semantics, dict):
                raise ConfigurationError(f"{path} must be a table")
            unexpected = set(semantics) - {"operation", "reversibility", "resource_fields"}
            if unexpected:
                raise ConfigurationError(
                    f"{path} has unknown fields: {', '.join(sorted(unexpected))}"
                )
            operation = _choice(
                semantics, "operation", {"read", "write", "delete", "unknown"}, path
            )
            reversibility = _choice(semantics, "reversibility", {"A", "B", "C"}, path)
            if operation == "read" and reversibility != "A":
                raise ConfigurationError(f"{path} read operations must use reversibility A")
            if operation in {"write", "delete"} and reversibility == "A":
                raise ConfigurationError(f"{path} mutations cannot use reversibility A")
            if operation == "unknown" and reversibility != "C":
                raise ConfigurationError(f"{path} unknown operations must use reversibility C")
            resource_fields = _string_tuple(semantics, "resource_fields")
            if len(resource_fields) > 8 or any(
                not field.strip() or len(field) > 64 for field in resource_fields
            ):
                raise ConfigurationError(f"{path}.resource_fields must contain 0-8 short names")
            if any(_sensitive_field(field) for field in resource_fields):
                raise ConfigurationError(
                    f"{path}.resource_fields cannot include secret-bearing names"
                )
            identity = (server.casefold(), tool.casefold())
            if identity in identities:
                raise ConfigurationError(f"duplicate MCP semantics for {server}/{tool}")
            identities.add(identity)
            entries.append(
                McpToolSemantics(
                    server=identity[0],
                    tool=identity[1],
                    operation=operation,
                    reversibility=reversibility,
                    resource_fields=resource_fields,
                )
            )
            if len(entries) > 256:
                raise ConfigurationError("mcp_semantics supports at most 256 tool entries")
    return tuple(entries)


def _choice(raw: dict[str, Any], key: str, choices: set[str], path: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ConfigurationError(f"{path}.{key} must be one of: {allowed}")
    return value


def _sensitive_field(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return any(part in normalized for part in ("auth", "credential", "password", "secret", "token"))


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
