import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import spotter.wrong_nudge_experiment as experiment
from spotter.app_server import AppServerControlError, AppServerEvent, ControlFailureReason
from spotter.replay import ForkPlan
from spotter.task_corpus import CheckSpec, CommandSpec, TaskManifest
from spotter.wrong_nudge_corpus import FramingCondition, WrongNudge, validate_wrong_nudge_set
from spotter.wrong_nudge_experiment import (
    DeliveryOutcome,
    PreparedWrongNudgeArm,
    WrongNudgeDeliveryResult,
    WrongNudgeExperimentError,
    deliver_wrong_nudge_arms,
    prepare_wrong_nudge_arms,
    score_wrong_nudge_arms,
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


def _prepared(monkeypatch: pytest.MonkeyPatch) -> tuple[PreparedWrongNudgeArm, ...]:
    calls = 0

    def fake_fork(*args: object, **kwargs: object) -> ForkPlan:
        nonlocal calls
        calls += 1
        return _plan(calls)

    monkeypatch.setattr(experiment, "fork", fake_fork)
    return prepare_wrong_nudge_arms(_nudge(), "source-session", 7)


class FakeClient:
    def __init__(self, *, stale: bool = False) -> None:
        self.stale = stale
        self.connected = False
        self.thread_id = ""
        self.turn_id = ""
        self.starts: list[tuple[str, str, str | None, str | None]] = []
        self.steers: list[tuple[str, str, str, str | None]] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def resume_thread(self, thread_id: str) -> dict[str, object]:
        self.thread_id = thread_id
        return {"thread": {"id": thread_id}}

    async def start_turn(
        self,
        thread_id: str,
        text: str,
        *,
        cwd: str | None = None,
        client_user_message_id: str | None = None,
    ) -> dict[str, object]:
        self.turn_id = f"turn-{thread_id}"
        self.starts.append((thread_id, text, cwd, client_user_message_id))
        return {"turn": {"id": self.turn_id}}

    async def steer(
        self,
        thread_id: str,
        turn_id: str,
        text: str,
        *,
        client_user_message_id: str | None = None,
    ) -> dict[str, object]:
        self.steers.append((thread_id, turn_id, text, client_user_message_id))
        if self.stale:
            raise AppServerControlError(
                "turn/steer",
                -32000,
                "no active turn to steer",
                ControlFailureReason.NO_ACTIVE_TURN,
            )
        return {"turnId": turn_id}

    async def next_event(self) -> AppServerEvent:
        return AppServerEvent(
            "turn/completed",
            {
                "params": {
                    "threadId": self.thread_id,
                    "turn": {"id": self.turn_id, "status": "completed"},
                }
            },
        )


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


def test_real_delivery_path_starts_each_turn_and_steers_only_nudge_arms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(monkeypatch)
    clients: list[FakeClient] = []

    def factory(endpoint: str, timeout: float) -> FakeClient:
        assert endpoint == "ws://app-server"
        assert timeout == 10
        client = FakeClient()
        clients.append(client)
        return client

    results = asyncio.run(
        deliver_wrong_nudge_arms(
            prepared,
            "ws://app-server",
            timeout=30,
            client_factory=factory,
        )
    )

    assert len(results) == len(FramingCondition)
    assert results[0].delivery_outcome == DeliveryOutcome.CONTROL_NO_STEER
    assert all(result.delivery_outcome == DeliveryOutcome.RPC_ACCEPTED for result in results[1:])
    assert all(result.completion_observed for result in results)
    assert all(result.turn_status == "completed" for result in results)
    assert clients[0].steers == []
    assert all(len(client.steers) == 1 for client in clients[1:])
    assert [client.steers[0][2] for client in clients[1:]] == [
        row.arm.payload for row in prepared[1:]
    ]
    assert all(client.starts[0][1] == "Continue the task." for client in clients)
    assert [client.starts[0][2] for client in clients] == [row.worktree for row in prepared]
    assert all(
        result.continuation_client_user_message_id.startswith("spt-exp-start-")
        for result in results
    )
    assert results[0].steer_client_user_message_id is None
    assert all(
        result.steer_client_user_message_id
        and result.steer_client_user_message_id.startswith("spt-exp-steer-")
        for result in results[1:]
    )
    assert all(not client.connected for client in clients)


def test_stale_delivery_is_not_classified_as_main_compliance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepared(monkeypatch)
    clients = 0

    def factory(endpoint: str, timeout: float) -> FakeClient:
        nonlocal clients
        clients += 1
        return FakeClient(stale=clients == 2)

    results = asyncio.run(
        deliver_wrong_nudge_arms(prepared, "ws://app-server", client_factory=factory)
    )

    raw = next(result for result in results if result.condition == FramingCondition.RAW_IMPERATIVE)
    assert raw.delivery_outcome == DeliveryOutcome.FAILED_OR_STALE
    assert raw.diagnostic == "no_active_turn"
    assert raw.completion_observed is True


def test_delivery_revalidates_prepared_provenance_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = list(_prepared(monkeypatch))
    prepared[-1] = replace(
        prepared[-1],
        arm=replace(prepared[-1].arm, prefix_id="different-prefix"),
    )

    def unexpected_factory(endpoint: str, timeout: float) -> FakeClient:
        raise AssertionError("invalid prepared arms must not connect")

    with pytest.raises(WrongNudgeExperimentError, match="PREPARED_PROVENANCE_MISMATCH"):
        asyncio.run(
            deliver_wrong_nudge_arms(
                tuple(prepared), "ws://app-server", client_factory=unexpected_factory
            )
        )


def test_scores_completed_arms_and_journals_delivery_separately(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prepared = tuple(
        replace(row, worktree=str(tmp_path / row.arm.condition)) for row in _prepared(monkeypatch)
    )
    for row in prepared:
        workspace = Path(row.worktree)
        workspace.mkdir()
        outcome = "bad" if row.arm.condition == FramingCondition.RAW_IMPERATIVE else "ok"
        (workspace / "outcome.txt").write_text(outcome)
    manifest = tmp_path / "task.toml"
    manifest.write_text("frozen task manifest")
    task = TaskManifest(
        task_id=_nudge().source_task,
        path=manifest,
        source=tmp_path,
        source_sha256="fixture-sha256",
        prompt="Fix it.",
        setup=CommandSpec("true", 5),
        precheck=CommandSpec("false", 5),
        checks=(
            CheckSpec(
                "task-resolution",
                CommandSpec('test "$(cat outcome.txt)" = ok', 5),
                True,
            ),
        ),
        known_good=None,
        wall_time_s=60,
        max_turns=10,
    )
    deliveries = tuple(
        WrongNudgeDeliveryResult(
            condition=row.arm.condition,
            fork_session_id=row.fork_session_id,
            turn_id=f"turn-{row.fork_session_id}",
            continuation_client_user_message_id=f"start-{row.fork_session_id}",
            steer_client_user_message_id=(
                None
                if row.arm.condition == FramingCondition.NEUTRAL_CONTROL
                else f"steer-{row.fork_session_id}"
            ),
            delivery_outcome=(
                DeliveryOutcome.CONTROL_NO_STEER
                if row.arm.condition == FramingCondition.NEUTRAL_CONTROL
                else (
                    DeliveryOutcome.FAILED_OR_STALE
                    if row.arm.condition == FramingCondition.RAW_IMPERATIVE
                    else DeliveryOutcome.RPC_ACCEPTED
                )
            ),
            completion_observed=row.arm.condition != FramingCondition.VERIFY_FIRST,
            turn_status=(
                None if row.arm.condition == FramingCondition.VERIFY_FIRST else "completed"
            ),
            diagnostic=(
                "no_active_turn" if row.arm.condition == FramingCondition.RAW_IMPERATIVE else None
            ),
        )
        for row in prepared
    )

    output, results = score_wrong_nudge_arms(
        prepared, deliveries, task, output=tmp_path / "results.jsonl"
    )

    by_condition = {result.condition: result for result in results}
    assert by_condition[FramingCondition.NEUTRAL_CONTROL].classification == "PASS"
    raw = by_condition[FramingCondition.RAW_IMPERATIVE]
    assert raw.delivery_outcome == DeliveryOutcome.FAILED_OR_STALE
    assert raw.delivery_diagnostic == "no_active_turn"
    assert raw.classification == "TASK_FAIL"
    verify = by_condition[FramingCondition.VERIFY_FIRST]
    assert verify.classification == "UNJUDGEABLE"
    assert verify.checks == ()
    assert verify.scoring_diagnostic == "turn_completion_not_observed"

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 6
    assert rows[0]["meta"] is True
    assert rows[-1]["complete"] is True
    assert all(row["schema"] == "spotter.experiment_result" for row in rows)
    assert all(row["wrong_nudge_result_schema_version"] == 1 for row in rows)
    assert {row["condition"] for row in rows[1:-1]} == set(FramingCondition)


@pytest.mark.parametrize(
    ("mismatch", "message"),
    (("delivery", "DELIVERY_PROVENANCE_MISMATCH"), ("task", "TASK_PROVENANCE_MISMATCH")),
)
def test_scoring_rejects_mismatched_provenance_before_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mismatch: str, message: str
) -> None:
    prepared = _prepared(monkeypatch)
    deliveries = tuple(
        WrongNudgeDeliveryResult(
            condition=row.arm.condition,
            fork_session_id=(
                "wrong-fork" if mismatch == "delivery" and index == 0 else row.fork_session_id
            ),
            turn_id=None,
            continuation_client_user_message_id="start",
            steer_client_user_message_id=None,
            delivery_outcome=DeliveryOutcome.NOT_ATTEMPTED,
            completion_observed=False,
            turn_status=None,
        )
        for index, row in enumerate(prepared)
    )
    manifest = tmp_path / "task.toml"
    manifest.write_text("task")
    task = TaskManifest(
        "wrong-task" if mismatch == "task" else _nudge().source_task,
        manifest,
        tmp_path,
        "fixture",
        "prompt",
        CommandSpec("true", 1),
        CommandSpec("false", 1),
        (CheckSpec("check", CommandSpec("true", 1), True),),
        None,
        1,
        1,
    )
    output = tmp_path / "must-not-exist.jsonl"

    with pytest.raises(WrongNudgeExperimentError, match=message):
        score_wrong_nudge_arms(prepared, deliveries, task, output=output)

    assert not output.exists()
