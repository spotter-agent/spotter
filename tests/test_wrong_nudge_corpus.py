import hashlib
from pathlib import Path
from typing import Any

import pytest

from spotter.wrong_nudge_corpus import (
    FramingCondition,
    NudgeKind,
    WrongNudgeCorpusError,
    build_wrong_nudge_arms,
    validate_wrong_nudge_set,
)


def _write_manifest(root: Path, *, nudge_id: str = "wrong/test-001") -> Path:
    path = root / "nudge.toml"
    path.write_text(
        f'''wrong_nudge_schema = "spotter.wrong_nudge"
wrong_nudge_schema_version = 1
wrong_nudge_id = "{nudge_id}"
source_task = "fixture/task-001"
prefix_family = "localized-bug-fix"
nudge_kind = "false_causal_premise"
false_premise = "The wrong premise."
contradicting_evidence = "A fact already visible in the fixture contradicts it."
intended_scope = "CURRENT_DECISION"
payload_template = "Act on the wrong premise."
payload_version = 1
expected_healthy_response = "REFUTE_WITH_AVAILABLE_EVIDENCE"
'''
    )
    return path


def _write_set(root: Path, manifest: Path, *, digest: str) -> Path:
    path = root / "set.toml"
    path.write_text(
        f'''wrong_nudge_set_schema = "spotter.wrong_nudge_set"
wrong_nudge_set_schema_version = 1
wrong_nudge_set_id = "test-wrong-nudges"
version = 1

[[nudges]]
wrong_nudge_id = "wrong/test-001"
manifest = "{manifest.name}"
sha256 = "{digest}"
'''
    )
    return path


def test_validates_hashed_wrong_nudge_manifest(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    corpus = validate_wrong_nudge_set(
        _write_set(tmp_path, manifest, digest=hashlib.sha256(manifest.read_bytes()).hexdigest())
    )

    assert corpus.wrong_nudge_set_id == "test-wrong-nudges"
    assert corpus.version == 1
    assert corpus.nudges[0].kind is NudgeKind.FALSE_CAUSAL_PREMISE
    assert corpus.nudges[0].contradicting_evidence.startswith("A fact")


def test_rejects_changed_manifest(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    path = _write_set(tmp_path, manifest, digest="0" * 64)

    with pytest.raises(WrongNudgeCorpusError, match="manifest sha256 mismatch"):
        validate_wrong_nudge_set(path)


def test_rejects_unknown_nudge_kind(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path)
    manifest.write_text(manifest.read_text().replace("false_causal_premise", "guess"))
    path = _write_set(tmp_path, manifest, digest=hashlib.sha256(manifest.read_bytes()).hexdigest())

    with pytest.raises(WrongNudgeCorpusError, match="nudge_kind must be one of"):
        validate_wrong_nudge_set(path)


def test_repo_wrong_nudge_corpus_is_frozen_and_covers_required_kinds() -> None:
    root = Path(__file__).parents[1] / "corpus"
    corpus = validate_wrong_nudge_set(root / "wrong-nudges-v1.toml")

    assert len(corpus.nudges) == 7
    assert {nudge.kind for nudge in corpus.nudges} == set(NudgeKind)
    assert all(nudge.false_premise != nudge.contradicting_evidence for nudge in corpus.nudges)


def test_builds_equivalent_prefix_framing_arms() -> None:
    root = Path(__file__).parents[1] / "corpus"
    nudge = validate_wrong_nudge_set(root / "wrong-nudges-v1.toml").nudges[0]

    arms = build_wrong_nudge_arms(
        nudge,
        source_session_id="source-session",
        source_step=7,
        prefix_id="prefix-sha256",
        environment_fingerprint="environment-sha256",
    )

    assert {arm.condition for arm in arms} == set(FramingCondition)
    assert len({(arm.source_session_id, arm.source_step) for arm in arms}) == 1
    assert len({arm.prefix_id for arm in arms}) == 1
    assert len({arm.environment_fingerprint for arm in arms}) == 1
    assert {arm.manifest_sha256 for arm in arms} == {nudge.manifest_sha256}
    assert {arm.payload_version for arm in arms} == {nudge.payload_version}
    by_condition = {arm.condition: arm for arm in arms}
    assert by_condition[FramingCondition.NEUTRAL_CONTROL].payload is None
    assert by_condition[FramingCondition.RAW_IMPERATIVE].payload == nudge.payload_template
    advisory = by_condition[FramingCondition.SPOTTER_ADVISORY].payload or ""
    assert "not a new user requirement" in advisory
    assert advisory.endswith(f"NUDGE: {nudge.payload_template}")
    verify = by_condition[FramingCondition.VERIFY_FIRST].payload or ""
    assert verify.endswith(nudge.false_premise)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("source_session_id", "", "source_session_id"),
        ("prefix_id", " ", "prefix_id"),
        ("environment_fingerprint", "", "environment_fingerprint"),
        ("source_step", -1, "source_step"),
        ("source_step", False, "source_step"),
    ),
)
def test_wrong_nudge_arms_require_complete_prefix_provenance(
    field: str, value: str | int, message: str
) -> None:
    root = Path(__file__).parents[1] / "corpus"
    nudge = validate_wrong_nudge_set(root / "wrong-nudges-v1.toml").nudges[0]
    kwargs: dict[str, Any] = {
        "source_session_id": "source-session",
        "source_step": 7,
        "prefix_id": "prefix-sha256",
        "environment_fingerprint": "environment-sha256",
    }
    kwargs[field] = value

    with pytest.raises(WrongNudgeCorpusError, match=message):
        build_wrong_nudge_arms(nudge, **kwargs)
