"""Prepare equivalent-prefix forks for wrong-nudge susceptibility experiments."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from spotter.replay import ForkPlan, fork
from spotter.wrong_nudge_corpus import (
    FramingCondition,
    WrongNudge,
    WrongNudgeArm,
    build_wrong_nudge_arms,
)


class WrongNudgeExperimentError(ValueError):
    """Wrong-nudge arms cannot be compared or safely delivered."""


@dataclass(frozen=True)
class PreparedWrongNudgeArm:
    arm: WrongNudgeArm
    fork_session_id: str
    worktree: str
    rollout: str
    fork_manifest: str


def prepare_wrong_nudge_arms(
    nudge: WrongNudge,
    source_session_id: str,
    source_step: int,
    *,
    repo: Path | None = None,
    codex_home: Path | None = None,
    environment_resources: Sequence[str] = (),
    environment_variables: Sequence[str] = (),
    environment_venv_or_cache: Sequence[str] = (),
) -> tuple[PreparedWrongNudgeArm, ...]:
    """Fork every condition independently, then refuse non-equivalent prefixes."""

    if not source_session_id.strip():
        raise WrongNudgeExperimentError("source_session_id must be non-empty text")
    if isinstance(source_step, bool) or source_step < 0:
        raise WrongNudgeExperimentError("source_step must be non-negative")
    plans = tuple(
        fork(
            source_session_id,
            source_step,
            repo=repo,
            codex_home=codex_home,
            environment_resources=environment_resources,
            environment_variables=environment_variables,
            environment_venv_or_cache=environment_venv_or_cache,
        )
        for _ in FramingCondition
    )
    prefix_id, environment_fingerprint = _preflight_forks(plans)
    arms = build_wrong_nudge_arms(
        nudge,
        source_session_id=source_session_id,
        source_step=source_step,
        prefix_id=prefix_id,
        environment_fingerprint=environment_fingerprint,
    )
    return tuple(
        PreparedWrongNudgeArm(
            arm=arm,
            fork_session_id=plan.session_id,
            worktree=plan.worktree,
            rollout=plan.rollout,
            fork_manifest=plan.manifest or "",
        )
        for arm, plan in zip(arms, plans, strict=True)
    )


def _preflight_forks(plans: tuple[ForkPlan, ...]) -> tuple[str, str]:
    if len(plans) != len(FramingCondition):
        raise WrongNudgeExperimentError(
            f"wrong-nudge experiment requires exactly {len(FramingCondition)} forks"
        )
    if len({plan.session_id for plan in plans}) != len(plans):
        raise WrongNudgeExperimentError("DUPLICATE_FORK_SESSION")
    if len({Path(plan.worktree).resolve() for plan in plans}) != len(plans):
        raise WrongNudgeExperimentError("SHARED_ARM_WORKTREE")
    source_mismatch = next(
        (
            plan.source_environment_preflight
            for plan in plans
            if plan.source_environment_preflight != "MATCHED"
        ),
        None,
    )
    if source_mismatch is not None:
        raise WrongNudgeExperimentError(source_mismatch)
    if any(not plan.manifest for plan in plans):
        raise WrongNudgeExperimentError("FORK_MANIFEST_UNAVAILABLE")
    prefix_ids = {plan.prefix_id for plan in plans}
    if None in prefix_ids or "" in prefix_ids:
        raise WrongNudgeExperimentError("FORK_PROVENANCE_UNAVAILABLE")
    if len(prefix_ids) != 1:
        raise WrongNudgeExperimentError("PREFIX_MISMATCH")
    fingerprints = {plan.environment_fingerprint for plan in plans}
    if None in fingerprints or "" in fingerprints:
        raise WrongNudgeExperimentError("ENVIRONMENT_FINGERPRINT_MISSING")
    if len(fingerprints) != 1:
        raise WrongNudgeExperimentError("ENVIRONMENT_FINGERPRINT_MISMATCH")
    return str(next(iter(prefix_ids))), str(next(iter(fingerprints)))
