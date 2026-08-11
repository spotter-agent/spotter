"""Command-line entry point: passive observation and the Codex hook bridge."""

import argparse
import json
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from spotter.codex import CodexAdapter
from spotter.config import ConfigurationError, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.core import SpotterRuntime
from spotter.experiment import results_path, run_experiment, summarize
from spotter.gates import Gate
from spotter.hook import journal_path, run_hook
from spotter.replay import ReplayError, fork, plan_to_json
from spotter.reviewer import review
from spotter.snapshot import (
    SnapshotError,
    StepJournal,
    StepRecord,
    global_lock,
    prune_snapshots,
    referenced_snapshots,
)
from spotter.trace import TraceEvent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Observe a coding-agent trajectory")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["observe", "hook", "analyze", "fork", "prune", "review", "experiment"],
        default="observe",
        help=(
            "observe: validate config and start; hook: Codex hook bridge (JSON on stdin); "
            "analyze: summarize journaled sessions; fork: branch a session at a step; "
            "prune: drop unreferenced refs/spotter snapshots (dry-run without --apply); "
            "review: run the shadow reviewer on a session (records only, injects nothing); "
            "experiment: counterfactual fork pairs — nudge vs control (needs --run to execute)"
        ),
    )
    parser.add_argument("--config", type=Path, help="path to Spotter TOML config")
    parser.add_argument("--session", help="session id (fork; analyze filters to it)")
    parser.add_argument("--step", type=int, help="journal step to branch at (fork)")
    parser.add_argument(
        "--repo", type=Path, help="repo path (prune; fork override when the journal lacks cwd)"
    )
    parser.add_argument("--guidance", help="course-correction text for the forked run (fork)")
    parser.add_argument(
        "--apply", action="store_true", help="prune: actually delete (default is dry-run)"
    )
    parser.add_argument(
        "--window", type=int, default=40, help="review: trajectory tail size fed to the reviewer"
    )
    parser.add_argument("--pairs", type=int, default=1, help="experiment: counterfactual pairs")
    parser.add_argument(
        "--check", help="experiment: success command run in each fork worktree (exit 0 = pass)"
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="experiment: actually execute the 2*pairs agent runs (costs real tokens)",
    )
    return parser


def _load_config(parser: argparse.ArgumentParser, path: Path | None) -> SpotterConfig | None:
    if path is None:
        return None
    try:
        return SpotterConfig.from_toml(path)
    except (OSError, tomllib.TOMLDecodeError, ConfigurationError) as error:
        parser.error(str(error))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = _load_config(parser, args.config)

    if args.command == "hook":
        return _hook_main(config)
    if args.command == "analyze":
        return _analyze_main(args.session)
    if args.command == "prune":
        return _prune_main(args.repo or Path.cwd(), apply=args.apply)
    if args.command == "experiment":
        if not args.session or args.step is None or not args.guidance:
            parser.error("experiment requires --session, --step and --guidance")
        try:
            results = run_experiment(
                args.session,
                args.step,
                args.guidance,
                pairs=args.pairs,
                check=args.check,
                run=args.run,
            )
        except (ReplayError, SnapshotError, OSError, subprocess.SubprocessError) as error:
            print(f"experiment failed: {error}", file=sys.stderr)
            return 1
        print(summarize(results))
        print(f"rows appended to {results_path(args.session, args.step)}")
        return 0
    if args.command == "review":
        if not args.session:
            parser.error("review requires --session")
        if config is None:
            config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig())
        return _review_main(args.session, config, window=args.window)
    if args.command == "fork":
        if not args.session or args.step is None:
            parser.error("fork requires --session and --step")
        try:
            plan = fork(args.session, args.step, repo=args.repo, guidance=args.guidance)
        except (ReplayError, SnapshotError) as error:
            print(f"fork failed: {error}", file=sys.stderr)
            return 1
        print(plan_to_json(plan))
        return 0

    if config is None:
        parser.error("observe requires --config")
    adapter = CodexAdapter()
    gate = Gate(
        forbidden_paths=config.gates.forbidden_paths,
        block_dependency_changes=config.gates.block_dependency_changes,
        root=str(Path.cwd()),
    )
    runtime = SpotterRuntime(config=config, adapter=adapter, gate=gate)
    runtime.observe(TraceEvent(kind="session_start"))
    mode = "observation-only" if config.observation_only else "active"
    print(
        f"Spotter ready: adapter={config.main_agent.adapter}, "
        f"reviewer={config.reviewer.model}, mode={mode}"
    )
    return 0


def _analyze_main(session: str | None) -> int:
    """Summarize journaled sessions: the aggregation step of the FP-review loop.

    This prints samples for a human to label; it deliberately does not label
    anything itself — FP/TP judgment is exactly what must not be automated
    away before the reviewer stage earns that trust.
    """
    sessions_dir = journal_path({"session_id": "probe"}).parent
    journals = sorted(sessions_dir.glob("*.jsonl"))
    if session:
        wanted = journal_path({"session_id": session})
        journals = [j for j in journals if j == wanted]
    if not journals:
        print(f"no journals found under {sessions_dir}", file=sys.stderr)
        return 1
    for journal in journals:
        try:
            records = StepJournal.load(journal)
        except SnapshotError as error:
            print(f"{journal.stem}: unreadable journal ({error})", file=sys.stderr)
            continue
        proposals = [r for r in records if r.event.kind == "tool_proposal"]
        snapshots = sum(1 for r in records if r.snapshot)
        flagged = [r for r in records if r.event.kind in ("gate_shadow_block", "gate_fail_open")]
        verdicts = [r for r in records if r.event.kind == "reviewer_decision"]
        print(
            f"{journal.stem}: steps={len(records)} proposals={len(proposals)} "
            f"snapshots={snapshots} flagged={len(flagged)} reviews={len(verdicts)}"
        )
        for record in verdicts:
            payload = record.event.payload
            print(
                f"  step {record.step:4d} reviewer         "
                f"{payload.get('decision')}/{payload.get('failure_class')} "
                f"conf={payload.get('confidence')}: "
                f"{str(payload.get('reason'))[:100]}"
            )
        for record in flagged:
            trigger = _trigger_for(records, record)
            summary = str(trigger.get("command") or trigger.get("patch") or trigger)
            summary = " ".join(summary.split())[:120]
            print(
                f"  step {record.step:4d} {record.event.kind:17s} "
                f"{record.event.payload.get('rule')}: {summary}"
            )
    return 0


def _trigger_for(records: list[StepRecord], flagged: StepRecord) -> dict[str, object]:
    """Resolve the proposal that triggered a gate event.

    Match by the tool_use_id the gate event carries; journal adjacency is not
    trustworthy under concurrent hook processes. Falls back to the previous
    record only for journals written before the id was recorded.
    """
    wanted = flagged.event.payload.get("tool_use_id")
    if wanted:
        for record in reversed(records[: flagged.step]):
            if (
                record.event.kind == "tool_proposal"
                and record.event.payload.get("tool_use_id") == wanted
            ):
                return record.event.payload
    if flagged.step > 0:
        return records[flagged.step - 1].event.payload
    return {}


def _review_main(session: str, config: SpotterConfig, *, window: int) -> int:
    """Shadow reviewer: judge the trajectory tail, journal the verdict, inject
    nothing. Injection rights are earned later via labeling + fork pairs."""
    journal_file = journal_path({"session_id": session})
    try:
        records = StepJournal.load(journal_file)
    except (OSError, SnapshotError) as error:
        print(f"review failed: {error}", file=sys.stderr)
        return 1
    if not records:
        print("review failed: empty journal", file=sys.stderr)
        return 1
    try:
        decision = review(records, config.reviewer.model, window=window)
    except Exception as error:  # noqa: BLE001 — reviewer failure must stay observable, not fatal
        print(f"review failed: {error}", file=sys.stderr)
        return 1
    StepJournal(journal_file).record(
        TraceEvent(
            "reviewer_decision",
            {
                "decision": decision.decision,
                "failure_class": decision.failure_class,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "model": config.reviewer.model,
                "reviewed_upto": records[-1].step,
                "shadow": True,
            },
        )
    )
    print(
        f"[shadow] {decision.decision} ({decision.failure_class}, "
        f"conf={decision.confidence:.2f}): {decision.reason}"
    )
    return 0


def _prune_main(repo: Path, *, apply: bool) -> int:
    """Drop refs/spotter/steps/* that no journal references (issue #7).

    Dry-run by default: deleting a snapshot is the one spotter operation that
    can destroy fork-ability, so the human confirms with --apply. The global
    lock serializes against a hook that has pinned a ref but not yet
    journaled it.
    """
    sessions_dir = journal_path({"session_id": "probe"}).parent
    try:
        with global_lock():
            referenced = referenced_snapshots(sessions_dir, repo)
            pruned = prune_snapshots(repo, referenced, apply=apply)
    except SnapshotError as error:
        print(f"prune aborted: {error}", file=sys.stderr)
        return 1
    verb = "deleted" if apply else "would delete (pass --apply)"
    print(f"{len(referenced)} snapshots referenced by journals; {verb} {len(pruned)} refs")
    for sha in pruned:
        print(f"  {sha}")
    return 0


def _hook_main(config: SpotterConfig | None) -> int:
    """Read one hook payload from stdin, print a decision if any.

    Always exits 0: any failure here fails open. Breaking the Codex session
    over a supervision bug would be the exact harm Spotter exists to prevent.
    """
    if config is None:
        config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig())
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        output = run_hook(payload, config)
    except Exception as error:  # noqa: BLE001 — deliberate fail-open boundary
        print(f"spotter hook error (failing open): {error}", file=sys.stderr)
        return 0
    if output is not None:
        print(output)
    return 0
