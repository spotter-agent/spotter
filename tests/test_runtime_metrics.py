import json
from pathlib import Path

import pytest

from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.runtime_metrics import (
    CoveredCount,
    ObjectiveOutcomeError,
    measure_objective_outcomes,
    measure_runtime_costs,
    render_objective_outcomes,
    render_runtime_costs,
)
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent, TraceProvenance


def _record(step: int, event: TraceEvent, *, at: float | None = 1.0) -> StepRecord:
    return StepRecord(step, event, None, at)


def _write_rows(path: Path, *rows: dict[str, object]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_objective_outcomes_join_costs_by_durable_arm_identity(tmp_path: Path) -> None:
    task = tmp_path / "task.jsonl"
    _write_rows(
        task,
        {"meta": True, "result_schema_version": 1, "run_id": "run-1"},
        {
            "result_schema_version": 1,
            "run_id": "run-1",
            "experiment_pair_id": "run-1:task-1",
            "arm": "control",
            "classification": "PASS",
            "agent_stderr": "tokens used\n100\n",
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:01+00:00",
        },
        {
            "result_schema_version": 1,
            "run_id": "run-1",
            "experiment_pair_id": "run-1:task-1",
            "arm": "guidance",
            "classification": "TASK_FAIL",
            "agent_stderr": "tokens used\n200\n",
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:02+00:00",
        },
    )
    experiment = tmp_path / "experiment.jsonl"
    _write_rows(
        experiment,
        {"meta": True, "result_schema_version": 2, "experiment_id": "experiment-1"},
        {
            "result_schema_version": 2,
            "experiment_id": "experiment-1",
            "pair": 0,
            "arm": "neutral_a",
            "classification": "PASS",
        },
        {
            "result_schema_version": 2,
            "experiment_id": "experiment-1",
            "pair": 0,
            "arm": "neutral_b",
            "classification": "TASK_FAIL",
        },
    )

    report = measure_objective_outcomes([task, experiment])

    assert report.artifacts == 2
    assert report.arms == report.judgeable_arms == 4
    assert report.passing_arms == report.failing_arms == 2
    assert report.judgeable_guidance_pairs == report.guidance_pairs == 1
    assert report.control_better == 1
    assert report.guidance_better == report.guidance_tied == 0
    assert report.judgeable_neutral_pairs == report.neutral_pairs == 1
    assert report.neutral_disagreements == 1
    assert report.reported_tokens == 300
    assert report.token_arms == 2
    assert report.elapsed_ms == (1000.0, 2000.0)
    assert report.paired_arm_costs["control"].reported_tokens == 100
    assert report.paired_arm_costs["control"].elapsed_ms == (1000.0,)
    assert report.paired_arm_costs["guidance"].reported_tokens == 200
    assert report.paired_arm_costs["guidance"].elapsed_ms == (2000.0,)
    assert report.paired_arm_costs["neutral_a"].reported_tokens is None
    assert report.paired_arm_costs["neutral_a"].arms == 1
    rendered = render_objective_outcomes(report)
    assert "agent_reported_tokens=300 (2/4 arms)" in rendered
    assert "guidance_better=0, control_better=1, tied=0" in rendered
    assert "control tokens=100 (1/1 arms), elapsed=avg=1000.00ms" in rendered
    assert "guidance tokens=200 (1/1 arms), elapsed=avg=2000.00ms" in rendered
    assert "neutral_a tokens=unknown (0/1 arms), elapsed=unknown (0/1)" in rendered
    assert "disagreements=1" in rendered


def test_objective_outcomes_reject_unknown_persisted_schema(tmp_path: Path) -> None:
    path = tmp_path / "future.jsonl"
    _write_rows(
        path,
        {
            "result_schema_version": 99,
            "experiment_id": "future",
            "pair": 0,
            "arm": "neutral_a",
            "classification": "PASS",
        },
    )

    with pytest.raises(ObjectiveOutcomeError, match="unsupported experiment result schema 99"):
        measure_objective_outcomes([path])


def test_objective_outcomes_project_v3_experiment_agent_costs(tmp_path: Path) -> None:
    path = tmp_path / "experiment-v3.jsonl"
    _write_rows(
        path,
        {
            "result_schema_version": 3,
            "experiment_id": "experiment-3",
            "pair": 0,
            "arm": "control",
            "classification": "PASS",
            "agent_reported_tokens": 1234,
            "agent_elapsed_ms": 125.5,
        },
        {
            "result_schema_version": 3,
            "experiment_id": "experiment-3",
            "pair": 0,
            "arm": "guidance",
            "classification": "PASS",
            "agent_reported_tokens": 2345,
            "agent_elapsed_ms": 250,
        },
    )

    report = measure_objective_outcomes([path])

    assert report.reported_tokens == 3579
    assert report.token_arms == 2
    assert report.elapsed_ms == (125.5, 250.0)
    rendered = render_objective_outcomes(report)
    assert "agent_reported_tokens=3579 (2/2 arms)" in rendered
    assert "control tokens=1234 (1/1 arms), elapsed=avg=125.50ms" in rendered
    assert "guidance tokens=2345 (1/1 arms), elapsed=avg=250.00ms" in rendered


def test_objective_arm_costs_do_not_pair_rows_with_different_pair_ids(tmp_path: Path) -> None:
    path = tmp_path / "incomplete-pairs.jsonl"
    _write_rows(
        path,
        {
            "result_schema_version": 3,
            "experiment_id": "experiment-3",
            "pair": 0,
            "arm": "control",
            "classification": "PASS",
            "agent_reported_tokens": 100,
        },
        {
            "result_schema_version": 3,
            "experiment_id": "experiment-3",
            "pair": 1,
            "arm": "guidance",
            "classification": "PASS",
            "agent_reported_tokens": 200,
        },
    )

    report = measure_objective_outcomes([path])

    assert report.guidance_pairs == 0
    assert report.paired_arm_costs == {}
    assert "guidance arm costs" not in render_objective_outcomes(report)


def test_runtime_costs_keep_surfaces_domains_and_coverage_separate() -> None:
    hook = [
        _record(
            0,
            TraceEvent(
                "tool_proposal",
                {
                    "tool_use_id": "call-1",
                    "turn_id": "legacy-turn",
                    "files": ["src/a.py"],
                    "resource": "workspace",
                },
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
            app(
                "token_usage",
                {
                    "total": {
                        "totalTokens": 14,
                        "inputTokens": 10,
                        "cachedInputTokens": 2,
                        "cacheWriteInputTokens": 1,
                        "outputTokens": 4,
                        "reasoningOutputTokens": 1,
                    }
                },
                arrival_seq=4,
            ),
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
    assert report.surfaces["hook"].actions_by_family == {"tool": 1}
    assert report.surfaces["hook"].failed_outcomes_by_family == {"tool": 1}
    assert report.surfaces["hook"].unique_resources == 2
    assert report.surfaces["hook"].resource_actions == 1
    assert report.surfaces["app_server"].actions == 1
    assert report.surfaces["app_server"].action_observations == 2
    assert report.surfaces["app_server"].outcomes == 1
    assert report.surfaces["app_server"].classified_outcomes == 1
    assert report.surfaces["app_server"].actions_by_family == {"command": 1}
    assert report.completed_turns == report.token_turns == 1
    assert report.cumulative_main_tokens == 14
    assert report.main_token_breakdown.input.value == 10
    assert report.main_token_breakdown.input.covered_sessions == 1
    assert report.main_token_breakdown.reasoning_output.value == 1
    assert report.reviewer_calls == 1
    assert report.reviewer_tokens == 25
    assert report.reviewer_jobs_queued == report.reviewer_jobs_started == 1
    assert report.reviewer_queue_ms == (4.0,)
    assert report.reviewer_inference_finishes == 1
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
    assert "command actions=1 failed=0/1 classified" in rendered
    assert "resources=2 unique (1/1 actions declared)" in rendered
    assert "jobs queued=1 started=1 decided=0 errors=0 capped=0 discarded=0 stale=0" in rendered
    assert "Main tokens: 14 (1/2 sessions) cumulative/unknown-scope" in rendered
    assert "input=10 (1/2 sessions)" in rendered
    assert "cpu=0.250s, peak_rss=1024 bytes; samples=1/1 gate calls" in rendered
    assert "arrival_order=5/5" in rendered
    assert "turn_wall(source)=avg=2000.00ms max=2000.00ms (1/1)" in rendered


def test_supervision_lifecycle_uses_correlated_monotonic_receipts() -> None:
    def event(
        kind: str,
        payload: dict[str, object],
        *,
        event_id: str,
        monotonic_ns: int,
        clock_id: str = "daemon-1",
    ) -> TraceEvent:
        return TraceEvent(
            kind,
            payload,
            event_id=event_id,
            observed_monotonic_ns=monotonic_ns,
            monotonic_clock_id=clock_id,
        )

    events = [
        event("tool_result", {}, event_id="evidence-1", monotonic_ns=1_000_000_000),
        event("tool_result", {}, event_id="evidence-2", monotonic_ns=2_000_000_000),
        event(
            "signal_candidate",
            {
                "signal_id": "signal-1",
                "status": "active",
                "evidence_event_ids": ["evidence-1", "evidence-2"],
            },
            event_id="signal-event-1",
            monotonic_ns=2_100_000_000,
        ),
        event(
            "review_job_queued",
            {"review_job_id": "job-1", "signal_id": "signal-1"},
            event_id="queued-1",
            monotonic_ns=2_200_000_000,
        ),
        event(
            "review_inference_started",
            {"review_job_id": "job-1", "queue_ms": 999.0},
            event_id="started-1",
            monotonic_ns=2_300_000_000,
        ),
        event(
            "reviewer_decision",
            {
                "review_job_id": "job-1",
                "timing": {"inference_ms": 500.0},
            },
            event_id="decision-1",
            monotonic_ns=2_800_000_000,
        ),
        event(
            "review_job_queued",
            {"review_job_id": "job-2", "signal_id": "signal-1"},
            event_id="queued-2",
            monotonic_ns=3_000_000_000,
        ),
        event(
            "reviewer_capped",
            {"review_job_id": "job-2"},
            event_id="capped-2",
            monotonic_ns=3_100_000_000,
        ),
        event(
            "review_job_queued",
            {"review_job_id": "job-3", "signal_id": "signal-1"},
            event_id="queued-3",
            monotonic_ns=3_200_000_000,
        ),
        event(
            "review_job_discarded",
            {"review_job_id": "job-3"},
            event_id="discarded-3",
            monotonic_ns=3_300_000_000,
        ),
        event(
            "review_job_queued",
            {"review_job_id": "job-4", "signal_id": "signal-1"},
            event_id="queued-4",
            monotonic_ns=3_400_000_000,
        ),
        event(
            "review_inference_started",
            {"review_job_id": "job-4"},
            event_id="started-4",
            monotonic_ns=3_500_000_000,
        ),
        event(
            "review_job_stale",
            {"review_job_id": "job-4"},
            event_id="stale-4",
            monotonic_ns=3_600_000_000,
        ),
        event(
            "reviewer_error",
            {"review_job_id": "job-4", "timing": {"inference_ms": 200.0}},
            event_id="error-4",
            monotonic_ns=3_700_000_000,
        ),
        event(
            "tool_result",
            {},
            event_id="foreign-evidence",
            monotonic_ns=4_000_000_000,
            clock_id="daemon-before-restart",
        ),
        event(
            "signal_candidate",
            {
                "signal_id": "signal-2",
                "status": "active",
                "evidence_event_ids": ["foreign-evidence"],
            },
            event_id="signal-event-2",
            monotonic_ns=100_000_000,
        ),
    ]
    records = [_record(index, item) for index, item in enumerate(events)]

    report = measure_runtime_costs([(records, 10)])

    assert report.signal_candidates_active == 2
    assert report.signal_detection_ms == (1100.0,)
    assert report.reviewer_jobs_queued == 4
    assert report.reviewer_jobs_started == 2
    assert report.reviewer_jobs_decided == 1
    assert report.reviewer_jobs_errored == 1
    assert report.reviewer_jobs_capped == 1
    assert report.reviewer_jobs_discarded == 1
    assert report.reviewer_jobs_stale == 1
    assert report.reviewer_queue_ms == (100.0, 100.0)
    assert report.reviewer_inference_finishes == 2
    assert report.reviewer_inference_ms == (500.0, 200.0)
    assert report.reviewer_end_to_end_ms == (1800.0,)
    assert report.monotonic_receipt_events == len(records)
    assert report.monotonic_clock_domains == 2

    rendered = render_runtime_costs(report)
    assert "signal_delay=avg=1100.00ms max=1100.00ms (1/2)" in rendered
    assert "detection_to_decision=avg=1800.00ms max=1800.00ms (1/1)" in rendered
    assert "jobs queued=4 started=2 decided=1 errors=1 capped=1 discarded=1 stale=1" in rendered
    assert "monotonic_receipt=16/16 across 2 clock domains" in rendered


def test_supervision_lead_and_lag_use_the_target_turn_monotonic_boundary() -> None:
    def event(
        kind: str,
        turn_id: str,
        monotonic_ns: int,
        *,
        job_id: str | None = None,
        clock_id: str = "daemon-1",
    ) -> TraceEvent:
        identity = RuntimeIdentity(
            ThreadId("thread-1"),
            TurnId(turn_id),
            None,
            IdentityProvenance("codex", "external-thread", turn_id),
        )
        payload: dict[str, object] = {}
        if job_id is not None:
            payload = {
                "review_job_id": job_id,
                "target_turn_id": turn_id,
                "target_connection_epoch": 1,
            }
        return TraceEvent(
            kind,
            payload,
            identity=identity,
            connection_epoch=1,
            observed_monotonic_ns=monotonic_ns,
            monotonic_clock_id=clock_id,
        )

    events = [
        event("reviewer_decision", "turn-lead", 1_000_000_000, job_id="job-lead"),
        event("turn_completed", "turn-lead", 1_200_000_000),
        event("turn_completed", "turn-lag", 2_000_000_000),
        event("reviewer_decision", "turn-lag", 2_300_000_000, job_id="job-lag"),
        event("reviewer_decision", "turn-restart", 3_000_000_000, job_id="job-restart"),
        event(
            "turn_completed",
            "turn-restart",
            3_200_000_000,
            clock_id="daemon-after-restart",
        ),
    ]

    report = measure_runtime_costs([([_record(i, item) for i, item in enumerate(events)], 10)])

    assert report.reviewer_jobs_decided == 3
    assert report.reviewer_decision_lead_ms == (200.0,)
    assert report.reviewer_decision_lag_ms == (300.0,)
    rendered = render_runtime_costs(report)
    assert "decision_boundary lead=avg=200.00ms max=200.00ms (1/3)" in rendered
    assert "lag=avg=300.00ms max=300.00ms (1/3)" in rendered


def test_repeated_action_metrics_reuse_durable_signal_features() -> None:
    records = [
        _record(
            0,
            TraceEvent(
                "signal_candidate",
                {
                    "signal_id": "repeat-1",
                    "signal_type": "repeated_equivalent_tool_call",
                    "status": "active",
                    "features": {"consecutive_equivalent_calls": 3},
                },
            ),
        ),
        _record(
            1,
            TraceEvent(
                "signal_candidate_suppressed",
                {
                    "signal_id": "repeat-1",
                    "signal_type": "repeated_equivalent_tool_call",
                    "status": "cooled_down",
                    "features": {"consecutive_equivalent_calls": 5},
                },
            ),
        ),
        _record(
            2,
            TraceEvent(
                "signal_candidate",
                {
                    "signal_id": "repeat-without-feature",
                    "signal_type": "repeated_equivalent_tool_call",
                    "status": "active",
                    "features": {},
                },
            ),
        ),
        _record(
            3,
            TraceEvent(
                "signal_candidate",
                {
                    "signal_id": "frontier-1",
                    "signal_type": "repeated_read_no_frontier",
                    "status": "active",
                    "features": {"reads_without_frontier_expansion": 3},
                },
            ),
        ),
        _record(
            4,
            TraceEvent(
                "signal_candidate_suppressed",
                {
                    "signal_id": "frontier-1",
                    "signal_type": "repeated_read_no_frontier",
                    "status": "cooled_down",
                    "features": {"reads_without_frontier_expansion": 4},
                },
            ),
        ),
        _record(
            5,
            TraceEvent(
                "signal_candidate",
                {
                    "signal_id": "failure-1",
                    "signal_type": "failure_streak",
                    "status": "active",
                    "features": {"consecutive_failures": 2},
                },
            ),
        ),
    ]

    report = measure_runtime_costs([(records, 10)])

    assert report.repeated_equivalent_actions == CoveredCount(4, 1, 2)
    assert report.reads_without_frontier_expansion == CoveredCount(4, 1, 1)
    rendered = render_runtime_costs(report)
    assert "equivalent_actions=4 (1/2 signal lifecycles)" in rendered
    assert "reads_without_frontier=4 (1/1 signal lifecycles)" in rendered


def test_unavailable_runtime_metrics_render_unknown_not_zero() -> None:
    report = measure_runtime_costs([([_record(0, TraceEvent("session_start"), at=None)], 10)])

    rendered = render_runtime_costs(report)
    assert "Main tokens: unknown" in rendered
    assert "resources=unknown (0/0 actions declared)" in rendered
    assert "hook=unknown (0/0)" in rendered
    assert "receipt_wall=0/1" in rendered
    assert "monotonic_receipt=0/1 across 0 clock domains" in rendered
    assert "equivalent_actions=unknown (0/0 signal lifecycles)" in rendered


def test_cumulative_token_updates_use_latest_and_report_field_coverage() -> None:
    first_session = [
        _record(0, TraceEvent("token_usage", {"total": {"totalTokens": 5}})),
        _record(
            1,
            TraceEvent(
                "token_usage",
                {
                    "total": {
                        "totalTokens": 14,
                        "inputTokens": 10,
                        "cachedInputTokens": 2,
                        "outputTokens": 4,
                    }
                },
            ),
        ),
    ]
    second_session = [
        _record(
            0,
            TraceEvent(
                "token_usage",
                {"total": {"totalTokens": 6, "inputTokens": 3}},
            ),
        )
    ]

    report = measure_runtime_costs([(first_session, 10), (second_session, 10)])

    assert report.token_observations == 3
    assert report.cumulative_main_tokens == 20
    assert report.main_token_breakdown.total.covered_sessions == 2
    assert report.main_token_breakdown.input.value == 13
    assert report.main_token_breakdown.input.covered_sessions == 2
    assert report.main_token_breakdown.cached_input.value == 2
    assert report.main_token_breakdown.cached_input.covered_sessions == 1
    assert report.main_token_breakdown.cache_write_input.value is None
    assert report.main_token_breakdown.cache_write_input.covered_sessions == 0

    rendered = render_runtime_costs(report)
    assert "Main tokens: 20 (2/2 sessions)" in rendered
    assert "cached_input=2 (1/2 sessions)" in rendered
    assert "cache_write_input=unknown (0/2 sessions)" in rendered


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


def test_semantic_action_families_reuse_correlated_action_identity() -> None:
    records = [
        _record(0, TraceEvent("command_started", operation_id="command-1")),
        _record(
            1,
            TraceEvent(
                "command_result",
                {"status": "completed"},
                operation_id="command-1",
            ),
        ),
        _record(
            2,
            TraceEvent(
                "file_edit",
                {"status": "failed", "files": ["src/a.py", "src/a.py"]},
                operation_id="edit-1",
            ),
        ),
        _record(
            3,
            TraceEvent(
                "tool_started",
                {"server": "github", "tool": "create_issue"},
                operation_id="mcp-1",
            ),
        ),
        _record(
            4,
            TraceEvent(
                "tool_result",
                {"server": "github", "tool": "create_issue"},
                operation_id="mcp-1",
            ),
        ),
    ]

    cost = measure_runtime_costs([(records, 10)]).surfaces["hook"]

    assert cost.actions == 3
    assert cost.actions_by_family == {"command": 1, "file_change": 1, "tool": 1}
    assert cost.classified_outcomes_by_family == {"command": 1, "file_change": 1}
    assert cost.failed_outcomes_by_family == {"file_change": 1}
    assert cost.unique_resources == 2
    assert cost.resource_actions == 2


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
