from spotter.effects import classify, effect_event, external_effects
from spotter.snapshot import StepRecord
from spotter.trace import TraceEvent


def test_representative_reversibility_classes() -> None:
    read = classify("Bash", {"command": "git status --short"})
    local = classify("apply_patch", {"path": "src/app.py"})
    remote = classify("Bash", {"command": "git push origin feature"})
    assert (read.reversibility_class, read.kind) == ("A", "observation")
    assert (local.reversibility_class, local.reversible) == ("B", True)
    assert (remote.reversibility_class, remote.kind, remote.resource) == (
        "C",
        "git_remote_write",
        "origin",
    )


def test_external_tool_writes_are_conservatively_class_c() -> None:
    write = classify("mcp__github__create_issue", {"repository": "org/repo"})
    read = classify("mcp__github__list_issues", {"repository": "org/repo"})
    assert (write.reversibility_class, write.resource) == ("C", "org/repo")
    assert read.reversibility_class == "A"


def test_class_c_result_becomes_effect_with_recovery_identity() -> None:
    result = TraceEvent(
        "tool_result",
        {
            "reversibility_class": "C",
            "effect_kind": "git_remote_write",
            "resource": "origin",
            "tool_response": {"exit_code": 0},
            "checkpoint": "abc123",
            "turn_id": "turn-2",
            "tool_use_id": "call-7",
        },
    )
    effect = effect_event(result)
    assert effect is not None
    assert effect.payload == {
        "kind": "git_remote_write",
        "resource": "origin",
        "result": "succeeded",
        "reversible": False,
        "checkpoint": "abc123",
        "turn_id": "turn-2",
        "tool_use_id": "call-7",
    }
    records = [StepRecord(4, effect, "abc123")]
    assert external_effects(records) == [effect.payload]
    assert external_effects(records, through_step=3) == []
