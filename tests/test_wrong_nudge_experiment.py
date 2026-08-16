from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import spotter.wrong_nudge_experiment as experiment
from spotter.replay import ForkPlan
from spotter.wrong_nudge_corpus import FramingCondition, WrongNudge, validate_wrong_nudge_set
from spotter.wrong_nudge_experiment import (
    WrongNudgeExperimentError,
    prepare_wrong_nudge_arms,
)


def _nudge() -> WrongNudge:
    root = Path(__file__).parents[1] / "corpus"
    return validate_wrong_nudge_set(root / "wrong-nudges-v1.toml").nudges[0]


def _plan(index: int, **overrides: Any) -> ForkPlan:
    plan = ForkPlan(
        session_id=f"fork-{index}",
        branch_step=7,
        worktree=f"/worktree/{index}",
        rollout=f"/rollout/{index}.jsonl",
        command="unused",
        manifest=f"/manifest/{index}.json",
        prefix_id="prefix-sha256",
        environment_fingerprint="environment-sha256",
        source_environment_preflight="MATCHED",
    )
    return replace(plan, **overrides)


def test_prepares_four_independent_equivalent_prefix_forks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_fork(session_id: str, step: int, **kwargs: object) -> ForkPlan:
        calls.append({"session_id": session_id, "step": step, **kwargs})
        return _plan(len(calls))

    monkeypatch.setattr(experiment, "fork", fake_fork)

    prepared = prepare_wrong_nudge_arms(
        _nudge(),
        "source-session",
        7,
        codex_home=Path("/codex"),
        environment_resources=(".env",),
        environment_variables=("API_MODE",),
        environment_venv_or_cache=(".venv",),
    )

    assert len(calls) == 4
    assert {row.arm.condition for row in prepared} == set(FramingCondition)
    assert len({row.fork_session_id for row in prepared}) == 4
    assert len({row.worktree for row in prepared}) == 4
    assert all(row.fork_manifest for row in prepared)
    assert all(call["codex_home"] == Path("/codex") for call in calls)
    assert all(call["environment_resources"] == (".env",) for call in calls)


@pytest.mark.parametrize(
    ("changed", "message"),
    (
        ({"prefix_id": "different"}, "PREFIX_MISMATCH"),
        ({"environment_fingerprint": "different"}, "ENVIRONMENT_FINGERPRINT_MISMATCH"),
        ({"source_environment_preflight": "SOURCE_ENVIRONMENT_MISMATCH"}, "SOURCE_ENVIRONMENT"),
        ({"manifest": None}, "FORK_MANIFEST_UNAVAILABLE"),
    ),
)
def test_preflight_refuses_any_non_equivalent_fork_before_delivery(
    monkeypatch: pytest.MonkeyPatch, changed: dict[str, object], message: str
) -> None:
    calls = 0

    def fake_fork(*args: object, **kwargs: object) -> ForkPlan:
        nonlocal calls
        calls += 1
        return _plan(calls, **(changed if calls == 4 else {}))

    monkeypatch.setattr(experiment, "fork", fake_fork)

    with pytest.raises(WrongNudgeExperimentError, match=message):
        prepare_wrong_nudge_arms(_nudge(), "source-session", 7)

    assert calls == 4


def test_preflight_refuses_reused_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_fork(*args: object, **kwargs: object) -> ForkPlan:
        nonlocal calls
        calls += 1
        return _plan(calls, worktree="/shared" if calls > 2 else f"/worktree/{calls}")

    monkeypatch.setattr(experiment, "fork", fake_fork)

    with pytest.raises(WrongNudgeExperimentError, match="SHARED_ARM_WORKTREE"):
        prepare_wrong_nudge_arms(_nudge(), "source-session", 7)


@pytest.mark.parametrize(("session", "step"), (("", 7), ("source", -1), ("source", False)))
def test_invalid_source_is_rejected_before_forking(
    monkeypatch: pytest.MonkeyPatch, session: str, step: int
) -> None:
    def unexpected_fork(*args: object, **kwargs: object) -> ForkPlan:
        raise AssertionError("invalid source must not create forks")

    monkeypatch.setattr(experiment, "fork", unexpected_fork)

    with pytest.raises(WrongNudgeExperimentError):
        prepare_wrong_nudge_arms(_nudge(), session, step)
