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
                {"hook_ms": 3.0, "ipc_ms": 2.0, "daemon_evaluation_ms": 1.0},
            ),
        ),
        _record(
            3,
            TraceEvent(
                "reviewer_decision",
                {"spend": {"session_tokens": 25}},
            ),
        ),
    ]
    identity = RuntimeIdentity(
        ThreadId("thread-1"),
        TurnId("turn-1"),
        None,
        IdentityProvenance("codex", "external-thread", "external-turn"),
    )

    def app(kind: str, payload: dict[str, object], operation: str | None = None) -> TraceEvent:
        return TraceEvent(
            kind,
            payload,
            occurred_at=2.0,
            identity=identity,
            operation_id=operation,
            provenance=TraceProvenance("codex_app_server", "synthetic"),
            connection_epoch=1,
        )

    app_server = [
        _record(0, app("command_started", {}, "command-1")),
        _record(
            1,
            app(
                "command_result",
                {"status": "completed", "exitCode": 0, "durationMs": 10},
                "command-1",
            ),
        ),
        _record(2, app("token_usage", {"total": {"totalTokens": 14}})),
        _record(3, app("turn_completed", {"status": "completed"})),
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
    assert report.gate_calls == 1
    assert report.tool_duration_ms == (10.0,)
    assert report.source_timestamps == 4
    assert report.receipt_timestamps == report.events == 8
    assert report.journal_bytes == 300

    rendered = render_runtime_costs(report)
    assert "turn coverage 1/1" in rendered
    assert "hook: actions=1/2 correlated, outcomes=1/1 correlated" in rendered
    assert "app_server: actions=1/2 correlated, outcomes=1/1 correlated" in rendered


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
