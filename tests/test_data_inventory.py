"""Schema-backed durable-data purge inventory (#89)."""

import json
from pathlib import Path

import pytest

from spotter.data_inventory import DataInventory, DataInventoryError
from spotter.repository_registry import OwnershipConfidence


@pytest.mark.parametrize(
    ("relative", "schema", "version", "encoding"),
    [
        ("sessions/s.jsonl", "spotter.trace_event", 1, "jsonl"),
        ("sessions/s.jsonl.state", "spotter.journal_state", 1, "json"),
        ("labels/s.jsonl", "spotter.label", 6, "jsonl"),
        ("signal-samples/s.jsonl", "spotter.signal_sampling", 1, "jsonl"),
        ("opportunities/s.jsonl", "spotter.intervention_opportunity", 1, "jsonl"),
        ("feedback/interventions.jsonl", "spotter.intervention_feedback", 1, "jsonl"),
        ("source-audit/samples.jsonl", "spotter.source_audit", 1, "jsonl"),
        ("experiments/run.jsonl", "spotter.experiment_result", 3, "jsonl"),
        ("experiments/task-batches/run.jsonl", "spotter.task_batch", 1, "jsonl"),
        ("fork-manifests/fork.json", "spotter.fork_manifest", 6, "json"),
        ("review-spend.json", "spotter.review_spend", 1, "json"),
    ],
)
def test_current_schema_family_is_safe_owned(
    tmp_path: Path, relative: str, schema: str, version: int, encoding: str
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": schema, "schema_version": version}
    text = json.dumps(payload) + ("\n" if encoding == "jsonl" else "")
    path.write_text(text)

    [inspection] = DataInventory(tmp_path).inspect()

    assert inspection.relative_path == relative
    assert inspection.expected_schema == schema
    assert inspection.schema_version == version
    assert inspection.confidence == OwnershipConfidence.SAFE_OWNED


def test_current_data_file_proves_its_exact_lock(tmp_path: Path) -> None:
    path = tmp_path / "labels/s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": "spotter.label", "schema_version": 6}) + "\n")
    lock = path.with_suffix(path.suffix + ".lock")
    lock.touch()

    inspections = {item.relative_path: item for item in DataInventory(tmp_path).inspect()}

    assert inspections["labels/s.jsonl"].confidence == OwnershipConfidence.SAFE_OWNED
    assert inspections["labels/s.jsonl.lock"].confidence == OwnershipConfidence.SAFE_OWNED


def test_non_regular_lock_is_ambiguous(tmp_path: Path) -> None:
    path = tmp_path / "labels/s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": "spotter.label", "schema_version": 6}) + "\n")
    lock = path.with_suffix(path.suffix + ".lock")
    lock.symlink_to(path)

    inspections = {item.relative_path: item for item in DataInventory(tmp_path).inspect()}

    assert inspections["labels/s.jsonl"].confidence == OwnershipConfidence.SAFE_OWNED
    assert inspections["labels/s.jsonl.lock"].confidence == OwnershipConfidence.AMBIGUOUS


def test_broken_root_lock_is_not_silently_ignored(tmp_path: Path) -> None:
    (tmp_path / "review-spend.lock").symlink_to(tmp_path / "missing")

    [inspection] = DataInventory(tmp_path).inspect()

    assert inspection.relative_path == "review-spend.lock"
    assert inspection.confidence == OwnershipConfidence.AMBIGUOUS


def test_regular_orphan_lock_is_ignored(tmp_path: Path) -> None:
    labels = tmp_path / "labels"
    labels.mkdir()
    (labels / "removed.jsonl.lock").touch()

    assert DataInventory(tmp_path).inspect() == ()


def test_symlinked_data_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(DataInventoryError, match="not a directory"):
        DataInventory(linked).inspect()


def test_future_schema_and_empty_history_are_ambiguous(tmp_path: Path) -> None:
    labels = tmp_path / "labels"
    labels.mkdir()
    (labels / "future.jsonl").write_text(
        json.dumps({"schema": "spotter.label", "schema_version": 99}) + "\n"
    )
    (labels / "empty.jsonl").touch()
    (labels / "legacy.jsonl").write_text(json.dumps({"label": "verify"}) + "\n")

    inspections = DataInventory(tmp_path).inspect()

    assert {item.confidence for item in inspections} == {OwnershipConfidence.AMBIGUOUS}
    assert {item.relative_path for item in inspections} == {
        "labels/empty.jsonl",
        "labels/future.jsonl",
        "labels/legacy.jsonl",
    }


def test_symlink_is_never_schema_ownership_proof(tmp_path: Path) -> None:
    outside = tmp_path.parent / "foreign.jsonl"
    outside.write_text(json.dumps({"schema": "spotter.label", "schema_version": 6}) + "\n")
    labels = tmp_path / "labels"
    labels.mkdir()
    (labels / "linked.jsonl").symlink_to(outside)

    [inspection] = DataInventory(tmp_path).inspect()

    assert inspection.confidence == OwnershipConfidence.AMBIGUOUS
    assert "not a regular file" in inspection.reason


def test_remove_revalidates_schema_and_retains_lock(tmp_path: Path) -> None:
    path = tmp_path / "labels/s.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"schema": "spotter.label", "schema_version": 6}) + "\n")
    inventory = DataInventory(tmp_path)
    [inspection] = inventory.inspect()

    result = inventory.remove(inspection)

    assert result.outcome == "removed"
    assert not path.exists()
    assert path.with_suffix(".jsonl.lock").is_file()
    assert inventory.inspect() == ()


def test_remove_refuses_data_that_changed_after_preview(tmp_path: Path) -> None:
    path = tmp_path / "labels/s.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"schema": "spotter.label", "schema_version": 6}) + "\n")
    inventory = DataInventory(tmp_path)
    [inspection] = inventory.inspect()
    path.write_text(json.dumps({"schema": "spotter.label", "schema_version": 99}) + "\n")

    result = inventory.remove(inspection)

    assert result.outcome == "skipped_ambiguous"
    assert path.exists()


def test_remove_refuses_non_regular_lock(tmp_path: Path) -> None:
    path = tmp_path / "labels/s.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"schema": "spotter.label", "schema_version": 6}) + "\n")
    lock = path.with_suffix(".jsonl.lock")
    lock.symlink_to(path)
    inventory = DataInventory(tmp_path)
    inspections = {item.relative_path: item for item in inventory.inspect()}

    result = inventory.remove(inspections["labels/s.jsonl"])

    assert result.outcome == "failed_retryable"
    assert path.exists()


def test_remove_root_ledger_uses_and_retains_root_lock(tmp_path: Path) -> None:
    path = tmp_path / "review-spend.json"
    path.write_text(json.dumps({"schema": "spotter.review_spend", "schema_version": 1}))
    lock = tmp_path / "review-spend.lock"
    lock.touch()
    inventory = DataInventory(tmp_path)
    inspections = {item.relative_path: item for item in inventory.inspect()}

    result = inventory.remove(inspections["review-spend.json"])

    assert result.outcome == "removed"
    assert not path.exists()
    assert lock.is_file()
    assert inventory.inspect() == ()


def test_remove_refuses_replaced_parent_directory(tmp_path: Path) -> None:
    labels = tmp_path / "labels"
    labels.mkdir()
    path = labels / "s.jsonl"
    row = json.dumps({"schema": "spotter.label", "schema_version": 6}) + "\n"
    path.write_text(row)
    inventory = DataInventory(tmp_path)
    [inspection] = inventory.inspect()
    labels.rename(tmp_path / "original-labels")
    foreign = tmp_path / "foreign-labels"
    foreign.mkdir()
    foreign_path = foreign / "s.jsonl"
    foreign_path.write_text(row)
    labels.symlink_to(foreign, target_is_directory=True)

    result = inventory.remove(inspection)

    assert result.outcome == "failed_retryable"
    assert foreign_path.exists()
