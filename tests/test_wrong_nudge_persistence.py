import json
from pathlib import Path

import pytest

from spotter.app_server import AppServerEvent, AppServerTransportError
from spotter.experiment import ArmClassification
from spotter.wrong_nudge_corpus import FramingCondition
from spotter.wrong_nudge_experiment import (
    DeliveryOutcome,
    WrongNudgeMechanicalResult,
)
from spotter.wrong_nudge_persistence import (
    PERSISTENCE_FOLLOW_UP_PROMPT,
    PersistenceDeliveryOutcome,
    WrongNudgePersistenceError,
    load_wrong_nudge_persistence_results,
    run_wrong_nudge_persistence_cohort,
)


def _sources(root: Path) -> tuple[WrongNudgeMechanicalResult, ...]:
    results: list[WrongNudgeMechanicalResult] = []
    for condition in FramingCondition:
        worktree = root / condition.value
        worktree.mkdir(parents=True)
        results.append(
            WrongNudgeMechanicalResult(
                experiment_id="experiment-1",
                condition=condition,
                wrong_nudge_id="wrong-1",
                wrong_nudge_manifest_sha256="nudge-sha",
                wrong_nudge_source_task="fixture/task",
                payload_version=1,
                source_session_id="source-session",
                source_step=7,
                prefix_id="prefix-sha",
                environment_fingerprint="environment-sha",
                fork_session_id=f"fork-{condition}",
                fork_manifest=f"/manifest-{condition}.json",
                worktree=str(worktree),
                turn_id=f"initial-{condition}",
                continuation_client_user_message_id=f"start-{condition}",
                steer_client_user_message_id=(
                    None if condition == FramingCondition.NEUTRAL_CONTROL else f"steer-{condition}"
                ),
                delivery_outcome=(
                    DeliveryOutcome.CONTROL_NO_STEER
                    if condition == FramingCondition.NEUTRAL_CONTROL
                    else DeliveryOutcome.RPC_ACCEPTED
                ),
                completion_observed=True,
                turn_status="completed",
                delivery_diagnostic=None,
                task_id="fixture/task",
                task_manifest_sha256="task-sha",
                fixture_sha256="fixture-sha",
                classification=ArmClassification.PASS,
                checks=(),
                scoring_diagnostic=None,
                started_at="2026-08-16T00:00:00+00:00",
                ended_at="2026-08-16T00:01:00+00:00",
            )
        )
    return tuple(results)


class FakeClient:
    def __init__(self) -> None:
        self.thread_id = ""
        self.turn_id = ""
        self.starts: list[tuple[str, str, str | None, str | None]] = []
        self.connected = False

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
        self.turn_id = f"follow-up-{thread_id}"
        self.starts.append((thread_id, text, cwd, client_user_message_id))
        return {"turn": {"id": self.turn_id}}

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


class CompletionFailureClient(FakeClient):
    async def next_event(self) -> AppServerEvent:
        raise AppServerTransportError("connection lost")


def test_runs_versioned_follow_up_for_each_arm_and_round_trips(
    tmp_path: Path,
) -> None:
    clients: list[FakeClient] = []

    def factory(endpoint: str, timeout: float) -> FakeClient:
        assert endpoint == "ws://app-server"
        assert timeout == 10
        client = FakeClient()
        clients.append(client)
        return client

    output, results = run_wrong_nudge_persistence_cohort(
        _sources(tmp_path / "worktrees"),
        "ws://app-server",
        output=tmp_path / "persistence.jsonl",
        client_factory=factory,
    )

    assert len(results) == len(FramingCondition)
    assert all(
        result.delivery_outcome == PersistenceDeliveryOutcome.START_ACCEPTED for result in results
    )
    assert all(result.completion_observed for result in results)
    assert all(result.source_result_fingerprint for result in results)
    assert all(client.starts[0][1] == PERSISTENCE_FOLLOW_UP_PROMPT for client in clients)
    assert all(
        client.starts[0][3] is not None and client.starts[0][3].startswith("spt-exp-persistence-")
        for client in clients
    )
    assert all(not client.connected for client in clients)
    assert load_wrong_nudge_persistence_results(output) == results

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert rows[0]["experiment_mode"] == "wrong-nudge-persistence"
    assert rows[-1]["complete"] is True
    assert len(rows) == len(FramingCondition) + 2


def test_rejects_incomplete_source_set_before_connecting_or_writing(tmp_path: Path) -> None:
    sources = _sources(tmp_path / "worktrees")[:-1]
    output = tmp_path / "must-not-exist.jsonl"

    def unexpected_factory(endpoint: str, timeout: float) -> FakeClient:
        raise AssertionError("invalid source evidence must not connect")

    with pytest.raises(WrongNudgePersistenceError, match="exactly one complete arm set"):
        run_wrong_nudge_persistence_cohort(
            sources,
            "ws://app-server",
            output=output,
            client_factory=unexpected_factory,
        )

    assert not output.exists()


def test_records_accepted_follow_up_when_completion_is_not_observed(tmp_path: Path) -> None:
    output, results = run_wrong_nudge_persistence_cohort(
        _sources(tmp_path / "worktrees"),
        "ws://app-server",
        output=tmp_path / "persistence.jsonl",
        client_factory=lambda endpoint, timeout: CompletionFailureClient(),
    )

    assert output.exists()
    assert all(
        result.delivery_outcome == PersistenceDeliveryOutcome.START_ACCEPTED for result in results
    )
    assert all(not result.completion_observed for result in results)
    assert all(result.diagnostic == "completion_transport_error" for result in results)


def test_loader_refuses_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "future.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema": "spotter.experiment_result",
                "schema_version": 3,
                "result_schema_version": 3,
                "wrong_nudge_persistence_schema_version": 999,
                "meta": True,
            }
        )
        + "\n"
    )

    with pytest.raises(WrongNudgePersistenceError, match="unsupported schema"):
        load_wrong_nudge_persistence_results(path)
