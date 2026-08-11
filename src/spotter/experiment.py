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
from dataclasses import asdict, dataclass
from pathlib import Path

from spotter.replay import fork

CONTROL_PROMPT = "Continue the task."


@dataclass(frozen=True)
class ArmResult:
    pair: int
    arm: str  # "control" | "guidance"
    session_id: str
    worktree: str
    agent_exit: int | None  # None = not run
    check_exit: int | None  # None = no check command


def results_path(session_id: str, step: int) -> Path:
    base = Path(os.environ.get("SPOTTER_HOME", Path.home() / ".spotter")) / "experiments"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{session_id}-step{step}.jsonl"


def _run_arm(
    plan_session: str,
    worktree: str,
    prompt: str,
    *,
    sandbox: str,
    timeout: int,
) -> int:
    result = subprocess.run(
        [
            "codex",
            "exec",
            "-C",
            worktree,
            "--skip-git-repo-check",
            "--sandbox",
            sandbox,
            "resume",
            plan_session,
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "SPOTTER_DISABLE": "1"},
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
) -> list[ArmResult]:
    """Build (and with run=True, execute) n counterfactual pairs."""
    out = results_path(session_id, step)
    results: list[ArmResult] = []
    for pair in range(pairs):
        for arm, prompt in (
            ("control", CONTROL_PROMPT),
            ("guidance", f"{CONTROL_PROMPT} {guidance}"),
        ):
            plan = fork(session_id, step, codex_home=codex_home)
            agent_exit: int | None = None
            check_exit: int | None = None
            if run:
                agent_exit = _run_arm(
                    plan.session_id, plan.worktree, prompt, sandbox=sandbox, timeout=timeout
                )
                if check:
                    check_exit = subprocess.run(
                        check, shell=True, cwd=plan.worktree, capture_output=True, timeout=timeout
                    ).returncode
            result = ArmResult(pair, arm, plan.session_id, plan.worktree, agent_exit, check_exit)
            results.append(result)
            with out.open("a", encoding="utf-8") as sink:  # one row per run, crash-safe
                sink.write(json.dumps(asdict(result)) + "\n")
    return results


def summarize(results: list[ArmResult]) -> str:
    lines = []
    for arm in ("control", "guidance"):
        rows = [r for r in results if r.arm == arm]
        ran = [r for r in rows if r.agent_exit is not None]
        passed = [r for r in ran if r.check_exit == 0]
        checked = [r for r in ran if r.check_exit is not None]
        if not ran:
            lines.append(f"{arm}: {len(rows)} fork(s) prepared, not run")
        elif not checked:
            lines.append(f"{arm}: {len(ran)} run(s), no --check given — completion is not success")
        else:
            lines.append(f"{arm}: {len(passed)}/{len(checked)} passed check")
    return "\n".join(lines)
