"""Command-line entry point: passive observation and the Codex hook bridge."""

import argparse
import asyncio
import json
import os
import shlex
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from contextlib import nullcontext, redirect_stderr, redirect_stdout, suppress
from dataclasses import replace
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from io import StringIO
from pathlib import Path
from typing import cast

from spotter.app_server_endpoint import display_app_server_endpoint
from spotter.budget import (
    LedgerCorrupt,
    cancel,
    charge,
    settle,
    spend_totals,
)
from spotter.budget import (
    read as read_spend,
)
from spotter.build_identity import current_build_identity, version_line
from spotter.codex import CodexAdapter
from spotter.config import ConfigurationError, ReloadDisposition, SpotterConfig, resolve_config
from spotter.core import SpotterRuntime
from spotter.daemon import (
    DaemonClient,
    DaemonError,
    DaemonStatus,
    ManagedServiceManager,
    ManualServiceManager,
    RuntimeHealth,
    ServiceManager,
)
from spotter.data_inventory import (
    DataInventory,
    DataInventoryError,
    DataResourceInspection,
)
from spotter.doctor import FAIL, INFO, OK, WARN, check_runtime, worst
from spotter.doctor import run as run_doctor
from spotter.effects import (
    EffectResolution,
    EffectResolutionError,
    effect_resolution_event,
    external_effects,
    measure_effect_coverage,
    render_effect_coverage,
)
from spotter.experiment import (
    ExperimentResultError,
    list_forks,
    results_path,
    run_experiment,
    summarize,
)
from spotter.feedback import FeedbackError, add_feedback, load_feedback
from spotter.gates import Gate
from spotter.hook import journal_path, run_hook
from spotter.integration import IntegrationError, IntegrationManager, IntegrationManifest
from spotter.integration_inventory import (
    IntegrationInventory,
    IntegrationInventoryError,
    IntegrationPurger,
    IntegrationResourceInspection,
)
from spotter.labels import LabelError, add_label, valid_session
from spotter.log_registry import LogRegistry, LogRegistryError, LogResourceInspection
from spotter.metrics import (
    AgreementTally,
    Tally,
    agreement_session,
    merge,
    merge_agreement,
    tally_reviewer_continues,
    tally_reviewer_triggers,
    tally_session,
    tally_signal_candidates,
    tally_signal_silence,
    tally_unflagged_proposals,
)
from spotter.observability import (
    SOURCE_AUDIT_RELATIVE_PATH,
    ObservabilityError,
    SourceAuditStore,
    measure_observability,
    render_observability,
)
from spotter.opportunities import OpportunityError, add_opportunity
from spotter.opportunity_metrics import (
    OpportunityTimingReport,
    measure_opportunity_timing,
    merge_opportunity_timing,
    render_opportunity_timing,
)
from spotter.paths import (
    InstallationMethod,
    InstallationProvenance,
    RuntimeLayout,
    installation_provenance,
    secure_dir,
    spotter_home,
)
from spotter.provenance import (
    BlockSummary,
    InterventionSummary,
    summarize_blocks,
    summarize_interventions,
)
from spotter.redact import scan_text
from spotter.replay import (
    ReplayError,
    branch_coverage,
    branch_coverage_to_json,
    fork,
    plan_to_json,
)
from spotter.repository_registry import (
    OwnershipConfidence,
    RepositoryRegistry,
    RepositoryRegistryError,
    RepositoryResourceInspection,
    ResourcePresence,
)
from spotter.retention import (
    ArtifactKey,
    ArtifactRetention,
    RetentionState,
    inspect_snapshot_retention,
    retention_for,
)
from spotter.reviewer import last_usage, review
from spotter.runtime_metrics import (
    ObjectiveOutcomeError,
    measure_objective_outcomes,
    measure_runtime_costs,
    render_objective_outcomes,
    render_runtime_cost_summary,
    render_runtime_costs,
)
from spotter.sampling import (
    SignalSampleError,
    SignalSamplingBatch,
    load_signal_sampling,
    sample_signal_silence,
)
from spotter.snapshot import (
    SnapshotError,
    StepJournal,
    StepRecord,
    global_lock,
    prune_snapshots,
    snapshot_references,
    stale_journals,
)
from spotter.snapshot_pins import SnapshotPinError, SnapshotPinStore
from spotter.task_corpus import (
    PreflightClassification,
    TaskCorpusError,
    TaskPreflight,
    preflight_task_set,
    run_task_batch,
    summarize_task_batch,
    validate_task_set,
)
from spotter.trace import TraceEvent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Observe a coding-agent trajectory")
    parser.add_argument("--version", action="version", version=version_line("spotter"))
    parser.add_argument(
        "command",
        nargs="?",
        choices=[
            "observe",
            "hook",
            "analyze",
            "fork",
            "fork-coverage",
            "prune",
            "purge",
            "pins",
            "review",
            "experiment",
            "label",
            "label-opportunity",
            "sample-signals",
            "metrics",
            "observability",
            "status",
            "doctor",
            "codex",
            "interventions",
            "explain",
            "feedback",
            "effects",
            "daemon",
            "update",
            "setup",
            "teardown",
            "tasks",
            "wrong-nudge",
        ],
        default="observe",
        help=(
            "observe: validate config and start; hook: Codex hook bridge (JSON on stdin); "
            "analyze: summarize journaled sessions; fork: branch a session at a step; "
            "fork-coverage: classify each session proposal for replay eligibility; "
            "prune: drop unreferenced refs/spotter snapshots (dry-run without --apply); "
            "purge: preview resources or remove exact owned snapshots, data, integration, and "
            "log contents; "
            "pins: add, remove, or list durable manual snapshot roots; "
            "review: run the shadow reviewer on a session (records only, injects nothing); "
            "experiment: guidance or identical-neutral fork pairs (needs --run to execute); "
            "label: record a human verdict on a gate flag, signal, reviewer decision, or session; "
            "label-opportunity: record semantic and observable intervention windows; "
            "sample-signals: persist a stratified random frame for detector misses; "
            "metrics: gate and signal precision, misses, reviewer precision, and ceiling; "
            "observability: compare Hook/App Server Trace IR and source-adapter coverage; "
            "tasks: validate or preflight a frozen task set without running agents; "
            "wrong-nudge: run paid susceptibility arms or report durable results; "
            "status: what Spotter is storing, and whether it is actually running; "
            "doctor: verify supervision end to end (non-zero exit when broken); "
            "interventions: list recent BLOCK/VERIFY/NUDGE/INTERRUPT lifecycle records; "
            "explain: inspect one intervention with --intervention-id; "
            "feedback: append structured human feedback to an intervention; "
            "effects: list or explicitly resolve external effects for a session; "
            "daemon: manually start, stop, restart, reload, or inspect spotterd; "
            "update: print package-manager-owned update guidance without modifying files; "
            "setup/teardown: manage the owned Codex integration"
        ),
    )
    parser.add_argument(
        "target",
        nargs="?",
        choices=[
            "start",
            "stop",
            "restart",
            "reload",
            "status",
            "codex",
            "validate",
            "preflight",
            "run",
            "list",
            "resolve",
            "add",
            "remove",
            "report",
            "persist",
        ],
        help="daemon lifecycle action, integration target, pin/task action, or experiment action",
    )
    parser.add_argument("subject", nargs="?", help="set manifest or result JSONL path")
    parser.add_argument("--resume", type=Path, help="tasks run: resume this task-batch JSONL")
    parser.add_argument(
        "--annotations",
        type=Path,
        help="wrong-nudge report: optional annotation JSONL",
    )
    parser.add_argument(
        "--persistence-results",
        type=Path,
        help="wrong-nudge report: optional persistence result JSONL",
    )
    parser.add_argument(
        "--persistence-annotations",
        type=Path,
        help="wrong-nudge report: optional persistence annotation JSONL",
    )
    parser.add_argument("--task-set", type=Path, help="wrong-nudge run: frozen task-set manifest")
    parser.add_argument("--wrong-nudge-id", help="wrong-nudge run: exact frozen corpus item")
    parser.add_argument(
        "--endpoint",
        help="setup or wrong-nudge run/persist: Codex App Server WebSocket URL",
    )
    parser.add_argument(
        "--capture-replay-sources",
        action="store_true",
        help="tasks run: journal and snapshot each arm as a future fork source",
    )
    parser.add_argument("--config", type=Path, help="path to Spotter TOML config")
    parser.add_argument(
        "--session", help="session id (fork/effects; analyze/metrics/observability filter to it)"
    )
    parser.add_argument(
        "--intervention-id",
        "--supervision-id",
        dest="intervention_id",
        help="explain/feedback: stable Spotter supervision id",
    )
    parser.add_argument("--category", help="feedback: structured feedback category")
    parser.add_argument("--effect-id", help="effects resolve: stable target effect id")
    parser.add_argument(
        "--related-effect-id", help="effects resolve: stable compensating effect id"
    )
    parser.add_argument(
        "--resolution",
        choices=[
            "reversed",
            "compensated",
            "reconciled_present",
            "reconciled_absent",
            "still_unresolved",
        ],
        help="effects resolve: explicit resolution state",
    )
    parser.add_argument("--step", type=int, help="journal step to branch at (fork)")
    parser.add_argument(
        "--repo",
        type=Path,
        help="repo path (prune/pins; fork override when the journal lacks cwd)",
    )
    parser.add_argument("--snapshot", help="pins add: exact Spotter-owned snapshot SHA")
    parser.add_argument("--pin-id", help="pins remove: durable manual pin id")
    parser.add_argument("--guidance", help="course-correction text for fork/experiment")
    parser.add_argument(
        "--apply", action="store_true", help="prune: actually delete (default is dry-run)"
    )
    parser.add_argument(
        "--all",
        dest="purge_all",
        action="store_true",
        help="purge: include every registered repository resource",
    )
    parser.add_argument(
        "--snapshots",
        dest="purge_snapshots",
        action="store_true",
        help="purge: remove exact unreferenced Spotter-owned worktrees and snapshot refs",
    )
    parser.add_argument(
        "--data",
        dest="purge_data",
        action="store_true",
        help="purge: remove schema-proven durable user data",
    )
    parser.add_argument(
        "--integration",
        dest="purge_integration",
        action="store_true",
        help="purge: remove exact integration-owned resources",
    )
    parser.add_argument(
        "--logs",
        dest="purge_logs",
        action="store_true",
        help="purge: clear exact Spotter-owned log contents",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="purge: emit machine-readable JSON",
    )
    parser.add_argument(
        "--forks", action="store_true", help="prune: also remove orphaned fork worktrees"
    )
    parser.add_argument(
        "--journals",
        action="store_true",
        help="prune: also remove journals past --max-age-days, with their snapshots",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        help=(
            "prune: also expire snapshots older than N days even when a journal "
            "references them — bounded disk in exchange for losing fork-ability"
        ),
    )
    parser.add_argument(
        "--window", type=int, default=40, help="review: trajectory tail size fed to the reviewer"
    )
    parser.add_argument("--pairs", type=int, default=1, help="experiment: counterfactual pairs")
    parser.add_argument(
        "--neutral",
        action="store_true",
        help="experiment: run identical neutral arms to measure the replay noise floor",
    )
    parser.add_argument("--model", help="review/tasks run/experiment: pin the Codex model")
    parser.add_argument(
        "--reasoning-effort", help="tasks run/experiment: pin the Codex model reasoning effort"
    )
    parser.add_argument(
        "--reservation",
        help="review: token for a budget slot the caller already reserved (internal)",
    )
    parser.add_argument("--review-job-id", help=argparse.SUPPRESS)
    parser.add_argument("--integration-generation", help=argparse.SUPPRESS)
    parser.add_argument(
        "--check", help="experiment: success command run in each fork worktree (exit 0 = pass)"
    )
    parser.add_argument(
        "--environment-resource",
        action="append",
        default=[],
        help=(
            "fork/experiment: relative non-secret file or directory that must survive restore "
            "(repeatable)"
        ),
    )
    parser.add_argument(
        "--environment-variable",
        action="append",
        default=[],
        help=(
            "fork/experiment: non-secret environment variable whose value hash must stay stable "
            "(repeatable)"
        ),
    )
    parser.add_argument(
        "--environment-venv-or-cache",
        action="append",
        default=[],
        help=(
            "fork/experiment: relative non-secret virtualenv or cache directory whose loss "
            "must be classified separately (repeatable)"
        ),
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="experiment: actually execute the 2*pairs agent runs (costs real tokens)",
    )
    parser.add_argument(
        "--verdict",
        help=(
            "label: tp|fp|unclear for a flag/signal/intervention, "
            "miss|tn|unclear for an unflagged proposal or reviewer CONTINUE, "
            "visible|invisible|unclear for a session"
        ),
    )
    parser.add_argument(
        "--note",
        default="",
        help="label/label-opportunity: rationale; effects resolve: explicit evidence",
    )
    parser.add_argument(
        "--rater",
        help="label/label-opportunity: stable human rater identity (defaults to the OS account)",
    )
    parser.add_argument("--opportunity-id", help="label-opportunity: stable opportunity identity")
    parser.add_argument(
        "--semantic-earliest", type=int, help="label-opportunity: earliest warranted journal step"
    )
    parser.add_argument(
        "--semantic-latest", type=int, help="label-opportunity: latest warranted journal step"
    )
    parser.add_argument(
        "--observable-earliest",
        type=int,
        help="label-opportunity: earliest observably warranted journal step",
    )
    parser.add_argument(
        "--observable-latest",
        type=int,
        help="label-opportunity: latest observably warranted journal step",
    )
    parser.add_argument(
        "--required-evidence",
        action="append",
        type=int,
        default=[],
        help="label-opportunity: required evidence journal step (repeatable)",
    )
    parser.add_argument(
        "--signal-type",
        help="sample-signals: detector family; label: sampled detector-negative scope",
    )
    parser.add_argument(
        "--event-kind",
        action="append",
        default=[],
        help="sample-signals: eligible source-event stratum (repeatable)",
    )
    parser.add_argument(
        "--sample-rate",
        type=float,
        help="sample-signals: deterministic inclusion probability in (0, 1]",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="experiment: keep forked worktrees (rollouts are always retained)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="setup/purge: inspect and print without mutation"
    )
    parser.add_argument(
        "--portable",
        action="store_true",
        help="setup: start spotterd without registering a login service",
    )
    return parser


def _load_config(parser: argparse.ArgumentParser, path: Path | None) -> SpotterConfig:
    try:
        resolved = resolve_config(repository=Path.cwd(), explicit_path=path)
    except (OSError, tomllib.TOMLDecodeError, ConfigurationError) as error:
        parser.error(str(error))
    for diagnostic in resolved.diagnostics:
        print(f"spotter: {diagnostic}", file=sys.stderr)
    return resolved.config


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "codex":
        return _codex_main(raw_args[1:])
    parser = build_parser()
    args = parser.parse_args(raw_args)

    if args.command == "hook":
        # Config is loaded inside the hook's own fail-open boundary rather than
        # by the shared path below. parser.error() exits 2, and 2 is how a
        # PreToolUse hook says "deny" — so a malformed config file would not
        # merely disable supervision, it would block every tool call in the
        # session. Unsupervised beats blocked.
        return _hook_main(None, args.config, args.integration_generation)

    if args.command == "daemon":
        if args.target is None or args.target == "codex":
            parser.error("daemon requires start, stop, restart, reload, or status")
        return _daemon_main(args.target)
    if args.command == "update":
        if args.target is not None:
            parser.error("update does not accept a target")
        return _update_main()
    if args.command in {"setup", "teardown"}:
        if args.target != "codex":
            parser.error(f"{args.command} requires the codex target")
        if (
            args.purge_all
            or args.purge_snapshots
            or args.purge_data
            or args.purge_integration
            or args.purge_logs
            or args.json_output
        ):
            parser.error(
                "--all, --snapshots, --data, --integration, --logs, and --json require purge"
            )
        if args.command == "teardown" and (args.dry_run or args.portable or args.endpoint):
            parser.error("--dry-run, --portable, and --endpoint are only supported by setup")
        return _integration_main(
            args.command,
            config_path=args.config,
            portable=args.portable,
            dry_run=args.dry_run,
            app_server_endpoint=args.endpoint,
        )
    if args.command == "purge":
        if (
            sum(
                (
                    args.purge_all,
                    args.purge_snapshots,
                    args.purge_data,
                    args.purge_integration,
                    args.purge_logs,
                )
            )
            != 1
        ):
            parser.error(
                "purge requires exactly one of --all, --snapshots, --data, --integration, or --logs"
            )
        if args.target is not None or args.portable:
            parser.error("purge accepts only a scope, optional --dry-run, and optional --json")
        if args.apply:
            parser.error("purge is destructive without --dry-run; do not pass --apply")
        if args.purge_data:
            return _purge_data_main(json_output=args.json_output, apply=not args.dry_run)
        if args.purge_integration:
            return _purge_integration_main(json_output=args.json_output, apply=not args.dry_run)
        if args.purge_logs:
            return _purge_logs_main(json_output=args.json_output, apply=not args.dry_run)
        if args.purge_all:
            return _purge_all_main(json_output=args.json_output, apply=not args.dry_run)
        return _purge_main(
            json_output=args.json_output,
            snapshot_scope=args.purge_snapshots,
            apply=not args.dry_run,
            remove_registry=False,
        )
    if args.command == "pins":
        if args.target not in {"add", "remove", "list"}:
            parser.error("pins requires add, remove, or list")
        if args.target == "add" and not args.snapshot:
            parser.error("pins add requires --snapshot")
        if args.target == "remove" and not args.pin_id:
            parser.error("pins remove requires --pin-id")
        return _pins_main(
            args.target,
            repo=args.repo or Path.cwd(),
            snapshot=args.snapshot,
            pin_id=args.pin_id,
        )
    if args.command == "tasks":
        if args.target not in {"validate", "preflight", "run"} or not args.subject:
            parser.error("tasks requires: spotter tasks validate|preflight|run <set.toml>")
        if args.target == "run":
            if not args.run:
                parser.error("tasks run requires --run because it executes paid agent arms")
            if not args.guidance:
                parser.error("tasks run requires --guidance")
            try:
                output, task_results = run_task_batch(
                    Path(args.subject),
                    args.guidance,
                    resume=args.resume,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    keep_artifacts=args.keep_artifacts,
                    capture_replay_sources=args.capture_replay_sources,
                )
            except TaskCorpusError as error:
                print(f"task batch failed: {error}", file=sys.stderr)
                return 1
            print(summarize_task_batch(task_results))
            print(f"results written to {output}")
            return 0
        if args.resume:
            parser.error("--resume requires tasks run")
        preflight_results: tuple[TaskPreflight, ...]
        try:
            if args.target == "validate":
                task_set = validate_task_set(Path(args.subject))
                preflight_results = ()
            else:
                task_set, preflight_results = preflight_task_set(Path(args.subject))
        except TaskCorpusError as error:
            print(f"task validation failed: {error}", file=sys.stderr)
            return 1
        print(
            f"validated {task_set.task_set_id} v{task_set.version} "
            f"({task_set.split}): {len(task_set.tasks)} task(s)"
        )
        for result in preflight_results:
            print(f"  {result.task_id}: {result.classification}")
        if any(
            result.classification != PreflightClassification.READY for result in preflight_results
        ):
            return 1
        return 0
    if args.command == "wrong-nudge":
        if args.target == "persist":
            from spotter.wrong_nudge_metrics import (
                WrongNudgeMetricsError,
                load_wrong_nudge_results,
            )
            from spotter.wrong_nudge_persistence import (
                WrongNudgePersistenceError,
                run_wrong_nudge_persistence_cohort,
            )

            if not args.subject or not args.endpoint:
                parser.error("wrong-nudge persist requires <results.jsonl> and --endpoint")
            if not args.run:
                parser.error(
                    "wrong-nudge persist requires --run because it executes paid follow-up turns"
                )
            if (
                args.annotations
                or args.persistence_results
                or args.persistence_annotations
                or args.task_set
                or args.wrong_nudge_id
            ):
                parser.error(
                    "--annotations, --persistence-results, --persistence-annotations, "
                    "--task-set, and --wrong-nudge-id do not apply to wrong-nudge persist"
                )
            if (
                args.session
                or args.step is not None
                or args.repo
                or args.environment_resource
                or args.environment_variable
                or args.environment_venv_or_cache
            ):
                parser.error(
                    "wrong-nudge persist uses source identities and worktrees from the result file"
                )
            try:
                source_results = load_wrong_nudge_results(Path(args.subject))
                output, persistence_results = run_wrong_nudge_persistence_cohort(
                    source_results, args.endpoint
                )
            except (
                ExperimentResultError,
                WrongNudgeMetricsError,
                WrongNudgePersistenceError,
                OSError,
            ) as error:
                print(f"wrong-nudge persistence failed: {error}", file=sys.stderr)
                return 1
            print(f"completed {len(persistence_results)} persistence follow-up(s)")
            print(f"results written to {output}")
            return 0

        if args.target == "run":
            from spotter.wrong_nudge_corpus import WrongNudgeCorpusError
            from spotter.wrong_nudge_experiment import (
                WrongNudgeExperimentError,
                run_wrong_nudge_experiment,
            )

            if not args.subject:
                parser.error("wrong-nudge run requires a wrong-nudge set manifest")
            if not args.run:
                parser.error("wrong-nudge run requires --run because it executes paid agent arms")
            if (
                not args.task_set
                or not args.wrong_nudge_id
                or not args.session
                or args.step is None
                or not args.endpoint
            ):
                parser.error(
                    "wrong-nudge run requires --task-set, --wrong-nudge-id, --session, --step, "
                    "and --endpoint"
                )
            if args.annotations or args.persistence_results or args.persistence_annotations:
                parser.error(
                    "--annotations, --persistence-results, and --persistence-annotations "
                    "require wrong-nudge report"
                )
            try:
                output, wrong_nudge_results = run_wrong_nudge_experiment(
                    Path(args.subject),
                    args.task_set,
                    args.wrong_nudge_id,
                    args.session,
                    args.step,
                    args.endpoint,
                    repo=args.repo,
                    environment_resources=tuple(args.environment_resource),
                    environment_variables=tuple(args.environment_variable),
                    environment_venv_or_cache=tuple(args.environment_venv_or_cache),
                )
            except (
                WrongNudgeCorpusError,
                WrongNudgeExperimentError,
                TaskCorpusError,
                ReplayError,
                SnapshotError,
                OSError,
            ) as error:
                print(f"wrong-nudge run failed: {error}", file=sys.stderr)
                return 1
            print(f"completed {len(wrong_nudge_results)} wrong-nudge arm(s)")
            print(f"results written to {output}")
            return 0

        from spotter.wrong_nudge_annotations import (
            WrongNudgeAnnotationError,
            load_wrong_nudge_annotations,
        )
        from spotter.wrong_nudge_metrics import (
            WrongNudgeMetricsError,
            load_wrong_nudge_results,
            measure_wrong_nudge_susceptibility,
            render_wrong_nudge_report,
        )
        from spotter.wrong_nudge_persistence import (
            WrongNudgePersistenceError,
            load_wrong_nudge_persistence_results,
        )
        from spotter.wrong_nudge_persistence_annotations import (
            WrongNudgePersistenceAnnotationError,
            load_wrong_nudge_persistence_annotations,
        )

        if args.target != "report" or not args.subject:
            parser.error("wrong-nudge requires run, persist, or report <path>")
        if args.task_set or args.wrong_nudge_id:
            parser.error("--task-set and --wrong-nudge-id require wrong-nudge run")
        if args.endpoint or args.run:
            parser.error("--endpoint and --run require wrong-nudge run or persist")
        if args.persistence_annotations and not args.persistence_results:
            parser.error("--persistence-annotations requires --persistence-results")
        try:
            wrong_nudge_results = load_wrong_nudge_results(Path(args.subject))
            wrong_nudge_annotations = (
                load_wrong_nudge_annotations(args.annotations) if args.annotations else ()
            )
            persistence_results = (
                load_wrong_nudge_persistence_results(args.persistence_results)
                if args.persistence_results
                else ()
            )
            persistence_annotations = (
                load_wrong_nudge_persistence_annotations(args.persistence_annotations)
                if args.persistence_annotations
                else ()
            )
            report = measure_wrong_nudge_susceptibility(
                wrong_nudge_results,
                wrong_nudge_annotations,
                persistence_results,
                persistence_annotations,
            )
        except (
            WrongNudgeMetricsError,
            WrongNudgeAnnotationError,
            WrongNudgePersistenceError,
            WrongNudgePersistenceAnnotationError,
        ) as error:
            print(f"wrong-nudge report failed: {error}", file=sys.stderr)
            return 1
        print(render_wrong_nudge_report(report))
        return 0
    if args.target is not None and args.command != "effects":
        parser.error(
            "the second positional argument requires daemon, setup, teardown, pins, tasks, or "
            "wrong-nudge"
        )
    if args.resume:
        parser.error("--resume requires tasks run")
    if args.annotations or args.persistence_results or args.persistence_annotations:
        parser.error(
            "--annotations, --persistence-results, and --persistence-annotations require "
            "wrong-nudge report"
        )
    if args.task_set or args.wrong_nudge_id or args.endpoint:
        parser.error(
            "--task-set and --wrong-nudge-id require wrong-nudge run; --endpoint requires "
            "wrong-nudge run or persist"
        )
    if args.dry_run or args.portable:
        parser.error("--dry-run and --portable require setup")
    if (
        args.purge_all
        or args.purge_snapshots
        or args.purge_data
        or args.purge_integration
        or args.purge_logs
        or args.json_output
    ):
        parser.error("--all, --snapshots, --data, --integration, --logs, and --json require purge")

    config = _load_config(parser, args.config)
    # One boundary check for every command that names a session: sanitizing
    # instead of rejecting maps distinct ids onto one file ("a/b" -> "a_b").
    if args.session is not None and not valid_session(args.session):
        parser.error(f"--session {args.session!r} is not a valid session id")
    if args.command == "effects":
        if args.target not in {"list", "resolve"} or not args.session:
            parser.error("effects requires: spotter effects list|resolve --session SESSION")
        if args.target == "resolve" and (not args.effect_id or not args.resolution):
            parser.error("effects resolve requires --effect-id and --resolution")
        return _effects_main(
            args.session,
            args.target,
            effect_id=args.effect_id,
            resolution=args.resolution,
            related_effect_id=args.related_effect_id,
            evidence=args.note,
        )
    if args.command == "analyze":
        return _analyze_main(args.session)
    if args.command == "interventions":
        return _interventions_main()
    if args.command == "explain":
        if not args.intervention_id:
            parser.error("explain requires --intervention-id")
        return _explain_main(args.intervention_id)
    if args.command == "feedback":
        if not args.intervention_id or not args.category:
            parser.error("feedback requires --intervention-id and --category")
        return _feedback_main(args.intervention_id, args.category, args.note, args.rater)
    if args.command == "fork-coverage":
        if not args.session:
            parser.error("fork-coverage requires --session")
        try:
            print(branch_coverage_to_json(branch_coverage(args.session)))
        except (OSError, SnapshotError, LabelError) as error:
            print(f"fork coverage failed: {error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "prune":
        if args.max_age_days is not None and args.max_age_days < 1:
            parser.error("--max-age-days must be >= 1")
        return _prune_main(
            args.repo or Path.cwd(),
            apply=args.apply,
            max_age_days=args.max_age_days,
            forks=args.forks,
            journals=args.journals,
        )
    if args.command == "status":
        return _status_main()
    if args.command == "doctor":
        return _doctor_main(args.config)
    if args.command == "experiment":
        if not args.session or args.step is None or (not args.neutral and not args.guidance):
            parser.error("experiment requires --session, --step and either --guidance or --neutral")
        if args.neutral and args.guidance:
            parser.error("--neutral and --guidance cannot be used together")
        if args.pairs < 1:
            parser.error("--pairs must be >= 1")
        try:
            results = run_experiment(
                args.session,
                args.step,
                args.guidance,
                pairs=args.pairs,
                check=args.check,
                run=args.run,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                keep_artifacts=args.keep_artifacts,
                neutral=args.neutral,
                environment_resources=tuple(args.environment_resource),
                environment_variables=tuple(args.environment_variable),
                environment_venv_or_cache=tuple(args.environment_venv_or_cache),
            )
        except (
            ExperimentResultError,
            ReplayError,
            SnapshotError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            print(f"experiment failed: {error}", file=sys.stderr)
            return 1
        print(summarize(results))
        print(f"rows appended to {results_path(args.session, args.step)}")
        return 0
    if args.command == "label":
        if not args.session or not args.verdict:
            parser.error("label requires --session and --verdict")
        return _label_main(
            args.session,
            args.step,
            args.verdict,
            args.note,
            args.rater,
            args.signal_type,
        )
    if args.command == "label-opportunity":
        required = (
            args.session,
            args.opportunity_id,
            args.semantic_earliest,
            args.semantic_latest,
            args.observable_earliest,
            args.observable_latest,
        )
        if any(value is None for value in required) or not args.required_evidence or not args.note:
            parser.error(
                "label-opportunity requires --session, --opportunity-id, semantic/observable "
                "earliest/latest steps, --required-evidence, and --note"
            )
        assert args.session is not None
        assert args.opportunity_id is not None
        assert args.semantic_earliest is not None
        assert args.semantic_latest is not None
        assert args.observable_earliest is not None
        assert args.observable_latest is not None
        return _label_opportunity_main(
            args.session,
            args.opportunity_id,
            args.semantic_earliest,
            args.semantic_latest,
            args.observable_earliest,
            args.observable_latest,
            tuple(args.required_evidence),
            args.note,
            args.rater,
        )
    if args.command == "sample-signals":
        if (
            not args.session
            or not args.signal_type
            or not args.event_kind
            or args.sample_rate is None
        ):
            parser.error(
                "sample-signals requires --session, --signal-type, --event-kind, and --sample-rate"
            )
        return _sample_signals_main(
            args.session,
            args.signal_type,
            tuple(args.event_kind),
            args.sample_rate,
        )
    if args.command == "metrics":
        return _metrics_main(args.session)
    if args.command == "observability":
        return _observability_main(args.session)
    if args.command == "review":
        if not args.session:
            parser.error("review requires --session")
        if args.model:
            config = replace(config, reviewer=replace(config.reviewer, model=args.model))
        return _review_main(
            args.session,
            config,
            window=args.window,
            reservation=args.reservation,
            review_job_id=args.review_job_id,
        )
    if args.command == "fork":
        if not args.session or args.step is None:
            parser.error("fork requires --session and --step")
        try:
            plan = fork(
                args.session,
                args.step,
                repo=args.repo,
                guidance=args.guidance,
                environment_resources=tuple(args.environment_resource),
                environment_variables=tuple(args.environment_variable),
                environment_venv_or_cache=tuple(args.environment_venv_or_cache),
            )
        except (ReplayError, SnapshotError) as error:
            print(f"fork failed: {error}", file=sys.stderr)
            return 1
        print(plan_to_json(plan))
        return 0

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


def _span_of(records: list[StepRecord]) -> str:
    """Wall-clock span of a session, or nothing when it cannot be known.

    Records written before timestamps existed report as unknown rather than
    contributing a zero, because a zero here is indistinguishable from a
    session that really was instantaneous (issue #55).
    """
    stamped = [r.at for r in records if r.at is not None]
    if not stamped:
        return "" if not records else " span=unknown"
    span = max(stamped) - min(stamped)
    missing = len(records) - len(stamped)
    suffix = f" (+{missing} untimed)" if missing else ""
    return f" span={span / 60:.1f}m{suffix}"


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
    experiment_dir = spotter_home() / "experiments"
    objective_paths = sorted(experiment_dir.rglob("*.jsonl")) if experiment_dir.exists() else []
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
            f"{_span_of(records)}"
        )
        report = measure_runtime_costs([(records, journal.stat().st_size)])
        print(f"  {render_runtime_cost_summary(report)}")
        try:
            objective_report = measure_objective_outcomes(objective_paths, session_id=journal.stem)
        except ObjectiveOutcomeError as error:
            print(f"{journal.stem}: objective outcome join unavailable ({error})", file=sys.stderr)
        else:
            if objective_report.arms:
                rendered = render_objective_outcomes(objective_report, session_id=journal.stem)
                print("\n".join(f"  {line}" for line in rendered.splitlines()))
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


def _effects_main(
    session: str,
    action: str,
    *,
    effect_id: str | None,
    resolution: str | None,
    related_effect_id: str | None,
    evidence: str,
) -> int:
    journal_file = journal_path({"session_id": session})
    try:
        records = StepJournal.load(journal_file)
    except (OSError, SnapshotError) as error:
        print(f"effects unavailable: {error}", file=sys.stderr)
        return 1
    effects = external_effects(records)
    if action == "list":
        if not effects:
            print(f"no external effects for {session}")
            return 0
        for effect in effects:
            state = effect.get("resolution") or effect.get("outcome") or "unknown"
            resolved = "resolved" if effect.get("resolved") is True else "unresolved"
            print(
                f"{effect.get('effect_id', 'legacy')} {resolved} {state} "
                f"{effect.get('semantic_operation') or effect.get('kind')} "
                f"resource={effect.get('resource')}"
            )
        return 0

    targets = {
        value
        for effect in effects
        if effect.get("correlation_quality") == "exact"
        and (value := effect.get("effect_id"))
        and isinstance(value, str)
    }
    if effect_id not in targets:
        print(
            f"effect resolution refused: exact effect {effect_id!r} was not found", file=sys.stderr
        )
        return 1
    if related_effect_id is not None and related_effect_id not in targets:
        print(
            f"effect resolution refused: related exact effect {related_effect_id!r} was not found",
            file=sys.stderr,
        )
        return 1
    if resolution is None:
        print("effect resolution refused: resolution is required", file=sys.stderr)
        return 1
    try:
        event = effect_resolution_event(
            effect_id or "",
            cast(EffectResolution, resolution),
            evidence,
            related_effect_id=related_effect_id,
        )
        StepJournal(journal_file).record(event)
    except (OSError, EffectResolutionError) as error:
        print(f"effect resolution refused: {error}", file=sys.stderr)
        return 1
    print(f"recorded {resolution} resolution for {effect_id}")
    return 0


def _intervention_history() -> tuple[InterventionSummary, ...]:
    sessions_dir = journal_path({"session_id": "probe"}).parent
    records: list[StepRecord] = []
    for journal in sorted(sessions_dir.glob("app-server-*.jsonl")):
        records.extend(StepJournal.load(journal, repair_tail=True))
    return summarize_interventions(records)


def _block_history() -> tuple[BlockSummary, ...]:
    sessions_dir = journal_path({"session_id": "probe"}).parent
    records: list[StepRecord] = []
    for journal in sorted(sessions_dir.glob("*.jsonl")):
        records.extend(StepJournal.load(journal, repair_tail=True))
    return summarize_blocks(records)


def _interventions_main() -> int:
    try:
        interventions = _intervention_history()
        blocks = _block_history()
    except (OSError, SnapshotError) as error:
        print(f"interventions unavailable: {error}", file=sys.stderr)
        return 1
    if not interventions and not blocks:
        print("no supervision actions recorded", file=sys.stderr)
        return 1
    for intervention in interventions:
        print(
            f"{intervention.intervention_id}  {intervention.action or 'UNKNOWN':9s}  "
            f"{intervention.status:24s}  thread={intervention.thread_id or 'unknown'} "
            f"turn={intervention.turn_id or 'unknown'}"
        )
    for block in blocks:
        status = "ENFORCED" if block.enforced else "SHADOW"
        print(
            f"{block.supervision_event_id}  BLOCK      {status:24s}  "
            f"session={block.session_id or 'unknown'} rule={block.rule or 'unknown'}"
        )
    return 0


def _explain_main(intervention_id: str) -> int:
    try:
        intervention = next(
            (item for item in _intervention_history() if item.intervention_id == intervention_id),
            None,
        )
        block = next(
            (item for item in _block_history() if item.supervision_event_id == intervention_id),
            None,
        )
    except (OSError, SnapshotError) as error:
        print(f"intervention explanation unavailable: {error}", file=sys.stderr)
        return 1
    if intervention is None:
        if block is None:
            print(f"supervision action {intervention_id!r} was not found", file=sys.stderr)
            return 1
        return _explain_block(block)

    print(f"Intervention\n  {intervention.intervention_id}")
    print(f"Action\n  {intervention.action or 'UNKNOWN'}")
    epoch = (
        intervention.connection_epoch if intervention.connection_epoch is not None else "unknown"
    )
    print(
        "Target\n"
        f"  thread={intervention.thread_id or 'unknown'} "
        f"turn={intervention.turn_id or 'unknown'} connection_epoch={epoch}"
    )
    print("Why Spotter acted (model judgment, not ground truth)")
    confidence = intervention.confidence if intervention.confidence is not None else "unknown"
    print(f"  class={intervention.failure_class or 'unknown'} confidence={confidence}")
    print(f"  hypothesis={_bounded(intervention.hypothesis)}")
    print(f"  reason={_bounded(intervention.reason)}")
    print("Observed evidence references")
    print(f"  signals={','.join(intervention.signal_ids) or 'unavailable'}")
    print(f"  events={','.join(intervention.evidence_event_ids) or 'unavailable'}")
    print("Delivery")
    suffix = f" ({intervention.status_reason})" if intervention.status_reason else ""
    print(f"  {intervention.status}{suffix}")
    return _print_feedback(intervention_id)


def _explain_block(block: BlockSummary) -> int:
    status = "ENFORCED" if block.enforced else "SHADOW_ONLY"
    print(f"Supervision action\n  {block.supervision_event_id}")
    print(f"Action\n  BLOCK ({status})")
    print(f"Target\n  session={block.session_id or 'unknown'} journal_step={block.step}")
    print("Observed policy facts")
    print(f"  rule={block.rule or 'unknown'} version={block.rule_version or 'unknown'}")
    print(f"  reason={_bounded(block.reason)}")
    print(f"  tool={block.tool or 'unknown'} resource={block.resource or 'unknown'}")
    print(
        f"  reversibility={block.reversibility_class or 'unknown'} "
        f"effect={block.effect_kind or 'unknown'}"
    )
    print("Safe next action")
    print(f"  {block.remedy}")
    return _print_feedback(block.supervision_event_id)


def _print_feedback(supervision_event_id: str) -> int:
    try:
        feedback = load_feedback(supervision_event_id)
    except FeedbackError as error:
        print(f"human feedback unavailable: {error}", file=sys.stderr)
        return 1
    print("Human feedback (evaluation evidence, not ground truth)")
    if not feedback:
        print("  none")
    for item in feedback:
        note = f" — {_bounded(item.note)}" if item.note else ""
        print(f"  {item.category} by {item.rater} at {item.created_at}{note}")
    return 0


def _feedback_main(intervention_id: str, category: str, note: str, rater: str | None) -> int:
    try:
        known = any(
            item.intervention_id == intervention_id for item in _intervention_history()
        ) or any(item.supervision_event_id == intervention_id for item in _block_history())
    except (OSError, SnapshotError) as error:
        print(f"feedback refused: intervention history unavailable ({error})", file=sys.stderr)
        return 1
    if not known:
        print(f"feedback refused: intervention {intervention_id!r} was not found", file=sys.stderr)
        return 1
    try:
        feedback = add_feedback(
            intervention_id,
            category,
            note=note,
            rater=rater,
        )
    except (FeedbackError, OSError) as error:
        print(f"feedback refused: {error}", file=sys.stderr)
        return 1
    print(
        f"recorded {feedback.category} feedback {feedback.feedback_id} "
        f"for {feedback.supervision_event_id}"
    )
    return 0


def _bounded(value: str | None) -> str:
    return " ".join((value or "unavailable").split())[:600]


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


def _constraints_of(config: SpotterConfig) -> list[str]:
    """Configured constraints the reviewer is expected to judge against.

    Asking a reviewer about scope drift while withholding the constraints is
    how the previous prompt produced confident answers about nothing.
    """
    constraints: list[str] = []
    if config.gates.forbidden_paths:
        constraints.append(f"must not touch paths matching {list(config.gates.forbidden_paths)}")
    if config.gates.block_dependency_changes:
        constraints.append("must not change dependency manifests")
    return constraints


def _review_main(
    session: str,
    config: SpotterConfig,
    *,
    window: int,
    reservation: str | None = None,
    review_job_id: str | None = None,
) -> int:
    """Shadow reviewer: judge the trajectory tail, journal the verdict, inject
    nothing. Injection rights are earned later via labeling + fork pairs."""
    journal_file = journal_path({"session_id": session})
    # In-flight guard: a slow model call must not stack duplicate paid reviews
    # of the same session when the next cadence tick arrives.
    lock_file = journal_file.with_suffix(".review.lock")
    lock = lock_file.open("w")
    try:
        flock(lock, LOCK_EX | LOCK_NB)
    except OSError:
        # Losing this race is ordinary: two cadence children for one session
        # both reserve, and this one never reviews. Hand the slot back.
        cancel(session, reservation)
        print(f"review already in flight for {session}; skipping", file=sys.stderr)
        lock.close()
        return 0
    try:
        records = StepJournal.load(journal_file)
    except (OSError, SnapshotError) as error:
        cancel(session, reservation)  # nothing was reviewed and nothing was spent
        print(f"review failed: {error}", file=sys.stderr)
        return 1
    if not records:
        cancel(session, reservation)
        print("review failed: empty journal", file=sys.stderr)
        return 1
    queued_at = next(
        (
            record.at
            for record in reversed(records)
            if record.event.kind == "review_job_queued"
            and record.event.payload.get("review_job_id") == review_job_id
        ),
        None,
    )
    review_trigger = next(
        (
            record.event.payload.get("review_trigger")
            for record in reversed(records)
            if record.event.kind == "review_job_queued"
            and record.event.payload.get("review_job_id") == review_job_id
            and isinstance(record.event.payload.get("review_trigger"), str)
        ),
        "manual"
        if review_job_id is None
        else "periodic"
        if review_job_id.startswith("proposal:")
        else "unknown",
    )
    try:
        # Check the ledger before spending, not after: the manual path calls
        # the model first, so a corrupt ledger would otherwise pay for a review
        # and then overwrite the history proving a ceiling was hit.
        read_spend(session)
    except LedgerCorrupt as error:
        print(f"review refused: {error}", file=sys.stderr)
        return 1
    started = StepJournal(journal_file).record(
        TraceEvent(
            "review_inference_started",
            {
                "review_job_id": review_job_id,
                "review_trigger": review_trigger,
                "queue_ms": max(0.0, (time.time() - queued_at) * 1000)
                if queued_at is not None
                else None,
            },
        )
    )
    constraints = _constraints_of(config)
    try:
        decision, digest = review(
            records, config.reviewer.model, window=window, constraints=constraints
        )
    except Exception as error:  # noqa: BLE001 — reviewer failure must stay observable, not fatal
        print(f"review failed: {error}", file=sys.stderr)
        # Failure evidence belongs in the journal too — "no verdict" must be
        # distinguishable from "reviewer silently died".
        with suppress(SnapshotError):
            StepJournal(journal_file).record(
                TraceEvent(
                    "reviewer_error",
                    {
                        "error": str(error)[:300],
                        "review_job_id": review_job_id,
                        "review_trigger": review_trigger,
                    },
                )
            )
        return 1
    # A reserved slot was already counted by the caller; charging again would
    # double-count it. An unrecognised token means no slot was ever taken, so
    # the review is charged normally — otherwise passing the flag would be
    # enough to review for free.
    spend = settle(session, reservation, last_usage()) or charge(session, last_usage())
    StepJournal(journal_file).record(
        TraceEvent(
            "reviewer_decision",
            {
                "decision": decision.decision,
                "failure_class": decision.failure_class,
                "reason": decision.reason,
                "hypothesis": decision.hypothesis,
                "confidence": decision.confidence,
                "model": config.reviewer.model,
                "reviewed_upto": records[-1].step,
                "review_job_id": review_job_id,
                "review_trigger": review_trigger,
                "timing": {
                    "queue_ms": started.event.payload.get("queue_ms"),
                    "inference_ms": decision.inference_ms,
                },
                "shadow": True,
                # What the reviewer could actually see when it judged: a verdict
                # made on a truncated view or with no goal must stay identifiable.
                "inputs": digest.provenance(),
                "spend": {"session_reviews": spend.session, "session_tokens": spend.tokens},
            },
        )
    )
    notes = []
    if not digest.goal_present:
        notes.append("no goal recorded")
    if digest.truncated:
        notes.append(f"truncated to {digest.steps_shown} steps")
    if digest.injection_suspected:
        notes.append("possible injection in trajectory text")
    notes.append(f"review {spend.session} this session, {spend.tokens} tokens")
    suffix = f"  [{'; '.join(notes)}]" if notes else ""
    print(
        f"[shadow] {decision.decision} ({decision.failure_class}, "
        f"conf={decision.confidence:.2f}): {decision.reason}{suffix}"
    )
    return 0


def _delete_journal(journal: Path, cutoff: float) -> bool:
    """Remove a journal and its state while no writer or reviewer is active.

    Staleness is re-checked *after* the lock is acquired. Holding the lock
    proves no one is writing right now; it says nothing about whether the
    journal was still stale by the time the wait ended. A writer that appended
    while this call was blocked has made the file current, and deleting it
    then would destroy a live session's record (PR #58 review, P0).

    Returns whether the journal was actually removed, because the caller must
    exclude exactly the deleted set from reference computation — excluding a
    journal that survived would prune snapshots it still points at.

    The lock file itself is deliberately left behind. Removing it — even last —
    does not remove the writers already blocked on the old inode: one wakes on
    the unlinked inode while the next writer creates a fresh file at the same
    path, and two processes holding locks on different inodes are serialised
    by nothing (PR #58 review, P0). An empty lock file is a few bytes; a
    corrupted journal is the evidence base for every published rate.
    """
    review_lock_path = journal.with_suffix(".review.lock")
    try:
        review_handle = review_lock_path.open("a")
    except OSError:
        return False
    try:
        flock(review_handle, LOCK_EX | LOCK_NB)
    except OSError:
        review_handle.close()
        return False

    lock_path = journal.with_suffix(journal.suffix + ".lock")
    try:
        handle = lock_path.open("a")
        try:
            flock(handle, LOCK_EX)
            if not journal.exists() or journal.stat().st_mtime >= cutoff:
                return False  # became current while we waited
            journal.unlink(missing_ok=True)
            journal.with_suffix(journal.suffix + ".state").unlink(missing_ok=True)
            return True
        finally:
            flock(handle, LOCK_UN)
            handle.close()
    except OSError:
        return False
    finally:
        flock(review_handle, LOCK_UN)
        review_handle.close()


def _remove_worktree(worktree: Path) -> None:
    """Remove a fork worktree through git so its administrative entry goes too.

    A plain rmtree leaves the parent repository listing a worktree that no
    longer exists, which then blocks reuse of that path.
    """
    result = subprocess.run(
        ["git", "-C", str(worktree), "worktree", "remove", "--force", str(worktree)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Deleting the directory anyway would leave the parent repository
        # registering a worktree that no longer exists — precisely the defect
        # this command exists to fix (PR #58 review, P1).
        print(
            f"  could not remove {worktree.name}: {result.stderr.strip()[:160]}",
            file=sys.stderr,
        )


def _doctor_main(config_path: Path | None) -> int:
    """Report whether supervision works, and exit non-zero when it does not.

    A tool whose normal output is silence needs one command that speaks.
    """
    checks = run_doctor(config_path)
    marks = {OK: "  ok  ", INFO: " info ", WARN: " warn ", FAIL: " FAIL "}
    for check in checks:
        print(f"[{marks[check.status]}] {check.name}: {check.detail}")
    verdict = worst(checks)
    if verdict == FAIL:
        print("\nsupervision is NOT working", file=sys.stderr)
        return 2
    if verdict == WARN:
        print("\nsupervision works, with warnings above", file=sys.stderr)
        return 1
    print("\nsupervision is working")
    return 0


def _daemon_main(action: str, manager: ServiceManager | None = None) -> int:
    if action == "reload":
        try:
            result = asyncio.run(DaemonClient().reload_config())
        except DaemonError as error:
            print(f"spotterd: config reload unavailable ({error})", file=sys.stderr)
            return 1
        details = [f"active={result.active_generation}"]
        if result.candidate_generation is not None:
            details.append(f"candidate={result.candidate_generation}")
        if result.changes:
            details.append(f"changes={','.join(change.path for change in result.changes)}")
        if result.required_boundaries:
            details.append(
                "required=" + ",".join(boundary.value for boundary in result.required_boundaries)
            )
        if result.error:
            details.append(result.error)
        print(f"spotterd: config {result.disposition.value.lower()} ({', '.join(details)})")
        return (
            0
            if result.disposition in {ReloadDisposition.APPLIED, ReloadDisposition.STAGED_NEXT_TURN}
            else 1
        )

    if manager is not None:
        service = manager
    else:
        try:
            manifest = IntegrationManifest.load(RuntimeLayout.discover().integration_manifest)
        except IntegrationError as error:
            print(f"spotterd: unavailable ({error})", file=sys.stderr)
            return 1
        service = (
            ManagedServiceManager()
            if manifest is not None and manifest.runtime_mode == "managed"
            else ManualServiceManager()
        )

    async def run() -> DaemonStatus:
        operations = {
            "start": service.start,
            "stop": service.stop,
            "restart": service.restart,
            "status": service.status,
        }
        return await operations[action]()

    status = asyncio.run(run())
    details = []
    if status.pid is not None:
        details.append(f"pid={status.pid}")
    if status.protocol is not None:
        details.append(f"protocol={status.protocol}")
    if status.version is not None:
        details.append(f"version={status.version}")
    if status.build_id is not None:
        details.append(f"build={status.build_id}")
    if status.config_generation is not None:
        details.append(f"config={status.config_generation}")
    if status.pending_config_generation is not None:
        details.append(f"pending-config={status.pending_config_generation}")
    if status.config_reload_error is not None:
        details.append(f"config-reload-error={status.config_reload_error}")
    details.append(f"compatibility={status.compatibility.value}")
    if status.runtime_generation is not None:
        details.append(f"runtime={status.runtime_generation}")
    if status.construction_fingerprint is not None:
        details.append(f"construction={status.construction_fingerprint}")
    if status.app_server_state is not None:
        app_server = status.app_server_state
        if status.app_server_connection_epoch is not None:
            app_server += f"@{status.app_server_connection_epoch}"
        if status.app_server_version is not None:
            app_server += f"/codex-{status.app_server_version}"
        details.append(f"app-server={app_server}")
    if status.app_server_capabilities is not None:
        details.append(
            "app-capabilities="
            + ",".join(f"{name}:{value}" for name, value in status.app_server_capabilities)
        )
    if status.app_server_server_changed:
        details.append("app-server-changed=true")
    if status.app_server_capabilities_changed:
        details.append("app-capabilities-changed=true")
    if status.started_at is not None:
        details.append(f"started-at={status.started_at:.3f}")
    if status.detail:
        details.append(status.detail)
    suffix = f" ({', '.join(details)})" if details else ""
    print(f"spotterd: {status.health.value}{suffix}")
    if action == "stop":
        return 0 if status.health == RuntimeHealth.UNAVAILABLE else 1
    return 0 if status.health == RuntimeHealth.HEALTHY else 1


def _codex_main(args: Sequence[str]) -> int:
    """Launch Codex through Spotter's verified external App Server."""
    try:
        manifest = IntegrationManifest.load(RuntimeLayout.discover().integration_manifest)
    except IntegrationError as error:
        print(f"Codex launch unavailable: {error}", file=sys.stderr)
        return 1
    if manifest is None or manifest.state != "ready":
        print("Codex launch unavailable: run `spotter setup codex` first", file=sys.stderr)
        return 1
    if manifest.app_server_endpoint is None:
        print(
            "Codex launch unavailable: rerun setup with --endpoint ws://127.0.0.1:4500",
            file=sys.stderr,
        )
        return 1
    if "--remote" in args:
        print("Codex launch unavailable: Spotter supplies --remote", file=sys.stderr)
        return 1
    if _daemon_main("start") != 0:
        return 1
    observation = next(
        (check for check in check_runtime(deep=True) if check.name == "observation"), None
    )
    if observation is None or observation.status != OK:
        detail = observation.detail if observation is not None else "probe produced no result"
        print(f"Codex launch unavailable: {detail}", file=sys.stderr)
        return 1
    command = [manifest.agent_path, "--remote", manifest.app_server_endpoint, *args]
    try:
        os.execv(manifest.agent_path, command)
    except OSError as error:
        print(f"Codex launch failed: {error}", file=sys.stderr)
        return 1


def _update_main(provenance: InstallationProvenance | None = None) -> int:
    """Report the package-owner command; never replace installed files directly."""

    identity = current_build_identity()
    detected = provenance or installation_provenance()
    python = shlex.quote(sys.executable)
    commands = {
        InstallationMethod.HOMEBREW: (
            "brew info spotter-agent/spotter/spotter",
            "brew upgrade spotter-agent/spotter/spotter",
        ),
        InstallationMethod.PIPX: (
            "pipx list",
            "pipx upgrade spotter-agent",
        ),
        InstallationMethod.UV_TOOL: (
            "uv tool list",
            "uv tool upgrade spotter-agent",
        ),
        InstallationMethod.PIP: (
            f"{python} -m pip index versions spotter-agent",
            f"{python} -m pip install --upgrade spotter-agent",
        ),
    }

    print(f"spotter {identity.version} (build {identity.build_id})")
    print(f"installation: {detected.method.value} ({detected.evidence})")
    command = commands.get(detected.method)
    if command is None:
        print("self-update: unsupported for source/editable installations")
        print("update the source checkout or development environment with its owning workflow")
        return 0
    check, upgrade = command
    print(f"check available version: {check}")
    print(f"upgrade with package owner: {upgrade}")
    print("then reconcile runtime: spotter setup codex && spotter doctor")
    return 0


def _integration_main(
    action: str,
    *,
    config_path: Path | None,
    portable: bool,
    dry_run: bool,
    app_server_endpoint: str | None = None,
    manager: IntegrationManager | None = None,
) -> int:
    """Install or remove the owned Codex integration transactionally."""
    try:
        integration = manager or IntegrationManager(
            portable=portable,
            config_path=config_path,
            app_server_endpoint=app_server_endpoint,
        )
        if action == "setup":
            plan = integration.plan()
            for line in plan.lines():
                print(line)
            if dry_run:
                print("dry-run: no changes made")
                return 0
            manifest = integration.setup()
            print(f"Codex integration: {manifest.state} ({integration.manifest_path})")
            if manifest.app_server_endpoint is None:
                print(
                    "App Server endpoint: pending; start a shared server and rerun setup with "
                    "--endpoint ws://127.0.0.1:4500"
                )
            else:
                endpoint = display_app_server_endpoint(manifest.app_server_endpoint)
                print(f"App Server endpoint: verified ({endpoint})")
                print("Start the shared Codex TUI: spotter codex")
            return 0
        removed = integration.teardown()
        print("Codex integration removed" if removed else "Codex integration not configured")
        return 0
    except IntegrationError as error:
        print(f"Codex integration failed: {error}", file=sys.stderr)
        return 1


def _status_main() -> int:
    """What Spotter is storing, and whether it is actually observing.

    Silence is Spotter's designed normal state, which is why silence cannot
    also be its failure state (issue #41).
    """
    home = spotter_home()
    if not home.exists():
        print(f"no spotter home at {home} — nothing has ever been recorded", file=sys.stderr)
        return 1
    sessions_dir = home / "sessions"
    journals = sorted(sessions_dir.glob("*.jsonl")) if sessions_dir.exists() else []
    total_bytes = sum(p.stat().st_size for p in home.rglob("*") if p.is_file())
    newest = max((p.stat().st_mtime for p in journals), default=None)

    # status is where a wrong posture gets fixed, not merely reported: a
    # journal of command history should never have been group-readable.
    before = home.stat().st_mode & 0o777
    secure_dir(home)
    after = home.stat().st_mode & 0o777
    note = f" (tightened from {oct(before)})" if before != after else ""
    print(f"home: {home}  ({total_bytes / 1e6:.1f} MB, mode {oct(after)}{note})")
    runtime_checks = check_runtime()
    marks = {OK: "ok", INFO: "info", WARN: "WARN", FAIL: "FAIL"}
    print("runtime:")
    for check in runtime_checks:
        print(f"  [{marks[check.status]}] {check.name}: {check.detail}")
    print(f"sessions: {len(journals)}")
    warned = False
    if newest is None:
        print("  last observation: never")
    else:
        age_hours = (time.time() - newest) / 3600
        print(f"  last observation: {age_hours:.1f}h ago")
        if age_hours > 24:
            warned = True
            print("  WARNING: nothing observed in over a day — is the hook still registered?")
    forks = list_forks()
    if forks:
        print(f"fork worktrees: {len(forks)} (remove with: spotter prune --forks --apply)")
    unreadable = 0
    reviewer_errors = 0
    exposed = 0
    for journal in journals:
        try:
            records = StepJournal.load(journal)
        except SnapshotError:
            print(f"  UNREADABLE: {journal.name}")
            unreadable += 1
            continue
        reviewer_errors += sum(1 for r in records if r.event.kind == "reviewer_error")
        exposed += sum(
            1 for line in journal.read_text(errors="replace").splitlines() if scan_text(line)
        )
    if reviewer_errors:
        warned = True
        print(f"reviewer errors recorded: {reviewer_errors} (see {home / 'logs'})")
    ledger_broken = False
    try:
        totals = spend_totals()
    except LedgerCorrupt as error:
        ledger_broken = True
        # The diagnostic command must survive the corruption it is diagnosing.
        print(f"WARNING: spend ledger unreadable ({error}); ceilings are refusing to spend")
    else:
        if totals is not None:
            print(f"reviews today: {totals['day']}  |  tokens recorded: {totals['tokens']}")
    if exposed:
        warned = True
        print(
            f"WARNING: {exposed} pre-redaction lines match credential patterns; "
            "these journals predate redaction"
        )
    verdict = worst(runtime_checks)
    if verdict == FAIL or unreadable or ledger_broken:
        return 2
    if verdict == WARN or warned:
        return 1
    return 0


def _label_main(
    session: str,
    step: int | None,
    verdict: str,
    note: str,
    rater: str | None,
    signal_type: str | None,
) -> int:
    """Record a human verdict. Labels live outside the journal so the reviewer
    never reads its own report card."""
    try:
        records = StepJournal.load(journal_path({"session_id": session}))
    except (OSError, SnapshotError) as error:
        print(f"label failed: {error}", file=sys.stderr)
        return 1
    try:
        label = add_label(
            session,
            step,
            verdict,
            note,
            records,
            rater=rater,
            signal_type=signal_type,
        )
    except (LabelError, SignalSampleError) as error:
        print(f"label failed: {error}", file=sys.stderr)
        return 1
    target = "session" if step is None else f"step {step}"
    scope = f" [{label.scope}]" if label.scope else ""
    print(f"labeled {session} {target}{scope}: {label.verdict} by {label.rater}")
    return 0


def _label_opportunity_main(
    session: str,
    opportunity_id: str,
    semantic_earliest: int,
    semantic_latest: int,
    observable_earliest: int,
    observable_latest: int,
    required_evidence: tuple[int, ...],
    note: str,
    rater: str | None,
) -> int:
    try:
        records = StepJournal.load(journal_path({"session_id": session}))
        window = add_opportunity(
            session,
            opportunity_id,
            records,
            semantic_earliest=semantic_earliest,
            semantic_latest=semantic_latest,
            observable_earliest=observable_earliest,
            observable_latest=observable_latest,
            required_evidence=required_evidence,
            note=note,
            rater=rater,
        )
    except (OSError, SnapshotError, OpportunityError) as error:
        print(f"opportunity label failed: {error}", file=sys.stderr)
        return 1
    print(
        f"labeled {session} opportunity {window.opportunity_id} by {window.rater}: "
        f"semantic={window.semantic_earliest.step}..{window.semantic_latest.step}, "
        f"observable={window.observable_earliest.step}..{window.observable_latest.step}"
    )
    return 0


def _sample_signals_main(
    session: str,
    signal_type: str,
    event_kinds: tuple[str, ...],
    sample_rate: float,
) -> int:
    try:
        records = StepJournal.load(journal_path({"session_id": session}))
        batch = sample_signal_silence(
            session,
            records,
            signal_type,
            event_kinds,
            sample_rate,
        )
    except (OSError, SnapshotError, SignalSampleError) as error:
        print(f"signal sampling failed: {error}", file=sys.stderr)
        return 1
    print(
        f"sampled {batch.selected}/{batch.eligible} eligible {batch.signal_type} sources "
        f"at p={batch.inclusion_probability:g}; "
        f"excluded emitted={batch.excluded_emitted}, suppressed={batch.excluded_suppressed}, "
        f"unobservable={batch.excluded_unobservable}"
    )
    _, samples = load_signal_sampling(session)
    for sample in samples:
        if sample.batch_id == batch.batch_id:
            print(f"  step {sample.step}: {sample.event_kind} {sample.event_id}")
    return 0


def _metrics_main(session: str | None) -> int:
    """Report each labeled measurement together with its coverage."""
    sessions_dir = journal_path({"session_id": "probe"}).parent
    journals = sorted(sessions_dir.glob("*.jsonl"))
    if session:
        journals = [j for j in journals if j == journal_path({"session_id": session})]
    if not journals:
        print("no journals found", file=sys.stderr)
        return 1
    gates: dict[str, Tally] = {}
    runtime_journals: list[tuple[list[StepRecord], int]] = []
    blind_spots: dict[str, int] = {}
    reviewer = ceiling = Tally()
    gate_misses = Tally()
    reviewer_misses = Tally()
    reviewer_by_trigger: dict[str, Tally] = {}
    reviewer_misses_by_trigger: dict[str, Tally] = {}
    signal_misses: dict[str, Tally] = {}
    signal_sampling_batches: list[SignalSamplingBatch] = []
    uncorrelatable_proposals = 0
    signals: dict[str, Tally] = {}
    unattributed_signals = 0
    agreement = AgreementTally()
    opportunity_reports: list[OpportunityTimingReport] = []
    for journal in journals:
        try:
            records = StepJournal.load(journal)
        except SnapshotError as error:
            print(f"{journal.stem}: unreadable journal ({error})", file=sys.stderr)
            continue
        runtime_journals.append((records, journal.stat().st_size))
        try:
            session_gates, session_reviewer, session_ceiling = tally_session(journal.stem, records)
            session_misses, session_uncorrelatable = tally_unflagged_proposals(
                journal.stem, records
            )
            session_signals, session_unattributed_signals = tally_signal_candidates(
                journal.stem, records
            )
            session_reviewer_misses = tally_reviewer_continues(journal.stem, records)
            session_reviewer_by_trigger, session_reviewer_misses_by_trigger = (
                tally_reviewer_triggers(journal.stem, records)
            )
            session_signal_misses, session_sampling_batches = tally_signal_silence(
                journal.stem, records
            )
            session_agreement = agreement_session(journal.stem, records)
            opportunity_reports.append(measure_opportunity_timing(journal.stem, records))
        except (LabelError, OpportunityError, SignalSampleError) as error:
            print(f"metrics aborted: {error}", file=sys.stderr)
            return 1
        for rule, tally in session_gates.items():
            gates[rule] = merge(gates.get(rule, Tally()), tally)
        for record in records:
            if record.event.kind == "gate_fail_open":
                rule = str(record.event.payload.get("rule") or "unknown")
                blind_spots[rule] = blind_spots.get(rule, 0) + 1
        reviewer = merge(reviewer, session_reviewer)
        ceiling = merge(ceiling, session_ceiling)
        gate_misses = merge(gate_misses, session_misses)
        reviewer_misses = merge(reviewer_misses, session_reviewer_misses)
        for trigger, tally in session_reviewer_by_trigger.items():
            reviewer_by_trigger[trigger] = merge(reviewer_by_trigger.get(trigger, Tally()), tally)
        for trigger, tally in session_reviewer_misses_by_trigger.items():
            reviewer_misses_by_trigger[trigger] = merge(
                reviewer_misses_by_trigger.get(trigger, Tally()), tally
            )
        for stratum, tally in session_signal_misses.items():
            signal_misses[stratum] = merge(signal_misses.get(stratum, Tally()), tally)
        signal_sampling_batches.extend(session_sampling_batches)
        uncorrelatable_proposals += session_uncorrelatable
        for signal_type, tally in session_signals.items():
            signals[signal_type] = merge(signals.get(signal_type, Tally()), tally)
        unattributed_signals += session_unattributed_signals
        agreement = merge_agreement(agreement, session_agreement)

    objective_report = None
    if session is None:
        experiment_dir = spotter_home() / "experiments"
        try:
            objective_report = measure_objective_outcomes(
                sorted(experiment_dir.rglob("*.jsonl")) if experiment_dir.exists() else ()
            )
        except ObjectiveOutcomeError as error:
            print(f"metrics aborted: {error}", file=sys.stderr)
            return 1

    print("P3 gate flag precision (label each flag tp|fp):")
    if not gates:
        print("  no gate flags recorded")
    for rule, tally in sorted(gates.items()):
        print("  " + tally.rate_line(rule, "false-discovery (1 - precision)", count_negative=True))
    print("  true FPR unavailable: no per-rule true-negative sampling frame")
    if blind_spots:
        rules = ", ".join(f"{rule}={count}" for rule, count in sorted(blind_spots.items()))
        print(
            f"  blind spots (not part of the rate): {sum(blind_spots.values())} fail-open ({rules})"
        )
    print("P3 gate misses (label unflagged tool proposals miss|tn):")
    print("  " + gate_misses.rate_line("unflagged proposals", "miss-rate"))
    if uncorrelatable_proposals:
        print(
            "  blind spots (not part of the rate): "
            f"{uncorrelatable_proposals} proposals lack a correlation id"
        )
    print("Signal candidate precision (label each active candidate tp|fp):")
    if not signals:
        print("  no active signal candidates recorded")
    for signal_type, tally in sorted(signals.items()):
        print("  " + tally.rate_line(signal_type, "correct"))
    if unattributed_signals:
        print(
            "  blind spots (not part of the rate): "
            f"{unattributed_signals} active candidates lack a stable type or identity"
        )
    print("Signal candidate misses (label sampled non-emitted sources miss|tn):")
    if not signal_misses:
        print("  no persisted signal-silence samples")
    for stratum, tally in sorted(signal_misses.items()):
        print("  " + tally.rate_line(stratum, "miss-rate"))
    for batch in signal_sampling_batches:
        kinds = ",".join(batch.event_kinds)
        frame_total = (
            batch.eligible
            + batch.excluded_emitted
            + batch.excluded_suppressed
            + batch.excluded_unobservable
        )
        blind_spot = batch.excluded_unobservable / frame_total if frame_total else 0
        print(
            f"  frame {batch.signal_type}[{kinds}]: p={batch.inclusion_probability:g}, "
            f"selected={batch.selected}/{batch.eligible}, "
            f"excluded emitted={batch.excluded_emitted}, "
            f"suppressed={batch.excluded_suppressed}, "
            f"unobservable={batch.excluded_unobservable}/{frame_total} ({blind_spot:.0%})"
        )
    if signal_sampling_batches:
        print("  bias: rates represent only the declared event-kind strata, not all trajectories")
    print("P4 reviewer precision (label each verify/nudge tp|fp):")
    print("  " + reviewer.rate_line("interventions", "correct"))
    for trigger, tally in sorted(reviewer_by_trigger.items()):
        print("    " + tally.rate_line(trigger, "correct"))
    print("Reviewer negative decisions (label each CONTINUE miss|tn):")
    print("  " + reviewer_misses.rate_line("continues", "miss-rate"))
    for trigger, tally in sorted(reviewer_misses_by_trigger.items()):
        print("    " + tally.rate_line(trigger, "miss-rate"))
    print("  sampling boundary: trajectories without a reviewer decision are outside this rate")
    print("P1 observability ceiling (label failed sessions visible|invisible):")
    print("  " + ceiling.rate_line("sessions", "visible"))
    print("Rater agreement (double-label subset):")
    print("  " + agreement.rate_line())
    print(render_opportunity_timing(merge_opportunity_timing(opportunity_reports)))
    print(
        render_effect_coverage(
            measure_effect_coverage(record for rows, _ in runtime_journals for record in rows)
        )
    )
    print(render_runtime_costs(measure_runtime_costs(runtime_journals)))
    if objective_report is not None:
        print(render_objective_outcomes(objective_report))
    return 0


def _observability_main(session: str | None) -> int:
    """Report evidence coverage without claiming an unlabeled failure ceiling."""

    sessions_dir = journal_path({"session_id": "probe"}).parent
    journals = sorted(sessions_dir.glob("*.jsonl"))
    if session:
        journals = [journal for journal in journals if journal.stem == session]
    if not journals:
        print("no journals found", file=sys.stderr)
        return 1
    histories: list[list[StepRecord]] = []
    for journal in journals:
        try:
            histories.append(StepJournal.load(journal))
        except (OSError, SnapshotError) as error:
            print(f"{journal.stem}: unreadable journal ({error})", file=sys.stderr)
    if not histories:
        return 1
    try:
        source_samples = SourceAuditStore(sessions_dir / SOURCE_AUDIT_RELATIVE_PATH).load()
    except (OSError, UnicodeError, ObservabilityError) as error:
        print(f"source audit unreadable: {error}", file=sys.stderr)
        return 1
    if session:
        thread_id = (
            session.removeprefix("app-server-") if session.startswith("app-server-") else None
        )
        source_samples = tuple(sample for sample in source_samples if sample.thread_id == thread_id)
    print(render_observability(measure_observability(histories, source_samples)))
    return 0


def _prune_main(
    repo: Path,
    *,
    apply: bool,
    max_age_days: int | None = None,
    forks: bool = False,
    journals: bool = False,
) -> int:
    """Drop refs/spotter/steps/* that no journal references (issue #7).

    Dry-run by default: deleting a snapshot is the one spotter operation that
    can destroy fork-ability, so the human confirms with --apply. The global
    lock serializes against a hook that has pinned a ref but not yet
    journaled it.
    """
    sessions_dir = journal_path({"session_id": "probe"}).parent
    verb = "deleted" if apply else "would delete (pass --apply)"
    if journals and max_age_days is None:
        print("--journals requires --max-age-days", file=sys.stderr)
        return 1
    try:
        # Everything that reads or removes journals and refs happens under one
        # lock: selecting stale journals, deleting them, recomputing what is
        # still referenced, and pruning. Releasing between those steps lets a
        # hook append to a journal being deleted, or pin a ref this pass has
        # already decided is unreferenced (PR #58 review, P0).
        with global_lock():
            pins = SnapshotPinStore().load()
            registry_entry = RepositoryRegistry().find_repository(repo) if pins else None
            manual_roots = {
                pin.snapshot_sha
                for pin in pins
                if registry_entry is not None
                and pin.registry_entry_id == registry_entry.registry_entry_id
            }
            cutoff = time.time() - (max_age_days or 0) * 86400
            doomed = (
                stale_journals(sessions_dir, max_age_days)
                if journals and max_age_days is not None
                else []
            )
            removed: list[Path] = []
            for journal in doomed:
                if apply:
                    if _delete_journal(journal, cutoff):
                        removed.append(journal)
                        print(f"journal {verb}: {journal.stem}")
                    else:
                        print(f"journal kept (written while pruning): {journal.stem}")
                else:
                    print(f"journal {verb}: {journal.stem}")
            # Reference computation happens after deletion, so a journal that
            # is going away cannot keep its snapshots alive — and, crucially,
            # snapshots are pruned exactly once, after that. The previous
            # version pruned before deleting and then again after, which is
            # the opposite of what its own comment claimed.
            #
            # In dry-run the journals are still on disk, so they are excluded
            # logically instead: a preview that omits the snapshots an apply
            # would orphan is a preview of a different operation. Under --apply
            # the exclusion is the set actually removed, so a journal that
            # survived the staleness re-check keeps its snapshots.
            references = snapshot_references(
                sessions_dir, repo, exclude=removed if apply else doomed
            )
            for snapshot in manual_roots:
                references.setdefault(snapshot, [])
            pruned = prune_snapshots(
                repo,
                set(references),
                protected=manual_roots,
                apply=apply,
                max_age_days=max_age_days,
            )
    except (RepositoryRegistryError, SnapshotError, SnapshotPinError) as error:
        print(f"prune aborted: {error}", file=sys.stderr)
        return 1
    if forks:
        for worktree in list_forks():
            print(f"fork worktree {verb}: {worktree.name}")
            if apply:
                _remove_worktree(worktree)
    print(f"{len(references)} retained snapshots; {verb} {len(pruned)} refs")
    for pruned_ref in pruned:
        print(f"  {pruned_ref.sha} ({pruned_ref.reason})")
        # An expired snapshot is still referenced: name the steps that lose
        # fork-ability instead of reporting a bare count (PR #19 review, P1).
        for session, step in references.get(pruned_ref.sha, []):
            print(f"    fork lost: session {session} step {step}")
    return 0


def _pins_main(
    action: str,
    *,
    repo: Path,
    snapshot: str | None,
    pin_id: str | None,
) -> int:
    store = SnapshotPinStore()
    try:
        if action == "list":
            entries = {entry.registry_entry_id: entry for entry in RepositoryRegistry().load()}
            for pin in store.load():
                entry = entries.get(pin.registry_entry_id)
                repository = entry.last_known_path if entry is not None else "<unknown repository>"
                print(f"{pin.pin_id} {pin.snapshot_sha} {repository}")
            return 0
        if action == "remove":
            if pin_id is None:
                raise SnapshotPinError("remove requires a pin id")
            with global_lock():
                removed = store.remove(pin_id)
            print(f"removed pin {removed.pin_id} for {removed.snapshot_sha}")
            return 0

        if snapshot is None:
            raise SnapshotPinError("add requires a snapshot SHA")
        registry = RepositoryRegistry()
        entry = registry.find_repository(repo)
        if entry is None:
            raise SnapshotPinError(f"{repo} has no Spotter repository ownership record")
        resource = next(
            (
                item
                for item in entry.resources
                if item.resource_type == "git_ref" and item.expected_target == snapshot
            ),
            None,
        )
        if resource is None:
            raise SnapshotPinError(f"{snapshot} is not an exact Spotter-owned snapshot in {repo}")
        with global_lock():
            entry = registry.record_ref(repo, resource.resource_id, snapshot)
            pin = store.add(entry.registry_entry_id, snapshot)
        print(f"pinned {snapshot} as {pin.pin_id}")
        return 0
    except (OSError, RepositoryRegistryError, SnapshotPinError) as error:
        print(f"pins {action} failed: {error}", file=sys.stderr)
        return 1


def _data_purge_outcome(item: DataResourceInspection) -> str:
    if item.confidence != OwnershipConfidence.SAFE_OWNED:
        return "skipped_ambiguous"
    if item.relative_path.endswith(".lock"):
        return "preserved_synchronization"
    return "planned"


def _purge_integration_main(*, json_output: bool, apply: bool) -> int:
    """Preview or remove exact integration-owned resources under the lifecycle lock."""
    try:
        if apply:
            result = IntegrationPurger().purge()
            inspections = result.resources
            outcomes = result.outcomes
            failures = result.failures
        else:
            inspections = IntegrationInventory().inspect()
            outcomes = {}
            failures = {}
    except (OSError, IntegrationInventoryError) as error:
        print(f"purge failed: {error}", file=sys.stderr)
        return 1

    def outcome(item: IntegrationResourceInspection) -> str:
        if item.confidence != OwnershipConfidence.SAFE_OWNED:
            return "skipped_ambiguous"
        if item.presence == ResourcePresence.ABSENT:
            return "already_absent"
        if item.resource_type == "lock":
            return "preserved_synchronization"
        return "planned"

    groups = ("SAFE_OWNED", "INACCESSIBLE", "AMBIGUOUS")
    summary = {name: sum(item.confidence.value == name for item in inspections) for name in groups}

    def resource_key(item: IntegrationResourceInspection) -> tuple[str, str]:
        return item.resource_type, item.resource_id

    resources = [
        {
            "resource_type": item.resource_type,
            "resource_id": item.resource_id,
            "group": item.confidence.value,
            "confidence": item.confidence.value,
            "presence": item.presence.value,
            "reason": item.reason,
            "outcome": outcomes.get(resource_key(item), outcome(item)),
            "failure": failures.get(resource_key(item)),
        }
        for item in inspections
    ]
    if json_output:
        print(
            json.dumps(
                {
                    "scope": "integration",
                    "dry_run": not apply,
                    "deletion_supported": True,
                    "summary": summary,
                    "resources": resources,
                },
                sort_keys=True,
            )
        )
    else:
        action = "purge results" if apply else "purge preview (dry-run)"
        print(f"{action}; scope=integration")
        for name in groups:
            matching = [item for item in inspections if item.confidence.value == name]
            print(f"{name} ({len(matching)})")
            for item in matching:
                print(
                    f"  {item.resource_type} {item.resource_id} [{item.presence.value.lower()}] "
                    f"- {outcomes.get(resource_key(item), outcome(item))} - {item.reason}"
                )
    return int(
        bool(failures)
        or any(
            item.confidence in {OwnershipConfidence.INACCESSIBLE, OwnershipConfidence.AMBIGUOUS}
            for item in inspections
        )
    )


def _purge_data_main(*, json_output: bool, apply: bool) -> int:
    """Plan or remove schema-proven user data under each store's retained lock."""
    inventory = DataInventory()
    outcomes: dict[str, str] = {}
    failures: dict[str, str] = {}
    try:
        inspections = inventory.inspect()
        reported = {item.relative_path: item for item in inspections}
        for item in inspections:
            outcomes[item.relative_path] = _data_purge_outcome(item)
        if apply:
            for item in inspections:
                if outcomes[item.relative_path] != "planned":
                    continue
                result = inventory.remove(item)
                outcomes[item.relative_path] = result.outcome
                if result.inspection is not None:
                    reported[item.relative_path] = result.inspection
                if result.failure is not None:
                    failures[item.relative_path] = result.failure
            for item in inventory.inspect():
                reported.setdefault(item.relative_path, item)
                outcomes.setdefault(item.relative_path, _data_purge_outcome(item))
        inspections = tuple(sorted(reported.values(), key=lambda item: item.relative_path))
    except (OSError, DataInventoryError) as error:
        print(f"purge failed: {error}", file=sys.stderr)
        return 1
    groups = ("SAFE_OWNED", "INACCESSIBLE", "AMBIGUOUS")
    summary = {name: sum(item.confidence.value == name for item in inspections) for name in groups}
    resources = [
        {
            "resource_type": "data",
            "resource_id": item.relative_path,
            "expected_schema": item.expected_schema,
            "schema_version": item.schema_version,
            "group": item.confidence.value,
            "confidence": item.confidence.value,
            "presence": item.presence.value,
            "size_bytes": item.size_bytes,
            "reason": item.reason,
            "outcome": outcomes[item.relative_path],
            "failure": failures.get(item.relative_path),
        }
        for item in inspections
    ]
    if json_output:
        print(
            json.dumps(
                {
                    "scope": "data",
                    "dry_run": not apply,
                    "deletion_supported": True,
                    "summary": summary,
                    "resources": resources,
                },
                sort_keys=True,
            )
        )
    else:
        action = "purge results" if apply else "purge preview (dry-run)"
        print(f"{action}; scope=data")
        for name in groups:
            matching = [item for item in inspections if item.confidence.value == name]
            print(f"{name} ({len(matching)})")
            for item in matching:
                size = "unknown" if item.size_bytes is None else str(item.size_bytes)
                print(
                    f"  data {item.relative_path} [{item.presence.value.lower()}, {size} bytes] "
                    f"- {outcomes[item.relative_path]} - {item.reason}"
                )
                if item.relative_path in failures:
                    print(f"    failure: {failures[item.relative_path]}")
    return int(
        bool(failures)
        or any(
            item.confidence in {OwnershipConfidence.INACCESSIBLE, OwnershipConfidence.AMBIGUOUS}
            for item in inspections
        )
    )


LogPurgeKey = tuple[str, str, str | None]


def _log_purge_key(item: LogResourceInspection) -> LogPurgeKey:
    return (item.resource_id, item.expected_path, item.identity_path)


def _log_purge_group(item: LogResourceInspection) -> str:
    return item.confidence.value


def _log_purge_outcome(item: LogResourceInspection) -> str:
    if item.confidence != OwnershipConfidence.SAFE_OWNED:
        return "skipped_ambiguous"
    if item.presence == ResourcePresence.ABSENT:
        return "already_absent"
    return "planned"


def _purge_logs_main(*, json_output: bool, apply: bool) -> int:
    """Plan or clear exact owned log contents without unlinking live files."""
    registry = LogRegistry()
    outcomes: dict[LogPurgeKey, str] = {}
    failures: dict[LogPurgeKey, str] = {}
    removed_bytes: dict[LogPurgeKey, int] = {}
    try:
        resources = registry.load()
        inspections = registry.inspect(resources)
        for item in inspections:
            outcomes[_log_purge_key(item)] = _log_purge_outcome(item)
        if apply:
            for item in inspections:
                key = _log_purge_key(item)
                if outcomes[key] != "planned" or item.owned is None:
                    continue
                try:
                    removed_bytes[key] = registry.clear(item.owned)
                except (OSError, LogRegistryError) as error:
                    outcomes[key] = "failed_retryable"
                    failures[key] = str(error)
                else:
                    outcomes[key] = "removed"
            inspections = registry.inspect()
    except (OSError, LogRegistryError) as error:
        print(f"purge failed: {error}", file=sys.stderr)
        return 1

    groups = ("SAFE_OWNED", "INACCESSIBLE", "AMBIGUOUS")
    summary = {name: sum(_log_purge_group(item) == name for item in inspections) for name in groups}
    resources_output = []
    for item in inspections:
        key = _log_purge_key(item)
        outcome = outcomes.get(key, _log_purge_outcome(item))
        resources_output.append(
            {
                "resource_type": "log",
                "resource_id": item.resource_id,
                "expected_path": item.expected_path,
                "identity_path": item.identity_path,
                "generation_id": item.generation_id,
                "group": _log_purge_group(item),
                "confidence": item.confidence.value,
                "presence": item.presence.value,
                "size_bytes": item.size_bytes,
                "reason": item.reason,
                "outcome": outcome,
                "removed_bytes": removed_bytes.get(key),
                "failure": failures.get(key),
            }
        )
    if json_output:
        print(
            json.dumps(
                {
                    "scope": "logs",
                    "dry_run": not apply,
                    "deletion_supported": True,
                    "summary": summary,
                    "resources": resources_output,
                },
                sort_keys=True,
            )
        )
    else:
        action = "purge results" if apply else "purge preview (dry-run)"
        print(f"{action}; scope=logs")
        for name in groups:
            matching = [item for item in inspections if _log_purge_group(item) == name]
            print(f"{name} ({len(matching)})")
            for item in matching:
                key = _log_purge_key(item)
                outcome = outcomes.get(key, _log_purge_outcome(item))
                size = "unknown" if item.size_bytes is None else str(item.size_bytes)
                print(
                    f"  log {item.expected_path} [{item.presence.value.lower()}, {size} bytes] "
                    f"- {outcome} - {item.reason}"
                )
                if key in failures:
                    print(f"    failure: {failures[key]}")
    return int(
        bool(failures)
        or any(
            item.confidence in {OwnershipConfidence.INACCESSIBLE, OwnershipConfidence.AMBIGUOUS}
            for item in inspections
        )
    )


PurgeResourceKey = tuple[str, str, str]


def _purge_key(item: RepositoryResourceInspection) -> PurgeResourceKey:
    return (item.registry_entry_id, item.resource_type, item.resource_id)


def _purge_group(
    item: RepositoryResourceInspection,
    reachability: dict[ArtifactKey, ArtifactRetention],
) -> str:
    if item.confidence != OwnershipConfidence.SAFE_OWNED:
        return item.confidence.value
    retention = retention_for(item, reachability)
    if retention.state == RetentionState.REFERENCED:
        return "REFERENCED"
    if retention.state == RetentionState.UNKNOWN:
        return "AMBIGUOUS"
    return "SAFE_OWNED"


def _purge_outcome(item: RepositoryResourceInspection, group: str) -> str:
    if group == "REFERENCED":
        return "skipped_referenced"
    if group in {"AMBIGUOUS", "INACCESSIBLE"}:
        return "skipped_ambiguous"
    if item.presence == ResourcePresence.ABSENT:
        return "already_absent"
    return "planned"


def _remove_owned_worktree(item: RepositoryResourceInspection) -> str | None:
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", item.resource_id],
            cwd=item.repository_path,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return str(error)
    return None if result.returncode == 0 else result.stderr.strip() or "git worktree remove failed"


def _remove_owned_ref(item: RepositoryResourceInspection) -> str | None:
    try:
        result = subprocess.run(
            ["git", "update-ref", "-d", item.resource_id, item.expected_target],
            cwd=item.repository_path,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return str(error)
    return None if result.returncode == 0 else result.stderr.strip() or "git update-ref failed"


def _purge_main(
    *, json_output: bool, snapshot_scope: bool, apply: bool, remove_registry: bool = False
) -> int:
    """Plan or remove exact unreferenced repository snapshot resources."""
    registry = RepositoryRegistry()
    data_dir = RuntimeLayout.discover().user_data_dir
    outcomes: dict[PurgeResourceKey, str] = {}
    failures: dict[PurgeResourceKey, str] = {}
    registry_outcome = "planned" if remove_registry else "preserved_ownership_evidence"

    try:
        lock = global_lock() if apply else nullcontext()
        with lock:
            entries = registry.load()
            inspections = registry.inspect(entries)
            selected_worktrees = frozenset(
                (item.registry_entry_id, item.resource_id)
                for item in inspections
                if snapshot_scope
                and item.resource_type == "worktree"
                and item.confidence == OwnershipConfidence.SAFE_OWNED
                and item.presence == ResourcePresence.PRESENT
            )
            reachability = inspect_snapshot_retention(
                entries,
                inspections,
                data_dir,
                exclude_worktrees=selected_worktrees,
            )
            for item in inspections:
                group = _purge_group(item, reachability)
                outcomes[_purge_key(item)] = _purge_outcome(item, group)

            if apply:
                for item in inspections:
                    key = _purge_key(item)
                    if item.resource_type != "worktree" or outcomes[key] != "planned":
                        continue
                    error = _remove_owned_worktree(item)
                    outcomes[key] = "removed" if error is None else "failed_retryable"
                    if error is not None:
                        failures[key] = error

                inspections = registry.inspect(entries)
                reachability = inspect_snapshot_retention(entries, inspections, data_dir)
                for item in inspections:
                    key = _purge_key(item)
                    if item.resource_type != "git_ref":
                        continue
                    group = _purge_group(item, reachability)
                    if group != "SAFE_OWNED":
                        outcomes[key] = _purge_outcome(item, group)
                        continue
                    if item.presence == ResourcePresence.ABSENT:
                        outcomes[key] = "already_absent"
                        continue
                    error = _remove_owned_ref(item)
                    outcomes[key] = "removed" if error is None else "failed_manual_recovery"
                    if error is not None:
                        failures[key] = error

                inspections = registry.inspect(entries)
                reachability = inspect_snapshot_retention(entries, inspections, data_dir)
                if remove_registry:
                    if not failures and all(
                        item.confidence == OwnershipConfidence.SAFE_OWNED
                        and item.presence == ResourcePresence.ABSENT
                        for item in inspections
                    ):
                        registry_outcome = _remove_repository_registry(registry)
                    else:
                        registry_outcome = "preserved_ownership_evidence"
    except (OSError, RepositoryRegistryError, SnapshotPinError) as error:
        print(f"purge failed: {error}", file=sys.stderr)
        return 1

    groups = ("SAFE_OWNED", "REFERENCED", "INACCESSIBLE", "AMBIGUOUS")
    grouped = {item: _purge_group(item, reachability) for item in inspections}
    summary = {name: sum(grouped[item] == name for item in inspections) for name in groups}
    resources = [
        {
            "registry_entry_id": item.registry_entry_id,
            "repository_path": item.repository_path,
            "resource_type": item.resource_type,
            "resource_id": item.resource_id,
            "expected_target": item.expected_target,
            "group": grouped[item],
            "confidence": item.confidence.value,
            "presence": item.presence.value,
            "reason": item.reason,
            "retention": retention_for(item, reachability).state.value,
            "references": list(retention_for(item, reachability).references),
            "retention_diagnostics": list(retention_for(item, reachability).diagnostics),
            "outcome": outcomes[_purge_key(item)],
            "failure": failures.get(_purge_key(item)),
        }
        for item in inspections
    ]
    if json_output:
        print(
            json.dumps(
                {
                    "scope": "snapshots" if snapshot_scope else "all",
                    "dry_run": not apply,
                    "deletion_supported": snapshot_scope,
                    "summary": summary,
                    "resources": resources,
                    "registry_outcome": registry_outcome,
                },
                sort_keys=True,
            )
        )
    else:
        action = "purge results" if apply else "purge preview (dry-run)"
        print(f"{action}; scope={'snapshots' if snapshot_scope else 'all'}")
        for name in groups:
            matching = [item for item in inspections if grouped[item] == name]
            print(f"{name} ({len(matching)})")
            for item in matching:
                retention = retention_for(item, reachability)
                key = _purge_key(item)
                print(
                    f"  {item.resource_type} {item.resource_id} "
                    f"[{item.presence.value.lower()}] - {outcomes[key]} - {item.reason}"
                )
                for reference in retention.references:
                    print(f"    retained by: {reference}")
                for diagnostic in retention.diagnostics:
                    print(f"    retention unknown: {diagnostic}")
                if key in failures:
                    print(f"    failure: {failures[key]}")
        if not inspections:
            print("no registered repository resources")
    return int(
        bool(failures)
        or any(
            grouped[item] in {"INACCESSIBLE", "AMBIGUOUS"}
            or (grouped[item] == "REFERENCED" and item.presence != ResourcePresence.PRESENT)
            for item in inspections
        )
    )


def _remove_repository_registry(registry: RepositoryRegistry) -> str:
    """Remove exhausted ownership evidence under its writer lock."""
    if not registry.path.exists():
        return "already_absent"
    lock_path = registry.path.with_suffix(registry.path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise RepositoryRegistryError("repository registry lock is not a regular file")
        flock(lock, LOCK_EX)
        try:
            entries = registry.load()
            inspections = registry.inspect(entries)
            if any(
                item.confidence != OwnershipConfidence.SAFE_OWNED
                or item.presence != ResourcePresence.ABSENT
                for item in inspections
            ):
                return "preserved_ownership_evidence"
            registry.path.unlink(missing_ok=True)
            directory = os.open(registry.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return "removed"
        finally:
            flock(lock, LOCK_UN)


def _capture_purge_scope(scope: str, operation: Callable[[], int]) -> tuple[int, dict[str, object]]:
    """Run one existing purge scope and capture its stable JSON contract."""
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = operation()
    try:
        payload = json.loads(stdout.getvalue())
    except (TypeError, json.JSONDecodeError):
        payload = {
            "scope": scope,
            "summary": {},
            "resources": [],
            "failure": stderr.getvalue().strip() or "scope produced no result",
        }
    if not isinstance(payload, dict):
        payload = {
            "scope": scope,
            "summary": {},
            "resources": [],
            "failure": "scope produced an invalid result",
        }
        exit_code = 1
    payload["scope"] = scope
    payload["deletion_supported"] = True
    payload["exit_code"] = exit_code
    if stderr.getvalue().strip():
        payload.setdefault("failure", stderr.getvalue().strip())
    return exit_code, payload


def _purge_all_main(*, json_output: bool, apply: bool) -> int:
    """Compose every independently safe purge scope in lifecycle order."""
    operations = (
        (
            "integration",
            lambda: _purge_integration_main(json_output=True, apply=apply),
        ),
        ("data", lambda: _purge_data_main(json_output=True, apply=apply)),
        (
            "snapshots",
            lambda: _purge_main(
                json_output=True,
                snapshot_scope=apply,
                apply=apply,
                remove_registry=apply,
            ),
        ),
        ("logs", lambda: _purge_logs_main(json_output=True, apply=apply)),
    )
    results = [_capture_purge_scope(scope, operation) for scope, operation in operations]
    exit_code = int(any(code != 0 for code, _ in results))
    scope_payloads = [payload for _, payload in results]
    snapshot_payload = next(
        payload for payload in scope_payloads if payload.get("scope") == "snapshots"
    )
    output = dict(snapshot_payload)
    output.update(
        {
            "scope": "all",
            "dry_run": not apply,
            "deletion_supported": True,
            "exit_code": exit_code,
            "scope_status": {str(payload.get("scope")): code for code, payload in results},
            "scopes": scope_payloads,
        }
    )
    if json_output:
        print(json.dumps(output, sort_keys=True))
    else:
        action = "purge results" if apply else "purge preview (dry-run)"
        print(f"{action}; scope=all")
        for code, payload in results:
            scope = str(payload.get("scope", "unknown"))
            print(f"{scope.upper()} ({'ok' if code == 0 else 'attention required'})")
            resources = payload.get("resources", [])
            if isinstance(resources, list):
                for group in ("SAFE_OWNED", "REFERENCED", "INACCESSIBLE", "AMBIGUOUS"):
                    matching = [
                        resource
                        for resource in resources
                        if isinstance(resource, dict) and resource.get("group") == group
                    ]
                    if matching or scope == "snapshots":
                        print(f"{group} ({len(matching)})")
                    for resource in matching:
                        print(
                            f"  {resource.get('resource_type', 'resource')} "
                            f"{resource.get('resource_id', '')} - "
                            f"{resource.get('outcome', 'unknown')} - "
                            f"{resource.get('reason', '')}"
                        )
                        references = resource.get("references", [])
                        if isinstance(references, list):
                            for reference in references:
                                print(f"    retained by: {reference}")
                        diagnostics = resource.get("retention_diagnostics", [])
                        if isinstance(diagnostics, list):
                            for diagnostic in diagnostics:
                                print(f"    retention unknown: {diagnostic}")
                        if resource.get("failure"):
                            print(f"    failure: {resource['failure']}")
            failure = payload.get("failure")
            if failure:
                print(f"{scope} purge failed: {failure}", file=sys.stderr)
            if not resources and not failure:
                print("  no resources")
    return exit_code


def _hook_main(
    config: SpotterConfig | None,
    config_path: Path | None = None,
    integration_generation: str | None = None,
) -> int:
    """Read one hook payload from stdin, print a decision if any.

    Always exits 0: any failure here fails open. Breaking the Codex session
    over a supervision bug would be the exact harm Spotter exists to prevent.
    """
    if integration_generation is not None:
        try:
            manifest = IntegrationManifest.load(RuntimeLayout.discover().integration_manifest)
        except Exception as error:  # noqa: BLE001 — generated Hook must always fail open
            print(
                f"spotter: integration generation unavailable ({error}); failing open",
                file=sys.stderr,
            )
            return 0
        if manifest is None or manifest.integration_generation != integration_generation:
            print(
                "spotter: stale integration generation; failing open; "
                "run `spotter setup codex` to reconcile",
                file=sys.stderr,
            )
            return 0
        installed_build = current_build_identity().build_id
        if manifest.setup_build_id != installed_build:
            print(
                "spotter: integration package build is stale; failing open; "
                "run `spotter setup codex` to reconcile",
                file=sys.stderr,
            )
            return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be a JSON object")
        configured_cwd = payload.get("cwd")
        repository = (
            Path(configured_cwd)
            if isinstance(configured_cwd, str) and configured_cwd.strip()
            else Path.cwd()
        )
        config_generation = "unversioned"
        if config is None:
            try:
                resolved = resolve_config(repository=repository, explicit_path=config_path)
                config = resolved.config
                config_generation = resolved.resolved_config_generation
                for diagnostic in resolved.diagnostics:
                    print(f"spotter: {diagnostic}", file=sys.stderr)
            except Exception as error:  # noqa: BLE001 — config is inside fail-open boundary
                # Unsupervised beats blocked: fall back to defaults and say so.
                location = f" {config_path}" if config_path is not None else ""
                print(
                    f"spotter: unusable config{location} ({error}); using defaults",
                    file=sys.stderr,
                )
                config = SpotterConfig.from_mapping({"main_agent": {"adapter": "codex"}})
                config_generation = "fallback-unversioned"
        output = run_hook(payload, config, config_path, config_generation)
    except Exception as error:  # noqa: BLE001 — deliberate fail-open boundary
        print(f"spotter hook error (failing open): {error}", file=sys.stderr)
        return 0
    if output is not None:
        print(output)
    return 0
