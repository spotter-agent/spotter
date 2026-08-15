import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from spotter.config import ReviewerConfig
from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.review_executor import ReviewExecutor
from spotter.review_scheduler import ReviewerJob
from spotter.reviewer import ReviewerDecision
from spotter.reviewer_input import ReviewerInput
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
        assert decision.payload["review_trigger"] == "signal"
        assert decision.payload["stale"] is False
        assert decision.payload["shadow"] is True
        assert decision.payload["spend"] == {"session_reviews": 1, "session_tokens": 7}
        assert runtime.review_scheduler.pending() == ()

    asyncio.run(scenario())


def test_live_opt_in_delivers_a_fresh_intervention_decision(tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = AppServerRecoveryLoop("ws://unused", tmp_path / "sessions", ThreadStateStore())
        delivered: list[tuple[ReviewerJob, ReviewerDecision]] = []

        async def reviewer(input_: object, model: str) -> tuple[ReviewerDecision, int]:
            return ReviewerDecision("nudge", "tool_failure_loop", "retrying blindly", 0.9), 2

        async def deliver(job: ReviewerJob, decision: ReviewerDecision) -> None:
            delivered.append((job, decision))

        executor = ReviewExecutor(
            ReviewerConfig(on_signals=True, deliver_on_signals=True),
            runtime.record_review_event,
            runtime.review_job_is_fresh,
            reviewer=reviewer,
            deliver=deliver,
        )
        runtime.set_review_job_callback(executor.submit)

        _trigger(runtime)
        await executor.drain()

        assert len(delivered) == 1
        assert delivered[0][1].decision == "nudge"
        decision = next(
            record.event
            for record in runtime.ingestor.records()
            if record.event.kind == "reviewer_decision"
        )
        assert decision.payload["shadow"] is False

    asyncio.run(scenario())


def test_live_advisory_keeps_current_turn_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        runtime = AppServerRecoveryLoop("ws://unused", tmp_path / "sessions", ThreadStateStore())
        jobs: list[ReviewerJob] = []
        sent: list[tuple[str, str, str | None]] = []
        runtime.set_review_job_callback(jobs.append)
        _trigger(runtime)

        async def steer(
            target: object,
            text: str,
            *,
            control_id: str | None = None,
            review_job_id: str | None = None,
        ) -> dict[str, object]:
            sent.append((text, control_id or "", review_job_id))
            return {}

        monkeypatch.setattr(runtime, "steer", steer)
        decision = ReviewerDecision("verify", "tool_failure_loop", "check the failures", 0.8)

        await runtime.deliver_review_decision(jobs[0], decision)

        text, control_id, review_job_id = sent[0]
        assert text.startswith(f"[Spotter / VERIFY / {control_id}]")
        assert "not a new user requirement" in text
        assert "continue the original user task" in text
        assert "VERIFY: Check this assumption with evidence" in text
        assert "check the failures" in text
        assert control_id.startswith("spt-")
        assert len(control_id) == 16
        assert review_job_id == jobs[0].job_id

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("terminal_kind", "terminal_payload", "stale_reason"),
    [
        ("turn_completed", {"status": "completed"}, "target_changed"),
        (
            "agent_message",
            {"text": "Done", "phase": "final_answer", "lifecycle": "completed"},
            "terminal_answer_settled",
        ),
    ],
)
def test_running_review_is_recorded_stale_after_target_settles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_kind: str,
    terminal_payload: dict[str, object],
    stale_reason: str,
) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))

    async def scenario() -> None:
        runtime = AppServerRecoveryLoop("ws://unused", tmp_path / "sessions", ThreadStateStore())
        entered = asyncio.Event()
        release = asyncio.Event()
        delivered: list[ReviewerDecision] = []

        async def reviewer(input_: object, model: str) -> tuple[ReviewerDecision, int]:
            entered.set()
            await release.wait()
            return ReviewerDecision("nudge", "tool_failure_loop", "retrying", 0.9), 5

        async def deliver(job: ReviewerJob, decision: ReviewerDecision) -> None:
            delivered.append(decision)

        executor = ReviewExecutor(
            ReviewerConfig(on_signals=True, deliver_on_signals=True),
            runtime.record_review_event,
            runtime.review_job_is_fresh,
            reviewer=reviewer,
            deliver=deliver,
        )
        runtime.set_review_job_callback(executor.submit)
        _trigger(runtime)
        await entered.wait()

        runtime._record(_event("turn-done", terminal_kind, terminal_payload))
        release.set()
        await executor.drain()

        records = [record.event for record in runtime.ingestor.records()]
        stale = next(event for event in records if event.kind == "review_job_stale")
        assert stale.payload["reason"] == stale_reason
        decision = next(event for event in records if event.kind == "reviewer_decision")
        assert decision.payload["stale"] is True
        assert decision.payload["decision"] == "nudge"
        assert delivered == []

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


def test_pending_reviews_use_severity_before_limited_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "home"))

    async def scenario() -> None:
        runtime = AppServerRecoveryLoop("ws://unused", tmp_path / "sessions", ThreadStateStore())
        captured: list[ReviewerJob] = []
        runtime.set_review_job_callback(captured.append)
        _trigger(runtime)
        base = captured[0]

        def queued_job(name: str, severity: int) -> ReviewerJob:
            return replace(
                base,
                job_id=name,
                signal_id=name,
                candidate_event_id=f"candidate-{name}",
                reviewer_input=replace(
                    base.reviewer_input,
                    signal_id=name,
                    severity_hint=severity,
                ),
                signal_ids=(name,),
                candidate_event_ids=(f"candidate-{name}",),
            )

        low = queued_job("low", 1)
        medium = queued_job("medium", 5)
        high = queued_job("high", 9)
        entered = asyncio.Event()
        release = asyncio.Event()
        order: list[str] = []
        recorded: list[TraceEvent] = []

        async def reviewer(input_: ReviewerInput, model: str) -> tuple[ReviewerDecision, int]:
            signal_id = input_.signal_id
            order.append(signal_id)
            if signal_id == "low":
                entered.set()
                await release.wait()
            return ReviewerDecision("continue", "none", "prioritized", 0.8), 1

        executor = ReviewExecutor(
            ReviewerConfig(on_signals=True, max_per_session=2, max_per_day=2),
            recorded.append,
            lambda job: True,
            reviewer=reviewer,
        )
        executor.submit(low)
        await entered.wait()
        executor.submit(medium)
        executor.submit(high)
        release.set()
        await executor.drain()
        await executor.close()

        assert order == ["low", "high"]
        started = [event for event in recorded if event.kind == "review_inference_started"]
        assert [event.payload["review_job_id"] for event in started] == ["low", "high"]
        assert [event.payload["priority"] for event in started] == [1.0, 9.0]
        capped = [event for event in recorded if event.kind == "reviewer_capped"]
        assert len(capped) == 1
        assert capped[0].payload["review_job_id"] == "medium"
        assert capped[0].payload["priority"] == 5.0

    asyncio.run(scenario())
