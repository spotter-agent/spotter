"""Command-line entry point for the passive-observation prototype."""

import argparse
import tomllib
from collections.abc import Sequence
from pathlib import Path

from spotter.codex import CodexAdapter
from spotter.config import ConfigurationError, SpotterConfig
from spotter.core import SpotterRuntime
from spotter.trace import TraceEvent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Observe a coding-agent trajectory")
    parser.add_argument("--config", type=Path, required=True, help="path to Spotter TOML config")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = SpotterConfig.from_toml(args.config)
    except (OSError, tomllib.TOMLDecodeError, ConfigurationError) as error:
        parser.error(str(error))

    adapter = CodexAdapter()
    runtime = SpotterRuntime(config=config, adapter=adapter)
    runtime.observe(TraceEvent(kind="session_start"))
    mode = "observation-only" if config.observation_only else "active"
    print(
        f"Spotter ready: adapter={config.main_agent.adapter}, "
        f"reviewer={config.reviewer.model}, mode={mode}"
    )
    return 0
