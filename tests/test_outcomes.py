import pytest

from spotter.outcomes import outcome_failure


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"tool_response": {"exit_code": 0}}, False),
        ({"tool_response": {"exit_code": 2}}, True),
        ({"tool_response": {"ok": True}}, False),
        ({"tool_response": {"ok": False}}, True),
        ({"tool_response": "output\nExit code: 0\n"}, False),
        ({"exitCode": 1}, True),
        ({"success": True}, False),
        ({"status": "cancelled"}, True),
        ({"status": "completed"}, False),
        ({"tool_response": {"exit_code": True}}, None),
        ({"tool_response": "output without an exit status"}, None),
        ({}, None),
    ],
)
def test_outcome_failure_normalizes_runtime_shapes(
    payload: dict[str, object], expected: bool | None
) -> None:
    assert outcome_failure(payload) is expected


def test_failure_signal_wins_over_conflicting_success_signal() -> None:
    assert outcome_failure({"exitCode": 1, "status": "completed"}) is True
