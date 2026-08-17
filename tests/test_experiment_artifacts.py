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
