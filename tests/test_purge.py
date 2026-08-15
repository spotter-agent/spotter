"""Conservative repository-aware purge preview (#89)."""

import json
import subprocess
from pathlib import Path

import pytest

from spotter.cli import main
from spotter.hook import journal_path
from spotter.replay import FORK_MANIFEST_SCHEMA, FORK_MANIFEST_SCHEMA_VERSION
from spotter.repository_registry import RepositoryRegistry
from spotter.snapshot import StepJournal, restore_snapshot, snapshot_worktree
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def spotter_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "spotter"
    monkeypatch.setenv("SPOTTER_HOME", str(home))
    return home


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(command, cwd=repository, check=True)
    (repository / "tracked.txt").write_text("one")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)
    return repository


def _fork_manifest(repo: Path, spotter_home: Path, sha: str, fork_id: str) -> dict[str, object]:
    return {
        "schema": FORK_MANIFEST_SCHEMA,
        "schema_version": FORK_MANIFEST_SCHEMA_VERSION,
        "fork_id": fork_id,
        "status": "READY",
        "prefix": {
            "prefix_id": f"prefix-{fork_id}",
            "source_session_id": "source",
            "branch_step": 0,
            "source_event_id": None,
            "source_turn_id": None,
            "connection_epoch": None,
            "journal_schema_version": 1,
            "tool_use_id": "call-1",
            "repository_path": str(repo),
            "repository_id": "repository-1",
            "snapshot_sha": sha,
            "snapshot_tree_sha": "tree-1",
            "rollout_prefix_sha256": "rollout-1",
            "agent": "codex",
            "model": None,
            "runtime_version": None,
            "agent_config": "not_captured",
            "context_source": "test",
            "context_limitations": [],
            "external_effects": [],
            "observation_gaps": 0,
            "created_at": "2026-08-15T00:00:00+00:00",
        },
        "worktree": str(spotter_home / f"forks/{fork_id}"),
        "rollout": None,
        "environment": None,
        "created_at": "2026-08-15T00:00:00+00:00",
        "updated_at": "2026-08-15T00:00:00+00:00",
        "failure": None,
        "source_environment_preflight": "MATCHED",
    }


def test_purge_preview_reports_exact_ref_and_worktree_without_deleting(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    worktree = restore_snapshot(repo, sha, tmp_path / "restored")

    assert main(["purge", "--all", "--dry-run"]) == 0

    output = capsys.readouterr().out
    assert "SAFE_OWNED (1)" in output
    assert "REFERENCED (1)" in output
    assert f"retained by: worktree:{worktree}" in output
    assert f"refs/spotter/steps/{sha}" in output
    assert str(worktree) in output
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/spotter/steps/{sha}"],
            cwd=repo,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    assert worktree.exists()


def test_purge_preview_json_is_machine_readable(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)

    assert main(["purge", "--all", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["deletion_supported"] is False
    assert payload["summary"] == {
        "AMBIGUOUS": 0,
        "INACCESSIBLE": 0,
        "REFERENCED": 0,
        "SAFE_OWNED": 1,
    }
    assert payload["resources"][0]["resource_id"] == f"refs/spotter/steps/{sha}"
    assert payload["resources"][0]["retention"] == "UNREFERENCED"


def test_missing_repository_is_inaccessible(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_worktree(repo)
    repo.rename(tmp_path / "moved-without-rediscovery")

    assert main(["purge", "--all", "--dry-run"]) == 1

    output = capsys.readouterr().out
    assert "INACCESSIBLE (1)" in output
    assert "path is unavailable" in output


def test_recreated_repository_path_is_ambiguous(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_worktree(repo)
    repo.rename(tmp_path / "original")
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    assert main(["purge", "--all", "--dry-run"]) == 1

    output = capsys.readouterr().out
    assert "AMBIGUOUS (1)" in output
    assert "different Git repository" in output


def test_changed_ref_target_is_ambiguous(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = snapshot_worktree(repo)
    subprocess.run(
        ["git", "update-ref", f"refs/spotter/steps/{sha}", "HEAD"],
        cwd=repo,
        check=True,
    )

    assert main(["purge", "--all", "--dry-run"]) == 1

    output = capsys.readouterr().out
    assert "AMBIGUOUS (1)" in output
    assert "ref target changed" in output


def test_purge_refuses_non_preview_invocation() -> None:
    with pytest.raises(SystemExit):
        main(["purge", "--all"])
    with pytest.raises(SystemExit):
        main(["purge", "codex", "--all", "--dry-run"])


def test_missing_worktree_with_disagreeing_metadata_is_ambiguous(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    worktree = restore_snapshot(repo, sha, tmp_path / "restored")
    [entry] = RepositoryRegistry().load()
    recorded = next(
        resource for resource in entry.resources if resource.resource_type == "worktree"
    )
    assert recorded.expected_git_dir is not None
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=repo,
        check=True,
    )
    Path(recorded.expected_git_dir).mkdir(parents=True)

    assert main(["purge", "--all", "--dry-run"]) == 1

    output = capsys.readouterr().out
    assert "worktree path and Git administrative metadata disagree" in output


def test_journal_reference_retains_snapshot(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sha = snapshot_worktree(repo)
    StepJournal(journal_path({"session_id": "live"})).record(
        TraceEvent("tool_proposal", {"cwd": str(repo)}), snapshot=sha
    )

    assert main(["purge", "--all", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["REFERENCED"] == 1
    [resource] = payload["resources"]
    assert resource["group"] == "REFERENCED"
    assert resource["retention"] == "REFERENCED"
    assert resource["references"] == ["journal:live:step:0"]


def test_recovery_checkpoint_retains_snapshot(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    StepJournal(journal_path({"session_id": "recovery"})).record(
        TraceEvent("tool_result", {"cwd": str(repo), "checkpoint": sha})
    )

    assert main(["purge", "--all", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["group"] == "REFERENCED"
    assert resource["references"] == ["journal:recovery:step:0"]


def test_manual_pin_retains_snapshot_until_removed(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    assert main(["pins", "add", "--repo", str(repo), "--snapshot", sha]) == 0
    pin_id = capsys.readouterr().out.strip().rsplit(" ", 1)[-1]
    assert main(["pins", "add", "--repo", str(repo), "--snapshot", sha]) == 0
    assert capsys.readouterr().out.strip().endswith(pin_id)

    assert main(["pins", "list"]) == 0
    assert f"{pin_id} {sha} {repo}" in capsys.readouterr().out
    assert main(["purge", "--all", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["group"] == "REFERENCED"
    assert resource["references"] == [f"manual_pin:{pin_id}"]

    assert main(["pins", "remove", "--pin-id", pin_id]) == 0
    capsys.readouterr()
    assert main(["purge", "--all", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resources"][0]["group"] == "SAFE_OWNED"


def test_manual_pin_refuses_unowned_commit(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    snapshot_worktree(repo)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert main(["pins", "add", "--repo", str(repo), "--snapshot", head]) == 1

    assert "is not an exact Spotter-owned snapshot" in capsys.readouterr().err


def test_future_manual_pin_store_makes_snapshot_ambiguous(
    repo: Path, spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_worktree(repo)
    (spotter_home / "snapshot-pins.json").write_text(
        json.dumps(
            {
                "schema": "spotter.snapshot_pins",
                "schema_version": 99,
                "pins": [],
            }
        )
    )

    assert main(["purge", "--all", "--dry-run", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["group"] == "AMBIGUOUS"
    assert "manual pin reachability unavailable" in resource["retention_diagnostics"][0]


def test_manual_pin_without_repository_registry_fails_preview(
    spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spotter_home.mkdir()
    (spotter_home / "snapshot-pins.json").write_text(
        json.dumps(
            {
                "schema": "spotter.snapshot_pins",
                "schema_version": 1,
                "pins": [
                    {
                        "pin_id": "pin-1",
                        "registry_entry_id": "missing",
                        "snapshot_sha": "abc123",
                        "created_at": "2026-08-15T00:00:00+00:00",
                    }
                ],
            }
        )
    )

    assert main(["purge", "--all", "--dry-run"]) == 1

    assert "has no repository ownership record" in capsys.readouterr().err


def test_journal_reference_survives_repository_move(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    StepJournal(journal_path({"session_id": "before-move"})).record(
        TraceEvent("tool_proposal", {"cwd": str(repo)}), snapshot=sha
    )
    moved = tmp_path / "moved"
    repo.rename(moved)
    assert snapshot_worktree(moved, sha) == sha

    assert main(["purge", "--all", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["group"] == "REFERENCED"
    assert resource["references"] == ["journal:before-move:step:0"]


def test_fork_manifest_reference_retains_snapshot(
    repo: Path, spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    directory = spotter_home / "fork-manifests"
    directory.mkdir(parents=True)
    (directory / "fork-1.json").write_text(
        json.dumps(_fork_manifest(repo, spotter_home, sha, "fork-1"))
    )

    assert main(["purge", "--all", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["group"] == "REFERENCED"
    assert resource["references"] == ["fork_manifest:fork-1:READY"]


def test_corrupt_fork_manifest_makes_unreferenced_snapshot_ambiguous(
    repo: Path, spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_worktree(repo)
    directory = spotter_home / "fork-manifests"
    directory.mkdir(parents=True)
    (directory / "broken.json").write_text("{")

    assert main(["purge", "--all", "--dry-run", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["group"] == "AMBIGUOUS"
    assert resource["retention"] == "UNKNOWN"
    assert "fork manifest reachability unavailable" in resource["retention_diagnostics"][0]


def test_experiment_result_reference_retains_external_fork_manifest(
    repo: Path, spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    archived = spotter_home / "archived"
    archived.mkdir(parents=True)
    manifest_path = archived / "fork-result.json"
    manifest_path.write_text(json.dumps(_fork_manifest(repo, spotter_home, sha, "result")))
    experiments = spotter_home / "experiments"
    experiments.mkdir()
    (experiments / "result.jsonl").write_text(
        json.dumps({"fork_manifest": str(manifest_path)}) + "\n"
    )

    assert main(["purge", "--all", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["group"] == "REFERENCED"
    assert resource["references"] == [
        "experiment_result:experiments/result.jsonl:line:1:fork:result"
    ]


def test_missing_experiment_result_manifest_makes_snapshot_ambiguous(
    repo: Path, spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot_worktree(repo)
    experiments = spotter_home / "experiments"
    experiments.mkdir(parents=True)
    (experiments / "result.jsonl").write_text(json.dumps({"fork_manifest": "missing.json"}) + "\n")

    assert main(["purge", "--all", "--dry-run", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["group"] == "AMBIGUOUS"
    assert resource["retention"] == "UNKNOWN"
    assert "experiment result reachability unavailable" in resource["retention_diagnostics"][0]
