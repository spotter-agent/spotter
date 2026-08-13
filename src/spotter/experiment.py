"""Same-prefix counterfactual experiment: does the nudge actually help? (plan Q3)

For one branch point, build n pairs of forks and run either:
- control:  resumed with a neutral "Continue the task."
- guidance: resumed with "Continue the task." + the nudge text
- neutral noise: two independent arms resumed with the same control prompt

Both arms receive a user message, so guidance mode changes content rather
than prompt presence, while neutral mode uses identical content. Success is
judged by an explicit --check command run in each fork's worktree afterward;
without a check the experiment records completion only and says so, because
"the agent finished" is not "the agent succeeded".

This is the expensive machine (2n real agent runs). It never starts without
--run, runs arms sequentially, and journals every result row immediately so
a crash loses at most one run.
"""

import json
import os
import subprocess
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from spotter.hook import journal_path
from spotter.paths import sanitize_session, spotter_home
from spotter.replay import ForkPlan, compare_environments, fork, load_fork_manifest
from spotter.snapshot import StepJournal

CONTROL_PROMPT = "Continue the task."
EXPERIMENT_RESULT_SCHEMA_VERSION = 2
_OUTPUT_LIMIT = 4000


class ArmClassification(StrEnum):
    PASS = "PASS"
    TASK_FAIL = "TASK_FAIL"
    SETUP_FAIL = "SETUP_FAIL"
    INFRA_FAIL = "INFRA_FAIL"
    TIMEOUT_AGENT = "TIMEOUT_AGENT"
    TIMEOUT_CHECK = "TIMEOUT_CHECK"
    CHECK_ERROR = "CHECK_ERROR"
    UNJUDGEABLE = "UNJUDGEABLE"


@dataclass(frozen=True)
class ArmResult:
    experiment_id: str
    pair: int
    arm: str  # "control" | "guidance" | "neutral_a" | "neutral_b"
    session_id: str
    worktree: str
    agent_exit: int | None  # None = not run
    check_exit: int | None  # None = no check command
    classification: ArmClassification
    check_stdout: str = ""
    check_stderr: str = ""
    infra_diagnostic: str | None = None
    result_schema_version: int = EXPERIMENT_RESULT_SCHEMA_VERSION
    fork_manifest: str | None = None
    prefix_id: str | None = None
    environment_fingerprint: str | None = None
    environment_preflight: str | None = None
    experiment_mode: str = "guidance"


def results_path(session_id: str, step: int) -> Path:
    base = spotter_home() / "experiments"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{sanitize_session(session_id)}-step{step}.jsonl"


def _run_arm(
    plan_session: str,
    worktree: str,
    prompt: str,
    *,
    sandbox: str,
    timeout: int,
    model: str | None = None,
    reasoning_effort: str | None = None,
    codex_home: Path | None = None,
) -> int:
    args = ["codex", "exec", "-C", worktree]
    if model:
        args += ["--model", model]
    if reasoning_effort:
        args += ["--config", f'model_reasoning_effort="{reasoning_effort}"']
    args += [
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "resume",
        plan_session,
        prompt,
    ]
    env = {**os.environ, "SPOTTER_DISABLE": "1"}
    if codex_home:
        env["CODEX_HOME"] = str(codex_home)
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return result.returncode


def _pair_environment_preflight(prepared: list[tuple[str, str, ForkPlan]]) -> str:
    plans = [plan for _, _, plan in prepared]
    if len(plans) != 2 or any(
        plan.prefix_id is None or plan.environment_fingerprint is None for plan in plans
    ):
        return "FORK_PROVENANCE_UNAVAILABLE"
    if plans[0].prefix_id != plans[1].prefix_id:
        return "PREFIX_MISMATCH"
    if plans[0].environment_fingerprint == plans[1].environment_fingerprint:
        return "MATCHED"
    if not plans[0].manifest or not plans[1].manifest:
        return "ENVIRONMENT_FINGERPRINT_MISMATCH"
    left = load_fork_manifest(Path(plans[0].manifest))
    right = load_fork_manifest(Path(plans[1].manifest))
    if left.environment is None or right.environment is None:
        return "ENVIRONMENT_FINGERPRINT_MISSING"
    comparison = compare_environments(left.environment, right.environment)
    detail = ",".join(comparison.drift) or "UNKNOWN_ENVIRONMENT_DRIFT"
    return f"ENVIRONMENT_MISMATCH:{detail}"


def _execute_arm(
    plan: ForkPlan,
    prompt: str,
    environment_preflight: str,
    *,
    check: str | None,
    sandbox: str,
    timeout: int,
    model: str | None,
    reasoning_effort: str | None,
    codex_home: Path | None,
) -> tuple[int | None, int | None, ArmClassification, str, str, str | None]:
    if environment_preflight != "MATCHED":
        return None, None, ArmClassification.INFRA_FAIL, "", "", environment_preflight
    try:
        agent_exit = _run_arm(
            plan.session_id,
            plan.worktree,
            prompt,
            sandbox=sandbox,
            timeout=timeout,
            model=model,
            reasoning_effort=reasoning_effort,
            codex_home=codex_home,
        )
    except subprocess.TimeoutExpired as error:
        return None, None, ArmClassification.TIMEOUT_AGENT, "", "", str(error)
    except OSError as error:
        return None, None, ArmClassification.INFRA_FAIL, "", "", str(error)
    if agent_exit != 0:
        return agent_exit, None, ArmClassification.INFRA_FAIL, "", "", f"agent exited {agent_exit}"
    if not check:
        return agent_exit, None, ArmClassification.UNJUDGEABLE, "", "", None
    try:
        completed = subprocess.run(
            check,
            shell=True,
            cwd=plan.worktree,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        return (
            agent_exit,
            None,
            ArmClassification.TIMEOUT_CHECK,
            _bounded_output(error.stdout),
            _bounded_output(error.stderr),
            None,
        )
    except OSError as error:
        return agent_exit, None, ArmClassification.CHECK_ERROR, "", "", str(error)
    classification = (
        ArmClassification.PASS if completed.returncode == 0 else ArmClassification.TASK_FAIL
    )
    return (
        agent_exit,
        completed.returncode,
        classification,
        _bounded_output(completed.stdout),
        _bounded_output(completed.stderr),
        None,
    )


def run_experiment(
    session_id: str,
    step: int,
    guidance: str | None,
    *,
    pairs: int = 1,
    check: str | None = None,
    run: bool = False,
    sandbox: str = "workspace-write",
    timeout: int = 1800,
    codex_home: Path | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    keep_artifacts: bool = False,
    neutral: bool = False,
) -> list[ArmResult]:
    """Build (and with run=True, execute) n counterfactual pairs."""
    if pairs < 1:
        raise ValueError("pairs must be >= 1 — an empty experiment is not a successful one")
    if neutral and guidance:
        raise ValueError("neutral noise mode cannot include guidance")
    if not neutral and not guidance:
        raise ValueError("guidance is required unless neutral noise mode is selected")
    experiment_mode = "neutral-noise" if neutral else "guidance"
    out = results_path(session_id, step)
    experiment_id = str(uuid.uuid4())
    source_snapshot = _source_snapshot(session_id, step)
    # Provenance header: rows are meaningless as measurements unless the exact
    # conditions that produced them can be recovered and compared.
    meta = {
        "meta": True,
        "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "experiment_mode": experiment_mode,
        "source_session": session_id,
        "step": step,
        "guidance": guidance,
        "control_prompt": CONTROL_PROMPT,
        "check": check,
        "sandbox": sandbox,
        "timeout": timeout,
        "run": run,
        "pairs": pairs,
        "model": model or "codex-config-default",
        "reasoning_effort": reasoning_effort or "codex-config-default",
        "codex_version": _codex_version(),
        "codex_home": str(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
        "source_snapshot": source_snapshot,
        "started_at": datetime.now(UTC).isoformat(),
    }
    with out.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(meta) + "\n")
    results: list[ArmResult] = []
    for pair in range(pairs):
        arms = (
            [("neutral_a", CONTROL_PROMPT), ("neutral_b", CONTROL_PROMPT)]
            if neutral
            else [("control", CONTROL_PROMPT), ("guidance", f"{CONTROL_PROMPT} {guidance}")]
        )
        if (pair + uuid.UUID(experiment_id).int) % 2:  # randomize pair 0, then alternate
            arms.reverse()
        prepared = [
            (arm, prompt, fork(session_id, step, codex_home=codex_home)) for arm, prompt in arms
        ]
        environment_preflight = _pair_environment_preflight(prepared)
        for arm, prompt, plan in prepared:
            if run:
                execution = _execute_arm(
                    plan,
                    prompt,
                    environment_preflight,
                    check=check,
                    sandbox=sandbox,
                    timeout=timeout,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    codex_home=codex_home,
                )
            else:
                execution = (None, None, ArmClassification.UNJUDGEABLE, "", "", None)
            agent_exit, check_exit, classification, check_stdout, check_stderr, diagnostic = (
                execution
            )
            result = ArmResult(
                experiment_id,
                pair,
                arm,
                plan.session_id,
                plan.worktree,
                agent_exit,
                check_exit,
                classification,
                check_stdout,
                check_stderr,
                diagnostic,
                fork_manifest=plan.manifest,
                prefix_id=plan.prefix_id,
                environment_fingerprint=plan.environment_fingerprint,
                environment_preflight=environment_preflight,
                experiment_mode=experiment_mode,
            )
            results.append(result)
            with out.open("a", encoding="utf-8") as sink:  # one row per run, crash-safe
                sink.write(json.dumps(asdict(result)) + "\n")
            if run and not keep_artifacts:
                _cleanup(plan.worktree)
    with out.open("a", encoding="utf-8") as sink:
        sink.write(
            json.dumps(
                {
                    "complete": True,
                    "experiment_id": experiment_id,
                    "results": len(results),
                    "finished_at": datetime.now(UTC).isoformat(),
                }
            )
            + "\n"
        )
    return results


def _codex_version() -> str | None:
    try:
        result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
        return str(getattr(result, "stdout", "")).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def forks_dir() -> Path:
    return spotter_home() / "forks"


def list_forks() -> list[Path]:
    """Fork worktrees currently on disk.

    Prepare-only forks (``spotter fork``, or ``experiment`` without ``--run``)
    were never cleaned up by anything, so they accumulated a full checkout each.
    """
    base = forks_dir()
    return sorted(p for p in base.glob("*") if p.is_dir()) if base.exists() else []


def _cleanup(worktree: str) -> None:
    if Path(worktree).exists():
        with suppress(OSError):
            subprocess.run(
                ["git", "-C", worktree, "worktree", "remove", "--force", worktree],
                capture_output=True,
                check=False,
            )


def _source_snapshot(session_id: str, step: int) -> str | None:
    try:
        records = StepJournal.load(journal_path({"session_id": session_id}))
    except FileNotFoundError:
        return None
    return next((r.snapshot for r in reversed(records[: step + 1]) if r.snapshot), None)


def _bounded_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return (value or "")[-_OUTPUT_LIMIT:]


def summarize(results: list[ArmResult]) -> str:
    lines = []
    arms = (
        ("neutral_a", "neutral_b")
        if any(result.experiment_mode == "neutral-noise" for result in results)
        else ("control", "guidance")
    )
    for arm in arms:
        rows = [r for r in results if r.arm == arm]
        attempted = [
            r
            for r in rows
            if r.agent_exit is not None or r.classification != ArmClassification.UNJUDGEABLE
        ]
        judged = [
            r
            for r in rows
            if r.classification in {ArmClassification.PASS, ArmClassification.TASK_FAIL}
        ]
        passed = [r for r in judged if r.classification == ArmClassification.PASS]
        invalid = [
            r
            for r in rows
            if r.classification
            not in {
                ArmClassification.PASS,
                ArmClassification.TASK_FAIL,
                ArmClassification.UNJUDGEABLE,
            }
        ]
        if not attempted:
            lines.append(f"{arm}: {len(rows)} fork(s) prepared, not run")
        elif not judged and not invalid:
            lines.append(
                f"{arm}: {len(attempted)} run(s), no --check given — completion is not success"
            )
        elif not judged:
            counts = ", ".join(
                f"{classification}={sum(r.classification == classification for r in invalid)}"
                for classification in ArmClassification
                if any(r.classification == classification for r in invalid)
            )
            lines.append(f"{arm}: {len(invalid)} invalid run(s), no result ({counts})")
        else:
            suffix = f", {len(invalid)} invalid run(s)" if invalid else ""
            lines.append(f"{arm}: {len(passed)}/{len(judged)} passed check{suffix}")
    pairs = {result.pair for result in results}
    if arms == ("neutral_a", "neutral_b"):
        judgeable_pairs = disagreements = 0
        for pair in pairs:
            pair_rows = {result.arm: result for result in results if result.pair == pair}
            if set(pair_rows) != set(arms) or any(
                row.classification not in {ArmClassification.PASS, ArmClassification.TASK_FAIL}
                for row in pair_rows.values()
            ):
                continue
            judgeable_pairs += 1
            disagreements += (
                pair_rows["neutral_a"].classification != pair_rows["neutral_b"].classification
            )
        environment_mismatches = len(
            {
                result.pair
                for result in results
                if result.environment_preflight not in {None, "MATCHED"}
            }
        )
        infrastructure_failures = sum(
            result.classification
            not in {
                ArmClassification.PASS,
                ArmClassification.TASK_FAIL,
                ArmClassification.UNJUDGEABLE,
            }
            for result in results
        )
        rate = f"{disagreements / judgeable_pairs:.1%}" if judgeable_pairs else "unknown"
        lines.append(
            f"noise pairs: n={judgeable_pairs}/{len(pairs)} judgeable; mechanical outcome "
            f"disagreements={disagreements}/{judgeable_pairs} ({rate}); environment mismatches="
            f"{environment_mismatches}/{len(pairs)}; infrastructure failures="
            f"{infrastructure_failures}/{len(results)}"
        )
        return "\n".join(lines)
    guidance_better = control_better = tied = complete = 0
    for pair in pairs:
        pair_rows = {result.arm: result for result in results if result.pair == pair}
        if set(pair_rows) != {"control", "guidance"} or any(
            row.classification not in {ArmClassification.PASS, ArmClassification.TASK_FAIL}
            for row in pair_rows.values()
        ):
            continue
        complete += 1
        control_passed = pair_rows["control"].classification == ArmClassification.PASS
        guidance_passed = pair_rows["guidance"].classification == ArmClassification.PASS
        guidance_better += guidance_passed and not control_passed
        control_better += control_passed and not guidance_passed
        tied += control_passed == guidance_passed
    lines.append(
        f"pairs: n={complete}/{len(pairs)} complete; guidance better={guidance_better}, "
        f"control better={control_better}, tied={tied}"
    )
    return "\n".join(lines)
