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
import re
import subprocess
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from fcntl import LOCK_EX, flock
from pathlib import Path

from spotter.hook import journal_path
from spotter.paths import sanitize_session, spotter_home
from spotter.replay import (
    ForkPlan,
    ReplayError,
    compare_environments,
    fingerprint_environment,
    fork,
    load_fork_manifest,
    prefix_contamination_preflight,
)
from spotter.snapshot import StepJournal

CONTROL_PROMPT = "Continue the task."
EXPERIMENT_RESULT_SCHEMA = "spotter.experiment_result"
EXPERIMENT_RESULT_SCHEMA_VERSION = 3
_OUTPUT_LIMIT = 4000
_TOKENS_USED_RE = re.compile(r"tokens used\s*\n\s*([0-9][0-9,]*)", re.IGNORECASE)


class ExperimentResultError(ValueError):
    """A durable experiment result history is incompatible or corrupt."""


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
    agent_reported_tokens: int | None = None
    agent_elapsed_ms: float | None = None


def results_path(session_id: str, step: int) -> Path:
    base = spotter_home() / "experiments"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{sanitize_session(session_id)}-step{step}.jsonl"


def append_experiment_result(path: Path, row: dict[str, object]) -> None:
    """Append and fsync one schema-checked experiment result row."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a") as lock:
        flock(lock, LOCK_EX)
        _validate_result_history(path)
        _write_result_row(path, row)


def initialize_experiment_result(path: Path, row: dict[str, object]) -> None:
    """Write a result header once while concurrent initializers share the lock."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a") as lock:
        flock(lock, LOCK_EX)
        _validate_result_history(path)
        if path.exists() and path.stat().st_size:
            return
        _write_result_row(path, row)


def _write_result_row(path: Path, row: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(row) + "\n")
        sink.flush()
        os.fsync(sink.fileno())


def _validate_result_history(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as error:
        raise ExperimentResultError(f"{path.name} is not valid UTF-8 ({error})") from error
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise TypeError("record is not an object")
            schema = raw.get("schema")
            schema_version = raw.get("schema_version")
            version = raw.get("result_schema_version")
            legacy_completion = (
                schema is None
                and schema_version is None
                and version is None
                and raw.get("complete") is True
            )
            if not legacy_completion and (
                not isinstance(version, int) or isinstance(version, bool)
            ):
                raise ExperimentResultError(
                    f"{path.name} line {number} has a non-integer result schema version"
                )
            if schema is None and schema_version is None:
                pass
            elif schema != EXPERIMENT_RESULT_SCHEMA:
                raise ExperimentResultError(
                    f"{path.name} line {number} uses unsupported schema {schema!r}"
                )
            elif not isinstance(schema_version, int) or isinstance(schema_version, bool):
                raise ExperimentResultError(
                    f"{path.name} line {number} has a non-integer schema version"
                )
            elif schema_version != version:
                raise ExperimentResultError(
                    f"{path.name} line {number} has mismatched schema versions"
                )
            if isinstance(version, int) and version not in {1, 2, EXPERIMENT_RESULT_SCHEMA_VERSION}:
                raise ExperimentResultError(
                    f"{path.name} line {number} uses result schema v{version}; "
                    f"this build understands up to v{EXPERIMENT_RESULT_SCHEMA_VERSION}"
                )
        except (json.JSONDecodeError, TypeError, UnicodeError) as error:
            raise ExperimentResultError(
                f"{path.name} line {number} is unreadable ({error})"
            ) from error


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
) -> subprocess.CompletedProcess[str]:
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
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _pair_environment_preflight(prepared: list[tuple[str, str, ForkPlan]]) -> str:
    plans = [plan for _, _, plan in prepared]
    if len(plans) != 2 or any(
        plan.prefix_id is None or plan.environment_fingerprint is None for plan in plans
    ):
        return "FORK_PROVENANCE_UNAVAILABLE"
    if Path(plans[0].worktree).resolve() == Path(plans[1].worktree).resolve():
        return "SHARED_ARM_WORKTREE"
    if contamination := prefix_contamination_preflight(plans):
        return contamination
    source_mismatch = next(
        (
            plan.source_environment_preflight
            for plan in plans
            if plan.source_environment_preflight != "MATCHED"
        ),
        None,
    )
    if source_mismatch is not None:
        return source_mismatch
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


def _source_config_preflight(
    prepared: list[tuple[str, str, ForkPlan]],
    model: str | None,
    reasoning_effort: str | None,
) -> str:
    if model is None and reasoning_effort is None:
        return "MATCHED"
    manifest_path = next((plan.manifest for _, _, plan in prepared if plan.manifest), None)
    if manifest_path is None:
        return "SOURCE_CONFIG_UNAVAILABLE"
    try:
        prefix = load_fork_manifest(Path(manifest_path)).prefix
        if model is not None and prefix.model != model:
            return "SOURCE_MODEL_MISMATCH" if prefix.model else "SOURCE_MODEL_UNAVAILABLE"
        if reasoning_effort is not None:
            if prefix.agent_config == "not_captured":
                return "SOURCE_REASONING_EFFORT_UNAVAILABLE"
            config = json.loads(prefix.agent_config)
            source_effort = config.get("effort") if isinstance(config, dict) else None
            if not isinstance(source_effort, str):
                return "SOURCE_REASONING_EFFORT_UNAVAILABLE"
            if source_effort != reasoning_effort:
                return "SOURCE_REASONING_EFFORT_MISMATCH"
    except (OSError, ReplayError, ValueError) as error:
        return f"SOURCE_CONFIG_ERROR:{error}"
    return "MATCHED"


def _arm_environment_preflight(plan: ForkPlan) -> str:
    try:
        if not plan.manifest:
            current = fingerprint_environment(Path(plan.worktree))
            return (
                "MATCHED"
                if current.fingerprint_sha256 == plan.environment_fingerprint
                else "ENVIRONMENT_FINGERPRINT_MISMATCH"
            )
        expected = load_fork_manifest(Path(plan.manifest)).environment
        if expected is None:
            return "ENVIRONMENT_FINGERPRINT_MISSING"
        resources = tuple(resource.path for resource in getattr(expected, "declared_resources", ()))
        venv_or_cache = tuple(
            resource.path
            for resource in getattr(expected, "declared_resources", ())
            if getattr(resource, "purpose", "resource") == "venv_or_cache"
        )
        resources = tuple(path for path in resources if path not in venv_or_cache)
        variables = tuple(
            variable.name for variable in getattr(expected, "declared_environment_variables", ())
        )
        current = fingerprint_environment(Path(plan.worktree), resources, variables, venv_or_cache)
        if current.fingerprint_sha256 == plan.environment_fingerprint:
            return "MATCHED"
        comparison = compare_environments(expected, current)
        if comparison.equivalent:
            return "MATCHED"
        detail = ",".join(comparison.drift) or "UNKNOWN_ENVIRONMENT_DRIFT"
        return f"ENVIRONMENT_MISMATCH:{detail}"
    except (OSError, ReplayError, ValueError) as error:
        return f"ENVIRONMENT_PREFLIGHT_ERROR:{error}"


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
) -> tuple[
    int | None,
    int | None,
    ArmClassification,
    str,
    str,
    str | None,
    int | None,
    float | None,
]:
    if environment_preflight != "MATCHED":
        return None, None, ArmClassification.INFRA_FAIL, "", "", environment_preflight, None, None
    if plan.manifest:
        environment_preflight = _arm_environment_preflight(plan)
        if environment_preflight != "MATCHED":
            return (
                None,
                None,
                ArmClassification.INFRA_FAIL,
                "",
                "",
                environment_preflight,
                None,
                None,
            )
    started_ns = time.monotonic_ns()
    try:
        agent = _run_arm(
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
        return (
            None,
            None,
            ArmClassification.TIMEOUT_AGENT,
            "",
            "",
            str(error),
            _reported_tokens(error.stderr),
            (time.monotonic_ns() - started_ns) / 1_000_000,
        )
    except OSError as error:
        return (
            None,
            None,
            ArmClassification.INFRA_FAIL,
            "",
            "",
            str(error),
            None,
            (time.monotonic_ns() - started_ns) / 1_000_000,
        )
    agent_exit = agent.returncode
    agent_reported_tokens = _reported_tokens(agent.stderr)
    agent_elapsed_ms = (time.monotonic_ns() - started_ns) / 1_000_000
    if agent_exit != 0:
        return (
            agent_exit,
            None,
            ArmClassification.INFRA_FAIL,
            "",
            "",
            f"agent exited {agent_exit}",
            agent_reported_tokens,
            agent_elapsed_ms,
        )
    if not check:
        return (
            agent_exit,
            None,
            ArmClassification.UNJUDGEABLE,
            "",
            "",
            None,
            agent_reported_tokens,
            agent_elapsed_ms,
        )
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
            agent_reported_tokens,
            agent_elapsed_ms,
        )
    except OSError as error:
        return (
            agent_exit,
            None,
            ArmClassification.CHECK_ERROR,
            "",
            "",
            str(error),
            agent_reported_tokens,
            agent_elapsed_ms,
        )
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
        agent_reported_tokens,
        agent_elapsed_ms,
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
    environment_resources: Sequence[str] = (),
    environment_variables: Sequence[str] = (),
    environment_venv_or_cache: Sequence[str] = (),
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
        "schema": EXPERIMENT_RESULT_SCHEMA,
        "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
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
        "environment_resources": list(environment_resources),
        "environment_variables": list(environment_variables),
        "environment_venv_or_cache": list(environment_venv_or_cache),
        "started_at": datetime.now(UTC).isoformat(),
    }
    append_experiment_result(out, meta)
    results: list[ArmResult] = []
    for pair in range(pairs):
        arms = (
            [("neutral_a", CONTROL_PROMPT), ("neutral_b", CONTROL_PROMPT)]
            if neutral
            else [("control", CONTROL_PROMPT), ("guidance", f"{CONTROL_PROMPT} {guidance}")]
        )
        if (pair + uuid.UUID(experiment_id).int) % 2:  # randomize pair 0, then alternate
            arms.reverse()
        if environment_resources or environment_variables or environment_venv_or_cache:
            prepared = [
                (
                    arm,
                    prompt,
                    fork(
                        session_id,
                        step,
                        codex_home=codex_home,
                        environment_resources=environment_resources,
                        environment_variables=environment_variables,
                        environment_venv_or_cache=environment_venv_or_cache,
                    ),
                )
                for arm, prompt in arms
            ]
        else:
            prepared = [
                (arm, prompt, fork(session_id, step, codex_home=codex_home)) for arm, prompt in arms
            ]
        environment_preflight = _pair_environment_preflight(prepared)
        source_config_preflight = _source_config_preflight(prepared, model, reasoning_effort)
        for arm, prompt, plan in prepared:
            execution: tuple[
                int | None,
                int | None,
                ArmClassification,
                str,
                str,
                str | None,
                int | None,
                float | None,
            ]
            if run:
                if source_config_preflight != "MATCHED":
                    execution = (
                        None,
                        None,
                        ArmClassification.SETUP_FAIL,
                        "",
                        "",
                        source_config_preflight,
                        None,
                        None,
                    )
                else:
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
                execution = (None, None, ArmClassification.UNJUDGEABLE, "", "", None, None, None)
            (
                agent_exit,
                check_exit,
                classification,
                check_stdout,
                check_stderr,
                diagnostic,
                agent_reported_tokens,
                agent_elapsed_ms,
            ) = execution
            if diagnostic and diagnostic.startswith("ENVIRONMENT_"):
                environment_preflight = diagnostic
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
                agent_reported_tokens=agent_reported_tokens,
                agent_elapsed_ms=agent_elapsed_ms,
            )
            results.append(result)
            append_experiment_result(
                out,
                {
                    "schema": EXPERIMENT_RESULT_SCHEMA,
                    "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
                    **asdict(result),
                },
            )
            if run and not keep_artifacts:
                _cleanup(plan.worktree)
    append_experiment_result(
        out,
        {
            "schema": EXPERIMENT_RESULT_SCHEMA,
            "schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "result_schema_version": EXPERIMENT_RESULT_SCHEMA_VERSION,
            "complete": True,
            "experiment_id": experiment_id,
            "results": len(results),
            "finished_at": datetime.now(UTC).isoformat(),
        },
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


def _reported_tokens(value: str | bytes | None) -> int | None:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    matches = _TOKENS_USED_RE.findall(value or "")
    return int(matches[-1].replace(",", "")) if matches else None


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
        preflight_failures = len(
            {
                result.pair
                for result in results
                if result.environment_preflight not in {None, "MATCHED"}
                or result.classification == ArmClassification.SETUP_FAIL
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
            f"disagreements={disagreements}/{judgeable_pairs} ({rate}); preflight failures="
            f"{preflight_failures}/{len(pairs)}; infrastructure failures="
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
