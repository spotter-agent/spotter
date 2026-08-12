"""Command-line entry point: passive observation and the Codex hook bridge."""

import argparse
import json
import subprocess
import sys
import time
import tomllib
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from fcntl import LOCK_EX, LOCK_NB, LOCK_UN, flock
from pathlib import Path

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
from spotter.codex import CodexAdapter
from spotter.config import ConfigurationError, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.core import SpotterRuntime
from spotter.doctor import FAIL, OK, WARN, worst
from spotter.doctor import run as run_doctor
from spotter.experiment import list_forks, results_path, run_experiment, summarize
from spotter.gates import Gate
from spotter.hook import journal_path, run_hook
from spotter.labels import LabelError, add_label, valid_session
from spotter.metrics import Tally, merge, tally_session
from spotter.paths import secure_dir, spotter_home
from spotter.redact import scan_text
from spotter.replay import ReplayError, fork, plan_to_json
from spotter.reviewer import last_usage, review
from spotter.snapshot import (
    SnapshotError,
    StepJournal,
    StepRecord,
    global_lock,
    prune_snapshots,
    snapshot_references,
    stale_journals,
)
from spotter.trace import TraceEvent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Observe a coding-agent trajectory")
    parser.add_argument(
        "command",
        nargs="?",
        choices=[
            "observe",
            "hook",
            "analyze",
            "fork",
            "prune",
            "review",
            "experiment",
            "label",
            "metrics",
            "status",
            "doctor",
        ],
        default="observe",
        help=(
            "observe: validate config and start; hook: Codex hook bridge (JSON on stdin); "
            "analyze: summarize journaled sessions; fork: branch a session at a step; "
            "prune: drop unreferenced refs/spotter snapshots (dry-run without --apply); "
            "review: run the shadow reviewer on a session (records only, injects nothing); "
            "experiment: counterfactual fork pairs — nudge vs control (needs --run to execute); "
            "label: record a human verdict on a gate flag, reviewer decision, or session; "
            "metrics: gate FP rate, reviewer precision and observability ceiling from labels; "
            "status: what Spotter is storing, and whether it is actually running; "
            "doctor: verify supervision end to end (non-zero exit when broken)"
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
    parser.add_argument("--model", help="review/experiment: pin the Codex model")
    parser.add_argument(
        "--reservation",
        help="review: token for a budget slot the caller already reserved (internal)",
    )
    parser.add_argument(
        "--check", help="experiment: success command run in each fork worktree (exit 0 = pass)"
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="experiment: actually execute the 2*pairs agent runs (costs real tokens)",
    )
    parser.add_argument(
        "--verdict",
        help="label: tp|fp|unclear for a step, visible|invisible|unclear for a session",
    )
    parser.add_argument("--note", default="", help="label: why (free text, stored verbatim)")
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="experiment: keep forked worktrees (rollouts are always retained)",
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
    # One boundary check for every command that names a session: sanitizing
    # instead of rejecting maps distinct ids onto one file ("a/b" -> "a_b").
    if args.session is not None and not valid_session(args.session):
        parser.error(f"--session {args.session!r} is not a valid session id")

    if args.command == "hook":
        return _hook_main(config, args.config)
    if args.command == "analyze":
        return _analyze_main(args.session)
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
        if not args.session or args.step is None or not args.guidance:
            parser.error("experiment requires --session, --step and --guidance")
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
                keep_artifacts=args.keep_artifacts,
            )
        except (ReplayError, SnapshotError, OSError, subprocess.SubprocessError) as error:
            print(f"experiment failed: {error}", file=sys.stderr)
            return 1
        print(summarize(results))
        print(f"rows appended to {results_path(args.session, args.step)}")
        return 0
    if args.command == "label":
        if not args.session or not args.verdict:
            parser.error("label requires --session and --verdict")
        return _label_main(args.session, args.step, args.verdict, args.note)
    if args.command == "metrics":
        return _metrics_main(args.session)
    if args.command == "review":
        if not args.session:
            parser.error("review requires --session")
        if config is None:
            config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig())
        if args.model:
            config = replace(config, reviewer=replace(config.reviewer, model=args.model))
        return _review_main(args.session, config, window=args.window, reservation=args.reservation)
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
    session: str, config: SpotterConfig, *, window: int, reservation: str | None = None
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
    try:
        # Check the ledger before spending, not after: the manual path calls
        # the model first, so a corrupt ledger would otherwise pay for a review
        # and then overwrite the history proving a ceiling was hit.
        read_spend(session)
    except LedgerCorrupt as error:
        print(f"review refused: {error}", file=sys.stderr)
        return 1
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
                TraceEvent("reviewer_error", {"error": str(error)[:300]})
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


def _delete_journal(journal: Path) -> None:
    """Remove a journal and its sidecars, holding its own lock first.

    The lock file itself is deliberately left behind. Removing it — even last —
    does not remove the writers already blocked on the old inode: one wakes on
    the unlinked inode while the next writer creates a fresh file at the same
    path, and two processes holding locks on different inodes are serialised
    by nothing (PR #58 review, P0). An empty lock file is a few bytes; a
    corrupted journal is the evidence base for every published rate.
    """
    lock_path = journal.with_suffix(journal.suffix + ".lock")
    handle = None
    try:
        handle = lock_path.open("a")
        flock(handle, LOCK_EX)
    except OSError:
        handle = None
    try:
        journal.unlink(missing_ok=True)
        for suffix in (".state", ".review.lock"):
            journal.with_suffix(journal.suffix + suffix).unlink(missing_ok=True)
    finally:
        if handle is not None:
            flock(handle, LOCK_UN)
            handle.close()


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
    marks = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
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
    print(f"sessions: {len(journals)}")
    if newest is None:
        print("  last observation: never")
    else:
        age_hours = (time.time() - newest) / 3600
        print(f"  last observation: {age_hours:.1f}h ago")
        if age_hours > 24:
            print("  WARNING: nothing observed in over a day — is the hook still registered?")
    forks = list_forks()
    if forks:
        print(f"fork worktrees: {len(forks)} (remove with: spotter prune --forks --apply)")
    errors = 0
    exposed = 0
    for journal in journals:
        try:
            records = StepJournal.load(journal)
        except SnapshotError:
            print(f"  UNREADABLE: {journal.name}")
            errors += 1
            continue
        errors += sum(1 for r in records if r.event.kind == "reviewer_error")
        exposed += sum(
            1 for line in journal.read_text(errors="replace").splitlines() if scan_text(line)
        )
    if errors:
        print(f"reviewer errors recorded: {errors} (see {home / 'logs'})")
    try:
        totals = spend_totals()
    except LedgerCorrupt as error:
        # The diagnostic command must survive the corruption it is diagnosing.
        print(f"WARNING: spend ledger unreadable ({error}); ceilings are refusing to spend")
    else:
        if totals is not None:
            print(f"reviews today: {totals['day']}  |  tokens recorded: {totals['tokens']}")
    if exposed:
        print(
            f"WARNING: {exposed} pre-redaction lines match credential patterns; "
            "these journals predate redaction"
        )
    return 0


def _label_main(session: str, step: int | None, verdict: str, note: str) -> int:
    """Record a human verdict. Labels live outside the journal so the reviewer
    never reads its own report card."""
    try:
        records = StepJournal.load(journal_path({"session_id": session}))
    except (OSError, SnapshotError) as error:
        print(f"label failed: {error}", file=sys.stderr)
        return 1
    try:
        label = add_label(session, step, verdict, note, records)
    except LabelError as error:
        print(f"label failed: {error}", file=sys.stderr)
        return 1
    target = "session" if step is None else f"step {step}"
    print(f"labeled {session} {target}: {label.verdict}")
    return 0


def _metrics_main(session: str | None) -> int:
    """Report the three numbers the plan gates on, each with its coverage."""
    sessions_dir = journal_path({"session_id": "probe"}).parent
    journals = sorted(sessions_dir.glob("*.jsonl"))
    if session:
        journals = [j for j in journals if j == journal_path({"session_id": session})]
    if not journals:
        print("no journals found", file=sys.stderr)
        return 1
    gates: dict[str, Tally] = {}
    blind_spots: dict[str, int] = {}
    reviewer = ceiling = Tally()
    for journal in journals:
        try:
            records = StepJournal.load(journal)
        except SnapshotError as error:
            print(f"{journal.stem}: unreadable journal ({error})", file=sys.stderr)
            continue
        try:
            session_gates, session_reviewer, session_ceiling = tally_session(journal.stem, records)
        except LabelError as error:
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

    print("P3 gate false positives (label each flag tp|fp):")
    if not gates:
        print("  no gate flags recorded")
    for rule, tally in sorted(gates.items()):
        print("  " + tally.rate_line(rule, "false-positive", count_negative=True))
    if blind_spots:
        rules = ", ".join(f"{rule}={count}" for rule, count in sorted(blind_spots.items()))
        print(
            f"  blind spots (not part of the rate): {sum(blind_spots.values())} fail-open ({rules})"
        )
    print("P4 reviewer precision (label each verify/nudge tp|fp):")
    print("  " + reviewer.rate_line("interventions", "correct"))
    print("P1 observability ceiling (label failed sessions visible|invisible):")
    print("  " + ceiling.rate_line("sessions", "visible"))
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
            doomed = (
                stale_journals(sessions_dir, max_age_days)
                if journals and max_age_days is not None
                else []
            )
            for journal in doomed:
                print(f"journal {verb}: {journal.stem}")
                if apply:
                    _delete_journal(journal)
            # Reference computation happens after deletion, so a journal that
            # is going away cannot keep its snapshots alive — and, crucially,
            # snapshots are pruned exactly once, after that. The previous
            # version pruned before deleting and then again after, which is
            # the opposite of what its own comment claimed.
            #
            # In dry-run the journals are still on disk, so they are excluded
            # logically instead: a preview that omits the snapshots an apply
            # would orphan is a preview of a different operation.
            references = snapshot_references(sessions_dir, repo, exclude=doomed)
            pruned = prune_snapshots(repo, set(references), apply=apply, max_age_days=max_age_days)
    except SnapshotError as error:
        print(f"prune aborted: {error}", file=sys.stderr)
        return 1
    if forks:
        for worktree in list_forks():
            print(f"fork worktree {verb}: {worktree.name}")
            if apply:
                _remove_worktree(worktree)
    print(f"{len(references)} snapshots referenced by journals; {verb} {len(pruned)} refs")
    for pruned_ref in pruned:
        print(f"  {pruned_ref.sha} ({pruned_ref.reason})")
        # An expired snapshot is still referenced: name the steps that lose
        # fork-ability instead of reporting a bare count (PR #19 review, P1).
        for session, step in references.get(pruned_ref.sha, []):
            print(f"    fork lost: session {session} step {step}")
    return 0


def _hook_main(config: SpotterConfig | None, config_path: Path | None = None) -> int:
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
        output = run_hook(payload, config, config_path)
    except Exception as error:  # noqa: BLE001 — deliberate fail-open boundary
        print(f"spotter hook error (failing open): {error}", file=sys.stderr)
        return 0
    if output is not None:
        print(output)
    return 0
