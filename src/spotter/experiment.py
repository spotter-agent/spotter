"""Same-prefix counterfactual experiment: does the nudge actually help? (plan Q3)

For one branch point, build n pairs of forks and run both arms:
- control:  resumed with a neutral "Continue the task."
- guidance: resumed with "Continue the task." + the nudge text

Both arms receive a user message, so the only difference is the guidance
content — prompt *presence* is not a confound. Success is judged by an
explicit --check command run in each fork's worktree afterward; without a
check the experiment records completion only and says so, because "the agent
finished" is not "the agent succeeded".

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
from pathlib import Path

from spotter.hook import journal_path
from spotter.paths import sanitize_session, spotter_home
from spotter.replay import fork
from spotter.snapshot import StepJournal

CONTROL_PROMPT = "Continue the task."


@dataclass(frozen=True)
class ArmResult:
    experiment_id: str
    pair: int
    arm: str  # "control" | "guidance"
    session_id: str
    worktree: str
    agent_exit: int | None  # None = not run
    check_exit: int | None  # None = no check command


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
    codex_home: Path | None = None,
) -> int:
    args = ["codex", "exec", "-C", worktree]
    if model:
        args += ["--model", model]
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


def run_experiment(
    session_id: str,
    step: int,
    guidance: str,
    *,
    pairs: int = 1,
    check: str | None = None,
    run: bool = False,
    sandbox: str = "workspace-write",
    timeout: int = 1800,
    codex_home: Path | None = None,
    model: str | None = None,
    keep_artifacts: bool = False,
    kind: str = "nudge",
    guidance_class: str | None = None,
) -> list[ArmResult]:
    """Build (and with run=True, execute) n counterfactual pairs."""
    if pairs < 1:
        raise ValueError("pairs must be >= 1 — an empty experiment is not a successful one")
    out = results_path(session_id, step)
    experiment_id = str(uuid.uuid4())
    source_snapshot = _source_snapshot(session_id, step)
    # Provenance header: rows are meaningless as measurements unless the exact
    # conditions that produced them can be recovered and compared.
    meta = {
        "meta": True,
        "experiment_id": experiment_id,
        "source_session": session_id,
        "step": step,
        "guidance": guidance,
        "kind": kind,
        "guidance_class": guidance_class,
        "control_prompt": CONTROL_PROMPT,
        "check": check,
        "sandbox": sandbox,
        "timeout": timeout,
        "run": run,
        "pairs": pairs,
        "model": model or "codex-config-default",
        "codex_version": _codex_version(),
        "codex_home": str(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex"),
        "source_snapshot": source_snapshot,
        "started_at": datetime.now(UTC).isoformat(),
    }
    with out.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(meta) + "\n")
    results: list[ArmResult] = []
    for pair in range(pairs):
        arms = [
            ("control", CONTROL_PROMPT),
            ("guidance", f"{CONTROL_PROMPT} {guidance}"),
        ]
        if (pair + uuid.UUID(experiment_id).int) % 2:  # randomize pair 0, then alternate
            arms.reverse()
        for arm, prompt in arms:
            plan = fork(session_id, step, codex_home=codex_home)
            agent_exit: int | None = None
            check_exit: int | None = None
            if run:
                try:
                    agent_exit = _run_arm(
                        plan.session_id,
                        plan.worktree,
                        prompt,
                        sandbox=sandbox,
                        timeout=timeout,
                        model=model,
                        codex_home=codex_home,
                    )
                except subprocess.TimeoutExpired:
                    agent_exit = 124
                if check and agent_exit == 0:
                    try:
                        check_exit = subprocess.run(
                            check,
                            shell=True,
                            cwd=plan.worktree,
                            capture_output=True,
                            timeout=timeout,
                        ).returncode
                    except subprocess.TimeoutExpired:
                        check_exit = 124
            result = ArmResult(
                experiment_id, pair, arm, plan.session_id, plan.worktree, agent_exit, check_exit
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


def summarize(results: list[ArmResult]) -> str:
    lines = []
    for arm in ("control", "guidance"):
        rows = [r for r in results if r.arm == arm]
        ran = [r for r in rows if r.agent_exit is not None]
        valid = [r for r in ran if r.agent_exit == 0]
        invalid = len(ran) - len(valid)
        passed = [r for r in valid if r.check_exit == 0]
        checked = [r for r in valid if r.check_exit is not None]
        if not ran:
            lines.append(f"{arm}: {len(rows)} fork(s) prepared, not run")
        elif not valid:
            lines.append(f"{arm}: {invalid} invalid agent run(s), no result")
        elif not checked:
            lines.append(
                f"{arm}: {len(valid)} run(s), no --check given — completion is not success"
            )
        else:
            suffix = f", {invalid} invalid agent run(s)" if invalid else ""
            lines.append(f"{arm}: {len(passed)}/{len(checked)} passed check{suffix}")
    pairs = {result.pair for result in results}
    guidance_better = control_better = tied = complete = 0
    for pair in pairs:
        pair_rows = {result.arm: result for result in results if result.pair == pair}
        if set(pair_rows) != {"control", "guidance"} or any(
            row.agent_exit != 0 or row.check_exit is None for row in pair_rows.values()
        ):
            continue
        complete += 1
        control_passed = pair_rows["control"].check_exit == 0
        guidance_passed = pair_rows["guidance"].check_exit == 0
        guidance_better += guidance_passed and not control_passed
        control_better += control_passed and not guidance_passed
        tied += control_passed == guidance_passed
    lines.append(
        f"pairs: n={complete}/{len(pairs)} complete; guidance better={guidance_better}, "
        f"control better={control_better}, tied={tied}"
    )
    return "\n".join(lines)
