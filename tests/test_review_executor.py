import asyncio
from pathlib import Path

import pytest

from spotter.config import ReviewerConfig
from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.review_executor import ReviewExecutor
from spotter.reviewer import ReviewerDecision
from spotter.runtime_connection import AppServerRecoveryLoop
from spotter.thread_state import ThreadStateStore
from spotter.trace import TraceEvent


def _event(event_id: str, kind: str, payload: dict[str, object]) -> TraceEvent:
    return TraceEvent(
        kind,
        payload,
        event_id=event_id,
        occurred_at=float(len(event_id)),
        identity=RuntimeIdentity(
            ThreadId("thread-1"),
            TurnId("turn-1"),
            None,
            IdentityProvenance("codex", "external-thread", "turn-1"),
        ),
        connection_epoch=1,
    )


def _trigger(runtime: AppServerRecoveryLoop) -> None:
    runtime._record(_event("turn-start", "turn_started", {}))
    runtime._record(
        _event(
            "failure-1",
            "tool_result",
            {"status": "failed", "server": "fixture", "tool": "lookup"},
        )
    )
    runtime._record(
        _event(
            "failure-2",
            "tool_result",
            {"status": "failed", "server": "fixture", "tool": "lookup"},
        )
    )


def test_signal_review_runs_asynchronously_and_finishes_durably(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))

    async def scenario() -> None:
        runtime = AppServerRecoveryLoop("ws://unused", tmp_path / "sessions", ThreadStateStore())
        entered = asyncio.Event()
        release = asyncio.Event()

        async def reviewer(input_: object, model: str) -> tuple[ReviewerDecision, int]:
            assert model == "review-model"
            entered.set()
            await release.wait()
            return ReviewerDecision("verify", "tool_failure_loop", "retrying", 0.8), 7

        executor = ReviewExecutor(
            ReviewerConfig(model="review-model", on_signals=True),
            runtime.record_review_event,
            runtime.review_job_is_fresh,
            reviewer=reviewer,
        )
        runtime.set_review_job_callback(executor.submit)

        _trigger(runtime)
        await entered.wait()
        assert not any(
            record.event.kind == "reviewer_decision" for record in runtime.ingestor.records()
        )
        release.set()
        await executor.drain()

        records = [record.event for record in runtime.ingestor.records()]
        decision = next(event for event in records if event.kind == "reviewer_decision")
        assert decision.payload["decision"] == "verify"
        assert decision.payload["stale"] is False
        assert decision.payload["shadow"] is True
        assert decision.payload["spend"] == {"session_reviews": 1, "session_tokens": 7}
        assert runtime.review_scheduler.pending() == ()

    asyncio.run(scenario())


def test_running_review_is_recorded_stale_after_target_turn_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))

    async def scenario() -> None:
        runtime = AppServerRecoveryLoop("ws://unused", tmp_path / "sessions", ThreadStateStore())
        entered = asyncio.Event()
        release = asyncio.Event()

        async def reviewer(input_: object, model: str) -> tuple[ReviewerDecision, int]:
            entered.set()
            await release.wait()
            return ReviewerDecision("nudge", "tool_failure_loop", "retrying", 0.9), 5

        executor = ReviewExecutor(
            ReviewerConfig(on_signals=True),
            runtime.record_review_event,
            runtime.review_job_is_fresh,
            reviewer=reviewer,
        )
        runtime.set_review_job_callback(executor.submit)
        _trigger(runtime)
        await entered.wait()

        runtime._record(_event("turn-done", "turn_completed", {"status": "completed"}))
        release.set()
        await executor.drain()

        records = [record.event for record in runtime.ingestor.records()]
        assert any(event.kind == "review_job_stale" for event in records)
        decision = next(event for event in records if event.kind == "reviewer_decision")
        assert decision.payload["stale"] is True
        assert decision.payload["decision"] == "nudge"

    asyncio.run(scenario())


def test_failed_review_records_monotonic_queue_and_inference_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))

    async def scenario() -> None:
        runtime = AppServerRecoveryLoop("ws://unused", tmp_path / "sessions", ThreadStateStore())

        async def reviewer(input_: object, model: str) -> tuple[ReviewerDecision, int]:
            raise RuntimeError("model unavailable")

        executor = ReviewExecutor(
            ReviewerConfig(on_signals=True),
            runtime.record_review_event,
            runtime.review_job_is_fresh,
            reviewer=reviewer,
        )
        runtime.set_review_job_callback(executor.submit)

        _trigger(runtime)
        await executor.drain()

        error = next(
            record.event
            for record in runtime.ingestor.records()
            if record.event.kind == "reviewer_error"
        )
        timing = error.payload["timing"]
        assert isinstance(timing, dict)
        assert float(timing["queue_ms"]) >= 0
        assert float(timing["inference_ms"]) >= 0
        assert runtime.review_scheduler.pending() == ()

    asyncio.run(scenario())
