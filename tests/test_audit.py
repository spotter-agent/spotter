import pytest

from spotter.audit import AuditState, Evidence, Hypothesis, build_state, stale_summary
from spotter.reviewer import review
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent


def _state() -> AuditState:
    state = AuditState()
    state.add_evidence(Evidence("e1", "tool_result", "timeout under high concurrency"))
    state.add_hypothesis(Hypothesis("h1", "redis pool exhaustion causes the timeout"))
    state.support("h1", "e1")
    state.add_hypothesis(Hypothesis("h2", "raise pool size", depends_on={"h1"}))
    return state


def test_summary_text_cannot_become_evidence() -> None:
    # Compile-time: mypy rejects Evidence(id, "summary", ...) — no such member.
    # Runtime guard for untyped callers (e.g. JSON ingestion):
    with pytest.raises(ValueError, match="not an evidence source"):
        Evidence("e2", "summary", "the agent says it is redis")  # type: ignore[arg-type]


def test_retraction_propagates_transitively() -> None:
    state = _state()
    stale = state.retract("e1")
    assert stale == {"h1", "h2"}
    assert state.hypotheses["h1"].status == "stale"
    assert state.hypotheses["h2"].status == "stale"


def test_hypothesis_with_remaining_evidence_survives_retraction() -> None:
    state = _state()
    state.add_evidence(Evidence("e2", "test_output", "profiler shows pool wait"))
    state.support("h1", "e2")
    assert state.retract("e1") == set()
    assert state.hypotheses["h1"].status == "supported"


def test_dependency_cycles_do_not_hang_retraction() -> None:
    state = AuditState()
    state.add_evidence(Evidence("e1", "diff", "d"))
    state.add_hypothesis(Hypothesis("a", "a", depends_on={"b"}))
    state.add_hypothesis(Hypothesis("b", "b", depends_on={"a"}))
    state.support("a", "e1")
    assert state.retract("e1") == {"a", "b"}


def test_supporting_with_retracted_evidence_is_rejected() -> None:
    state = _state()
    state.retract("e1")
    state.add_hypothesis(Hypothesis("h3", "late claim"))
    with pytest.raises(ValueError, match="retracted"):
        state.support("h3", "e1")


def test_duplicate_ids_are_rejected() -> None:
    state = _state()
    with pytest.raises(ValueError, match="duplicate"):
        state.add_evidence(Evidence("e1", "diff", "again"))
    with pytest.raises(ValueError, match="duplicate"):
        state.add_hypothesis(Hypothesis("h1", "again"))


# --- plan P2 wired: the ledger is built from the journal, not from claims ---


def _records(events: list[tuple[str, dict[str, object]]]) -> list[StepRecord]:
    return [
        StepRecord(i, TraceEvent(kind, payload), None) for i, (kind, payload) in enumerate(events)
    ]


def _result(command: str, exit_code: int) -> tuple[str, dict[str, object]]:
    return (
        "tool_result",
        {"tool_input": {"command": command}, "tool_response": {"exit_code": exit_code}},
    )


def test_evidence_comes_only_from_observable_outcomes() -> None:
    state = build_state(
        _records(
            [
                ("reasoning_summary", {"text": "redis is definitely the cause"}),
                _result("pytest -q", 1),
            ]
        )
    )
    assert [e.description for e in state.evidence.values()] == ["pytest -q -> exit 1"]
    assert all("redis" not in e.description for e in state.evidence.values())


def test_same_command_with_a_new_outcome_retracts_the_old_one() -> None:
    state = build_state(_records([_result("pytest -q", 0), _result("pytest -q", 1)]))
    assert len(state.retracted) == 1
    assert state.evidence[next(iter(state.retracted))].description.endswith("exit 0")


def test_repeated_identical_outcomes_retract_nothing() -> None:
    state = build_state(_records([_result("pytest -q", 0), _result("pytest -q", 0)]))
    assert state.retracted == set()


def test_reviewer_hypothesis_goes_stale_when_its_evidence_dies() -> None:
    state = build_state(
        _records(
            [
                _result("pytest -q", 0),
                ("reviewer_decision", {"decision": "verify", "hypothesis": "the suite is green"}),
                _result("pytest -q", 1),
            ]
        )
    )
    assert state.hypotheses["h1"].status == "stale"
    lines = stale_summary(state)
    assert any("RETRACTED" in line for line in lines)
    assert any("STALE hypothesis: the suite is green" in line for line in lines)


def test_continue_decisions_add_no_hypothesis() -> None:
    state = build_state(
        _records([("reviewer_decision", {"decision": "continue", "hypothesis": ""})])
    )
    assert state.hypotheses == {}


def test_reviewer_prompt_carries_invalidated_premises() -> None:
    seen: dict[str, str] = {}

    def runner(model: str, prompt: str) -> str:
        seen["prompt"] = prompt
        return (
            '{"decision": "nudge", "failure_class": "tool_failure_loop", "reason": "x",'
            ' "confidence": 0.7, "hypothesis": "the config is loaded"}'
        )

    records = _records(
        [
            _result("pytest -q", 0),
            ("reviewer_decision", {"decision": "verify", "hypothesis": "the suite is green"}),
            _result("pytest -q", 1),
        ]
    )
    decision, _ = review(records, "m", runner=runner)
    assert "RETRACTED pytest -q -> exit 0" in seen["prompt"]
    assert "STALE hypothesis: the suite is green" in seen["prompt"]
    assert decision.hypothesis == "the config is loaded"


def test_clean_trajectory_adds_no_premise_block() -> None:
    seen: dict[str, str] = {}

    def runner(model: str, prompt: str) -> str:
        seen["prompt"] = prompt
        return (
            '{"decision": "continue", "failure_class": "none", "reason": "ok",'
            ' "confidence": 0.5, "hypothesis": ""}'
        )

    review(_records([_result("pytest -q", 0)]), "m", runner=runner)
    assert "RETRACTED pytest" not in seen["prompt"]


def test_codex_text_exit_codes_are_read_too() -> None:
    """Real Codex responses are raw strings with an 'Exit code: n' line, not
    the structured dict the first tests assumed."""
    records = _records(
        [
            (
                "tool_result",
                {"tool_input": {"command": "apply"}, "tool_response": "ok\nExit code: 0\n"},
            ),
            (
                "tool_result",
                {"tool_input": {"command": "apply"}, "tool_response": "boom\nExit code: 1\n"},
            ),
        ]
    )
    state = build_state(records)
    assert len(state.evidence) == 2
    assert len(state.retracted) == 1


def test_results_without_an_observable_outcome_are_not_invented() -> None:
    """Codex shell results carry no exit code; guessing pass/fail from output
    text would retract evidence every time `git status` changed."""
    state = build_state(
        _records(
            [
                (
                    "tool_result",
                    {"tool_input": {"command": "git status"}, "tool_response": "A file"},
                ),
                (
                    "tool_result",
                    {"tool_input": {"command": "git status"}, "tool_response": "B file"},
                ),
            ]
        )
    )
    assert state.evidence == {} and state.retracted == set()
