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
    assert sum(row["controls"] for row in artifact["source_capture_cohorts"]) == 10
    assert sum(row["captured_sources"] for row in artifact["source_capture_cohorts"]) == 14
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


def test_natural_failure_v3_capture_passes_without_relaxing_qualification() -> None:
    artifact = json.loads(
        Path("docs/experiments/fork-natural-failure-v3-result.json").read_text(encoding="utf-8")
    )

    assert artifact["protocol_commit"] == "1002292"
    assert artifact["arms"]["total"] == 6
    assert artifact["arms"]["control"] == {"total": 3, "pass": 3, "task_fail": 0}
    assert artifact["capture_readiness"]["passed"] is True
    assert artifact["capture_readiness"]["isolated_homes"] is True
    assert artifact["replay_source_capture"] == {"requested": 6, "succeeded": 6, "failed": 0}
    assert [row["control"] for row in artifact["task_results"]] == ["PASS", "PASS", "PASS"]
    assert artifact["selection"] == {
        "eligible_control_failures": 0,
        "neutral_forks_started": 0,
        "stop_rule_applied": True,
    }
    assert artifact["decision"] == "NO_GO_REPRESENTATIVE_CAUSAL_USE"
    assert artifact["issue_42_complete"] is False


def test_natural_failure_v4_new_families_still_keep_qualification_no_go() -> None:
    artifact = json.loads(
        Path("docs/experiments/fork-natural-failure-v4-result.json").read_text(encoding="utf-8")
    )

    assert artifact["protocol_commit"] == "dc4cd0f"
    assert artifact["arms"]["total"] == 8
    assert artifact["arms"]["control"] == {"total": 4, "pass": 4, "task_fail": 0}
    assert len({row["family"] for row in artifact["task_results"]}) == 4
    assert artifact["replay_source_capture"] == {"requested": 8, "succeeded": 8, "failed": 0}
    assert artifact["selection"] == {
        "eligible_control_failures": 0,
        "neutral_forks_started": 0,
        "stop_rule_applied": True,
    }
    assert artifact["calibration"]["failure_region_sampled"] is False
    assert artifact["decision"] == "NO_GO_REPRESENTATIVE_CAUSAL_USE"
    assert artifact["issue_42_complete"] is False
