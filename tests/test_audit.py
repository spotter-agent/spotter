import pytest

from spotter.audit import AuditState, Evidence, Hypothesis


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
