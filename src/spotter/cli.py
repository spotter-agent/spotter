"""Command-line entry point for the passive-observation prototype."""

import argparse
import json
import tomllib
from collections.abc import Sequence
from pathlib import Path

from spotter.codex import CodexAdapter
from spotter.config import ConfigurationError, SpotterConfig
from spotter.core import SpotterRuntime
from spotter.reviewer import HeuristicReviewer
from spotter.trace import TraceEvent, read_jsonl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Observe a coding-agent trajectory")
    parser.add_argument("--config", type=Path, required=True, help="path to Spotter TOML config")
    parser.add_argument("--replay", type=Path, help="replay normalized JSONL events")
    parser.add_argument("--findings", type=Path, help="write passive findings as JSONL")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = SpotterConfig.from_toml(args.config)
    except (OSError, tomllib.TOMLDecodeError, ConfigurationError) as error:
        parser.error(str(error))

    adapter = CodexAdapter()
    runtime = SpotterRuntime(
        config=config,
        adapter=adapter,
        reviewer=HeuristicReviewer(config.reviewer.model),
    )
    events = read_jsonl(args.replay) if args.replay else [TraceEvent(kind="session_start")]
    try:
        for event in events:
            runtime.observe(event)
        if args.findings:
            _write_findings(args.findings, runtime)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    mode = "observation-only" if config.observation_only else "active"
    print(
        f"Spotter ready: adapter={config.main_agent.adapter}, "
        f"reviewer={config.reviewer.model}, mode={mode}, "
        f"invocations={runtime.invocations}, findings={len(runtime.findings)}"
    )
    return 0


def _write_findings(path: Path, runtime: SpotterRuntime) -> None:
    """Replace output atomically enough for repeatable CLI replay runs."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for finding in runtime.findings:
            output.write(json.dumps(finding.to_dict()) + "\n")
