"""Asynchronous shadow execution for durable signal-driven reviewer jobs."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from spotter.budget import Spend, charge, reserve, settle
from spotter.config import ReviewerConfig
from spotter.review_scheduler import ReviewerJob
from spotter.reviewer import ReviewerDecision, review_bounded_input
from spotter.reviewer_input import ReviewerInput
from spotter.trace import TraceEvent, TraceProvenance

RecordEvent = Callable[[TraceEvent], object]
FreshnessCheck = Callable[[ReviewerJob], bool]
ReviewFunction = Callable[[ReviewerInput, str], Awaitable[tuple[ReviewerDecision, int]]]


class ReviewExecutor:
    """Run opted-in jobs off the event loop and keep every paid outcome durable."""

    def __init__(
        self,
        config: ReviewerConfig,
        record: RecordEvent,
        is_fresh: FreshnessCheck,
        *,
        reviewer: ReviewFunction = review_bounded_input,
    ) -> None:
        self.config = config
        self.record = record
        self.is_fresh = is_fresh
        self.reviewer = reviewer
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._submitted: set[str] = set()

    def submit(self, job: ReviewerJob) -> None:
        if not self.config.on_signals or job.job_id in self._submitted:
            return
        self._submitted.add(job.job_id)
        task = asyncio.create_task(self._run(job, time.time()), name=f"review-{job.job_id[:12]}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks))

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _run(self, job: ReviewerJob, queued_at: float) -> None:
        async with self._lock:
            if not self.is_fresh(job):
                return
            session = job.thread_id.value
            try:
                token, refusal = reserve(
                    session,
                    self.config.max_per_session,
                    self.config.max_per_day,
                )
            except Exception as error:  # noqa: BLE001 — spending failure must stay visible
                self.record(
                    self._event(
                        job,
                        "reviewer_error",
                        {
                            "review_job_id": job.job_id,
                            "error": f"review budget unavailable: {error}"[:300],
                        },
                    )
                )
                return
            if token is None:
                kind = "reviewer_error" if "unreadable" in refusal else "reviewer_capped"
                key = "error" if kind == "reviewer_error" else "reason"
                self.record(self._event(job, kind, {key: refusal, "review_job_id": job.job_id}))
                return
            started_at = time.time()
            queue_ms = max(0.0, (started_at - queued_at) * 1000)
            self.record(
                self._event(
                    job,
                    "review_inference_started",
                    {"review_job_id": job.job_id, "queue_ms": queue_ms},
                    occurred_at=started_at,
                )
            )
            try:
                decision, tokens = await self.reviewer(job.reviewer_input, self.config.model)
            except asyncio.CancelledError:
                spend = settle(session, token, 0) or charge(session, 0)
                self.record(
                    self._event(
                        job,
                        "reviewer_error",
                        {
                            "review_job_id": job.job_id,
                            "error": "review cancelled during daemon shutdown",
                            "spend": _spend(spend),
                        },
                    )
                )
                raise
            except Exception as error:  # noqa: BLE001 — paid failure must remain observable
                spend = settle(session, token, 0) or charge(session, 0)
                self.record(
                    self._event(
                        job,
                        "reviewer_error",
                        {
                            "review_job_id": job.job_id,
                            "error": str(error)[:300],
                            "spend": _spend(spend),
                        },
                    )
                )
                return
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
                        "model": self.config.model,
                        "reviewed_state_version": job.state_version,
                        "target_turn_id": job.target_turn_id.value,
                        "target_connection_epoch": job.target_connection_epoch,
                        "stale": stale,
                        "shadow": True,
                        "inputs": job.reviewer_input.coverage(),
                        "timing": {
                            "queue_ms": queue_ms,
                            "inference_ms": decision.inference_ms,
                        },
                        "spend": _spend(spend),
                    },
                )
            )

    @staticmethod
    def _event(
        job: ReviewerJob,
        kind: str,
        payload: dict[str, object],
        *,
        occurred_at: float | None = None,
    ) -> TraceEvent:
        return TraceEvent(
            kind,
            payload,
            event_id=f"spotter:review-job:{job.job_id}:{kind}",
            occurred_at=occurred_at if occurred_at is not None else time.time(),
            identity=job.snapshot.identity,
            provenance=TraceProvenance("spotterd", "review_executor"),
            connection_epoch=job.target_connection_epoch,
        )


def _spend(spend: Spend) -> dict[str, int]:
    return {
        "session_reviews": spend.session,
        "session_tokens": spend.tokens,
    }
