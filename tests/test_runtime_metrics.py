from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.runtime_metrics import measure_runtime_costs, render_runtime_costs
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent, TraceProvenance


def _record(step: int, event: TraceEvent, *, at: float | None = 1.0) -> StepRecord:
    return StepRecord(step, event, None, at)


def test_runtime_costs_keep_surfaces_domains_and_coverage_separate() -> None:
    hook = [
        _record(
            0,
            TraceEvent(
                "tool_proposal",
                {"tool_use_id": "call-1", "turn_id": "legacy-turn"},
                provenance=TraceProvenance("codex_hook", "PreToolUse"),
            ),
        ),
        _record(
            1,
            TraceEvent(
                "tool_result",
                {
                    "tool_use_id": "call-1",
                    "turn_id": "legacy-turn",
                    "tool_response": {"exit_code": 1},
                },
                provenance=TraceProvenance("codex_hook", "PostToolUse"),
            ),
        ),
        _record(
            2,
            TraceEvent(
                "gate_ipc",
                {
                    "hook_ms": 3.0,
                    "ipc_ms": 2.0,
                    "daemon_evaluation_ms": 1.0,
                    "runtime_sample": {
                        "runtime_id": "daemon-1",
                        "sample_seq": 1,
                        "cpu_seconds": 0.25,
                        "peak_rss_bytes": 1024,
                    },
                },
            ),
        ),
        _record(
            3,
            TraceEvent("review_job_queued", {"review_job_id": "review-1"}),
        ),
        _record(
            4,
            TraceEvent(
                "review_inference_started",
                {"review_job_id": "review-1", "queue_ms": 4.0},
            ),
        ),
        _record(
            5,
            TraceEvent(
                "reviewer_decision",
                {
                    "spend": {"session_tokens": 25},
                    "timing": {"queue_ms": 4.0, "inference_ms": 8.0},
                },
            ),
        ),
    ]
    identity = RuntimeIdentity(
        ThreadId("thread-1"),
        TurnId("turn-1"),
        None,
        IdentityProvenance("codex", "external-thread", "external-turn"),
    )

    def app(
        kind: str,
        payload: dict[str, object],
        operation: str | None = None,
        *,
        occurred_at: float = 2.0,
        arrival_seq: int,
    ) -> TraceEvent:
        return TraceEvent(
            kind,
            payload,
            occurred_at=occurred_at,
            identity=identity,
            operation_id=operation,
            provenance=TraceProvenance("codex_app_server", "synthetic"),
            connection_epoch=1,
            arrival_seq=arrival_seq,
        )

    app_server = [
        _record(0, app("turn_started", {}, occurred_at=1.0, arrival_seq=1)),
        _record(1, app("command_started", {}, "command-1", arrival_seq=2)),
        _record(
            2,
            app(
                "command_result",
                {"status": "completed", "exitCode": 0, "durationMs": 10},
                "command-1",
                arrival_seq=3,
            ),
        ),
        _record(
            3,
            app("token_usage", {"total": {"totalTokens": 14}}, arrival_seq=4),
        ),
        _record(
            4,
            app(
                "turn_completed",
                {"status": "completed"},
                occurred_at=3.0,
                arrival_seq=5,
            ),
        ),
    ]

    report = measure_runtime_costs([(hook, 100), (app_server, 200)])

    assert report.surfaces["hook"].actions == 1
    assert report.surfaces["hook"].action_observations == 2
    assert report.surfaces["hook"].classified_outcomes == 1
    assert report.surfaces["hook"].failed_outcomes == 1
    assert report.surfaces["app_server"].actions == 1
    assert report.surfaces["app_server"].action_observations == 2
    assert report.surfaces["app_server"].outcomes == 1
    assert report.surfaces["app_server"].classified_outcomes == 1
    assert report.completed_turns == report.token_turns == 1
    assert report.cumulative_main_tokens == 14
    assert report.reviewer_calls == 1
    assert report.reviewer_tokens == 25
    assert report.reviewer_jobs_queued == report.reviewer_jobs_started == 1
    assert report.reviewer_queue_ms == (4.0,)
    assert report.reviewer_inference_ms == (8.0,)
    assert report.turn_wall_ms == (2000.0,)
    assert report.gate_calls == 1
    assert report.daemon_resource_samples == 1
    assert report.daemon_cpu_seconds == 0.25
    assert report.daemon_peak_rss_bytes == 1024
    assert report.tool_duration_ms == (10.0,)
    assert report.source_timestamps == 5
    assert report.receipt_timestamps == report.events == 11
    assert report.arrival_ordered_events == report.arrival_order_eligible_events == 5
    assert report.journal_bytes == 300

    rendered = render_runtime_costs(report)
    assert "turn coverage 1/1" in rendered
    assert "hook: actions=1 (from 2 observations), outcomes=1 (from 1 observation)" in rendered
    assert (
        "app_server: actions=1 (from 2 observations), outcomes=1 (from 1 observation)" in rendered
    )
    assert "jobs=1/1/1 decided/started/queued" in rendered
    assert "cpu=0.250s, peak_rss=1024 bytes; samples=1/1 gate calls" in rendered
    assert "arrival_order=5/5" in rendered
    assert "turn_wall(source)=avg=2000.00ms max=2000.00ms (1/1)" in rendered


def test_unavailable_runtime_metrics_render_unknown_not_zero() -> None:
    report = measure_runtime_costs([([_record(0, TraceEvent("session_start"), at=None)], 10)])

    rendered = render_runtime_costs(report)
    assert "Main tokens: unknown" in rendered
    assert "hook=unknown (0/0)" in rendered
    assert "receipt_wall=0/1" in rendered


def test_uncorrelated_action_observations_do_not_invent_semantic_identity() -> None:
    records = [
        _record(0, TraceEvent("tool_proposal")),
        _record(1, TraceEvent("tool_result", {"tool_response": {"exit_code": 1}})),
    ]

    report = measure_runtime_costs([(records, 10)])
    hook = report.surfaces["hook"]
    assert hook.actions == hook.outcomes == hook.classified_outcomes == 0
    assert hook.action_observations == 2
    assert hook.outcome_observations == 1


def test_turn_duration_never_crosses_connection_epochs() -> None:
    identity = RuntimeIdentity(
        ThreadId("thread-1"),
        TurnId("turn-1"),
        None,
        IdentityProvenance("codex", "external-thread", "external-turn"),
    )
    records = [
        _record(
            0,
            TraceEvent("turn_started", occurred_at=10.0, identity=identity, connection_epoch=1),
        ),
        _record(
            1,
            TraceEvent("turn_completed", occurred_at=20.0, identity=identity, connection_epoch=2),
        ),
    ]

    report = measure_runtime_costs([(records, 10)])

    assert report.completed_turns == 1
    assert report.turn_wall_ms == ()
    rendered = render_runtime_costs(report)
    assert "arrival_order=0/2" in rendered
    assert "turn_wall(source)=unknown (0/1)" in rendered
