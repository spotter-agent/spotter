"""Asynchronous shadow execution for durable signal-driven reviewer jobs."""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from spotter.budget import Spend, charge, reserve, settle
from spotter.config import ReviewerConfig
from spotter.review_scheduler import ReviewerJob
from spotter.reviewer import ReviewerDecision, review_bounded_input
from spotter.reviewer_input import ReviewerInput
from spotter.trace import TraceEvent, TraceProvenance

RecordEvent = Callable[[TraceEvent], object]
FreshnessCheck = Callable[[ReviewerJob], bool]
ReviewFunction = Callable[[ReviewerInput, str], Awaitable[tuple[ReviewerDecision, int]]]
DeliveryFunction = Callable[[ReviewerJob, ReviewerDecision], Awaitable[None]]


class ReviewExecutor:
    """Run opted-in jobs off the event loop and keep every paid outcome durable."""

    def __init__(
        self,
        config: ReviewerConfig,
        record: RecordEvent,
        is_fresh: FreshnessCheck,
        *,
        config_generation: str = "unversioned",
        reviewer: ReviewFunction = review_bounded_input,
        deliver: DeliveryFunction | None = None,
    ) -> None:
        self.config = config
        self.config_generation = config_generation
        self.record = record
        self.is_fresh = is_fresh
        self.reviewer = reviewer
        self.deliver = deliver
        self._queue: asyncio.PriorityQueue[
            tuple[float, int, int, ReviewerJob, ReviewerConfig, str]
        ] = asyncio.PriorityQueue()
        self._runner: asyncio.Task[None] | None = None
        self._sequence = 0
        self._submitted: set[str] = set()

    def submit(self, job: ReviewerJob) -> None:
        config = self.config
        config_generation = self.config_generation
        if not config.on_signals or job.job_id in self._submitted:
            return
        job = replace(
            job,
            config_generation=config_generation,
            reviewer_model=config.model,
        )
        self._submitted.add(job.job_id)
        self._sequence += 1
        self.record(
            self._event(
                job,
                "review_job_config_pinned",
                {
                    "review_job_id": job.job_id,
                    "model": config.model,
                },
                config_generation,
            )
        )
        self._queue.put_nowait(
            (
                -_priority(job),
                self._sequence,
                time.perf_counter_ns(),
                job,
                config,
                config_generation,
            )
        )
        if self._runner is None or self._runner.done():
            self._runner = asyncio.create_task(self._work(), name="review-queue")

    def update_config(self, config: ReviewerConfig, config_generation: str) -> None:
        """Use a new immutable config only for jobs submitted after this call."""

        self.config = config
        self.config_generation = config_generation

    async def drain(self) -> None:
        await self._queue.join()

    async def close(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
            self._runner = None

    async def _work(self) -> None:
        while True:
            _, _, queued_ns, job, config, config_generation = await self._queue.get()
            try:
                await self._run(job, queued_ns, config, config_generation)
            finally:
                self._queue.task_done()

    async def _run(
        self,
        job: ReviewerJob,
        queued_ns: int,
        config: ReviewerConfig,
        config_generation: str,
    ) -> None:
        if not self.is_fresh(job):
            return
        session = job.thread_id.value
        try:
            token, refusal = reserve(
                session,
                config.max_per_session,
                config.max_per_day,
            )
        except Exception as error:  # noqa: BLE001 — spending failure must stay visible
            self.record(
                self._event(
                    job,
                    "reviewer_error",
                    {
                        "review_job_id": job.job_id,
                        "error": f"review budget unavailable: {error}"[:300],
                        "priority": _priority(job),
                    },
                    config_generation,
                )
            )
            return
        if token is None:
            kind = "reviewer_error" if "unreadable" in refusal else "reviewer_capped"
            key = "error" if kind == "reviewer_error" else "reason"
            self.record(
                self._event(
                    job,
                    kind,
                    {
                        key: refusal,
                        "review_job_id": job.job_id,
                        "priority": _priority(job),
                    },
                    config_generation,
                )
            )
            return
        started_at = time.time()
        queue_ms = _elapsed_ms(queued_ns)
        self.record(
            self._event(
                job,
                "review_inference_started",
                {
                    "review_job_id": job.job_id,
                    "queue_ms": queue_ms,
                    "priority": _priority(job),
                },
                occurred_at=started_at,
                config_generation=config_generation,
            )
        )
        inference_started_ns = time.perf_counter_ns()
        try:
            decision, tokens = await self.reviewer(job.reviewer_input, config.model)
        except asyncio.CancelledError:
            inference_ms = _elapsed_ms(inference_started_ns)
            spend = settle(session, token, 0) or charge(session, 0)
            self.record(
                self._event(
                    job,
                    "reviewer_error",
                    {
                        "review_job_id": job.job_id,
                        "error": "review cancelled during daemon shutdown",
                        "spend": _spend(spend),
                        "timing": {"queue_ms": queue_ms, "inference_ms": inference_ms},
                    },
                    config_generation,
                )
            )
            raise
        except Exception as error:  # noqa: BLE001 — paid failure must remain observable
            inference_ms = _elapsed_ms(inference_started_ns)
            spend = settle(session, token, 0) or charge(session, 0)
            self.record(
                self._event(
                    job,
                    "reviewer_error",
                    {
                        "review_job_id": job.job_id,
                        "error": str(error)[:300],
                        "spend": _spend(spend),
                        "timing": {"queue_ms": queue_ms, "inference_ms": inference_ms},
                    },
                    config_generation,
                )
            )
            return
        inference_ms = _elapsed_ms(inference_started_ns)
        spend = settle(session, token, tokens) or charge(session, tokens)
        stale = not self.is_fresh(job)
        self.record(
            self._event(
                job,
                "reviewer_decision",
                {
                    "review_job_id": job.job_id,
                    "decision": decision.decision,
                    "failure_class": decision.failure_class,
                    "reason": decision.reason,
                    "hypothesis": decision.hypothesis,
                    "confidence": decision.confidence,
                    "model": config.model,
                    "reviewed_state_version": job.state_version,
                    "target_turn_id": job.target_turn_id.value,
                    "target_connection_epoch": job.target_connection_epoch,
                    "stale": stale,
                    "shadow": not config.deliver_on_signals,
                    "inputs": job.reviewer_input.coverage(),
                    "timing": {
                        "queue_ms": queue_ms,
                        "inference_ms": inference_ms,
                    },
                    "spend": _spend(spend),
                },
                config_generation,
            )
        )
        if stale or not config.deliver_on_signals or decision.decision not in {"verify", "nudge"}:
            return
        if self.deliver is None:
            self.record(
                self._event(
                    job,
                    "intervention_delivery_error",
                    {
                        "review_job_id": job.job_id,
                        "error": "live delivery is enabled but no delivery controller is available",
                    },
                    config_generation,
                )
            )
            return
        try:
            await self.deliver(job, decision)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — the control path also journals wire outcomes
            self.record(
                self._event(
                    job,
                    "intervention_delivery_error",
                    {
                        "review_job_id": job.job_id,
                        "error": str(error)[:300],
                    },
                    config_generation,
                )
            )

    @staticmethod
    def _event(
        job: ReviewerJob,
        kind: str,
        payload: dict[str, object],
        config_generation: str,
        *,
        occurred_at: float | None = None,
    ) -> TraceEvent:
        payload = {
            **payload,
            "review_trigger": "signal",
            "config_generation": config_generation,
        }
        return TraceEvent(
            kind,
            payload,
            event_id=f"spotter:review-job:{job.job_id}:{kind}",
            occurred_at=occurred_at if occurred_at is not None else time.time(),
            identity=job.snapshot.identity,
            provenance=TraceProvenance("spotterd", "review_executor"),
            connection_epoch=job.target_connection_epoch,
            config_generation=config_generation,
        )


def _spend(spend: Spend) -> dict[str, int]:
    return {
        "session_reviews": spend.session,
        "session_tokens": spend.tokens,
    }


def _elapsed_ms(started_ns: int) -> float:
    return max(0.0, (time.perf_counter_ns() - started_ns) / 1_000_000)


def _priority(job: ReviewerJob) -> float:
    value = job.reviewer_input.severity_hint
    if isinstance(value, int | float) and not isinstance(value, bool):
        priority = float(value)
        return priority if math.isfinite(priority) else 0.0
    return 0.0
