import json
from pathlib import Path


def test_fork_fidelity_v1_keeps_uncalibrated_controls_no_go() -> None:
    artifact = json.loads(
        Path("docs/experiments/fork-fidelity-v1.json").read_text(encoding="utf-8")
    )

    assert artifact["decision"] == "NO_GO_REPRESENTATIVE_CAUSAL_USE"
    assert artifact["instrument_controls"]["external_effect_and_observation_gap_exclusion"] is False
    assert artifact["errata"]
    erratum = artifact["errata"][0]
    assert erratum["recorded_at"] == "2026-08-17"
    assert erratum["control"] == "external_effect_and_observation_gap_exclusion"
    assert "observation gaps" in erratum["correction"]
    assert "external effects" in erratum["correction"]
    assert artifact["blocking_gaps"]
    assert artifact["downstream"] == {"issue_34": "NO_GO", "issue_23": "NO_GO"}


def test_natural_failure_v2_null_cohort_keeps_qualification_no_go() -> None:
    artifact = json.loads(
        Path("docs/experiments/fork-natural-failure-v2-result.json").read_text(encoding="utf-8")
    )

    assert artifact["protocol_commit"] == "c5cc70d"
    assert artifact["arms"]["total"] == 6
    assert artifact["arms"]["control"] == {"total": 3, "pass": 3, "task_fail": 0}
    assert artifact["selection"] == {
        "eligible_control_failures": 0,
        "neutral_forks_started": 0,
        "stop_rule_applied": True,
    }
    assert artifact["replay_source_capture"]["succeeded"] == 0
    assert artifact["replay_source_capture"]["failed"] == 6
    assert artifact["replay_source_capture"]["follow_up_issue"] == 307
    assert artifact["decision"] == "NO_GO_REPRESENTATIVE_CAUSAL_USE"
    assert artifact["issue_42_complete"] is False
