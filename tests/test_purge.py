"""Conservative repository-aware purge preview (#89)."""

import json
import subprocess
from pathlib import Path

import pytest

import spotter.cli as cli
from spotter.cli import main
from spotter.hook import journal_path
from spotter.integration import MANIFEST_SCHEMA, IntegrationManifest
from spotter.log_registry import LogRegistry, LogRegistryError, OwnedLog
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


def _ref_exists(repo: Path, sha: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/spotter/steps/{sha}"],
            cwd=repo,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


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


def test_snapshot_purge_dry_run_plans_worktree_then_ref(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    worktree = restore_snapshot(repo, sha, tmp_path / "restored")

    assert main(["purge", "--snapshots", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "snapshots"
    assert payload["dry_run"] is True
    assert payload["deletion_supported"] is True
    assert {resource["outcome"] for resource in payload["resources"]} == {"planned"}
    assert worktree.exists()
    assert _ref_exists(repo, sha)


def test_snapshot_purge_removes_worktree_then_ref(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    worktree = restore_snapshot(repo, sha, tmp_path / "restored")

    assert main(["purge", "--snapshots", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is False
    assert {resource["outcome"] for resource in payload["resources"]} == {"removed"}
    assert {resource["presence"] for resource in payload["resources"]} == {"ABSENT"}
    assert not worktree.exists()
    assert not _ref_exists(repo, sha)

    assert main(["purge", "--snapshots", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {resource["outcome"] for resource in payload["resources"]} == {"already_absent"}


def test_snapshot_purge_keeps_ref_when_worktree_removal_fails(
    repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = snapshot_worktree(repo)
    worktree = restore_snapshot(repo, sha, tmp_path / "restored")
    monkeypatch.setattr(cli, "_remove_owned_worktree", lambda _item: "busy")

    assert main(["purge", "--snapshots", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    outcomes = {resource["resource_type"]: resource["outcome"] for resource in payload["resources"]}
    assert outcomes == {
        "git_ref": "skipped_referenced",
        "worktree": "failed_retryable",
    }
    assert worktree.exists()
    assert _ref_exists(repo, sha)


def test_snapshot_purge_skips_referenced_ref(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    StepJournal(journal_path({"session_id": "live"})).record(
        TraceEvent("tool_proposal", {"cwd": str(repo)}), snapshot=sha
    )

    assert main(["purge", "--snapshots", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["outcome"] == "skipped_referenced"
    assert resource["references"] == ["journal:live:step:0"]
    assert _ref_exists(repo, sha)


def test_snapshot_purge_continues_past_inaccessible_repository(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    healthy_sha = snapshot_worktree(repo)
    other = tmp_path / "other"
    other.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test"],
    ):
        subprocess.run(command, cwd=other, check=True)
    (other / "tracked.txt").write_text("other")
    other_sha = snapshot_worktree(other)
    moved = tmp_path / "other-moved"
    other.rename(moved)

    assert main(["purge", "--snapshots", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    outcomes = {
        resource["expected_target"]: resource["outcome"] for resource in payload["resources"]
    }
    assert outcomes == {
        healthy_sha: "removed",
        other_sha: "skipped_ambiguous",
    }
    assert not _ref_exists(repo, healthy_sha)
    assert _ref_exists(moved, other_sha)


def test_log_purge_dry_run_then_clear_is_idempotent(
    spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = LogRegistry()
    log = registry.log_dir / "spotterd.log"
    assert registry.claim(log, "spotterd") is True
    log.write_text("owned log")

    assert main(["purge", "--logs", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "logs"
    assert payload["dry_run"] is True
    assert payload["resources"][0]["outcome"] == "planned"
    assert payload["resources"][0]["size_bytes"] == len("owned log")
    assert log.read_text() == "owned log"

    assert main(["purge", "--logs", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resources"][0]["outcome"] == "removed"
    assert payload["resources"][0]["removed_bytes"] == len("owned log")
    assert log.read_bytes() == b""
    assert registry.load(), "ownership evidence survives content purge"

    assert main(["purge", "--logs", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resources"][0]["outcome"] == "already_absent"


def test_log_purge_reports_unregistered_file_without_deleting_it(
    spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    logs = spotter_home / "logs"
    logs.mkdir(parents=True)
    foreign = logs / "foreign.log"
    foreign.write_text("keep")

    assert main(["purge", "--logs", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    [resource] = payload["resources"]
    assert resource["group"] == "AMBIGUOUS"
    assert resource["outcome"] == "skipped_ambiguous"
    assert foreign.read_text() == "keep"


def test_log_purge_clears_owned_anchor_but_preserves_replaced_public_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = LogRegistry()
    log = registry.log_dir / "spotterd.log"
    assert registry.claim(log, "spotterd") is True
    log.write_text("owned")
    log.unlink()
    log.write_text("foreign")

    assert main(["purge", "--logs", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    outcomes = {resource["group"]: resource["outcome"] for resource in payload["resources"]}
    assert outcomes == {"SAFE_OWNED": "removed", "AMBIGUOUS": "skipped_ambiguous"}
    assert log.read_text() == "foreign"
    [owned] = registry.load()
    assert Path(owned.identity_path).read_bytes() == b""


def test_log_purge_continues_after_retryable_clear_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = LogRegistry()
    daemon_log = registry.log_dir / "spotterd.log"
    review_log = registry.log_dir / "review-s1.log"
    assert registry.claim(daemon_log, "spotterd") is True
    assert registry.claim(review_log, "review:s1") is True
    daemon_log.write_text("daemon")
    review_log.write_text("review")
    original_clear = LogRegistry.clear

    def selective_clear(self: LogRegistry, resource: OwnedLog) -> int:
        if resource.resource_id == "spotterd":
            raise LogRegistryError("busy")
        return original_clear(self, resource)

    monkeypatch.setattr(LogRegistry, "clear", selective_clear)

    assert main(["purge", "--logs", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    outcomes = {resource["resource_id"]: resource["outcome"] for resource in payload["resources"]}
    assert outcomes == {"spotterd": "failed_retryable", "review:s1": "removed"}
    assert daemon_log.read_text() == "daemon"
    assert review_log.read_bytes() == b""


def test_data_purge_preview_reports_current_journal_family(
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = StepJournal(journal_path({"session_id": "owned"}))
    journal.record(TraceEvent("user_prompt", {"prompt": "test"}))

    assert main(["purge", "--data", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "data"
    assert payload["deletion_supported"] is True
    resources = {resource["resource_id"]: resource for resource in payload["resources"]}
    assert set(resources) == {
        "sessions/owned.jsonl",
        "sessions/owned.jsonl.lock",
        "sessions/owned.jsonl.state",
    }
    assert {resource["group"] for resource in resources.values()} == {"SAFE_OWNED"}
    assert resources["sessions/owned.jsonl"]["outcome"] == "planned"
    assert resources["sessions/owned.jsonl.state"]["outcome"] == "planned"
    assert resources["sessions/owned.jsonl.lock"]["outcome"] == "preserved_synchronization"
    assert journal.path.exists(), "preview must not delete data"


def test_data_purge_removes_schema_proven_files_and_retains_lock(
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal = StepJournal(journal_path({"session_id": "owned"}))
    journal.record(TraceEvent("user_prompt", {"prompt": "test"}))
    lock = journal.path.with_suffix(journal.path.suffix + ".lock")

    assert main(["purge", "--data", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    resources = {resource["resource_id"]: resource for resource in payload["resources"]}
    assert resources["sessions/owned.jsonl"]["outcome"] == "removed"
    assert resources["sessions/owned.jsonl.state"]["outcome"] == "removed"
    assert resources["sessions/owned.jsonl.lock"]["outcome"] == "preserved_synchronization"
    assert not journal.path.exists()
    assert not journal.path.with_suffix(journal.path.suffix + ".state").exists()
    assert lock.is_file()

    assert main(["purge", "--data", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["resources"] == []


def test_data_purge_removes_safe_data_but_preserves_ambiguous_data(
    spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = spotter_home / "labels"
    labels.mkdir(parents=True)
    safe = labels / "safe.jsonl"
    safe.write_text(json.dumps({"schema": "spotter.label", "schema_version": 6}) + "\n")
    ambiguous = labels / "legacy.jsonl"
    ambiguous.write_text(json.dumps({"verdict": "verify"}) + "\n")

    assert main(["purge", "--data", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    resources = {resource["resource_id"]: resource for resource in payload["resources"]}
    assert resources["labels/safe.jsonl"]["outcome"] == "removed"
    assert resources["labels/legacy.jsonl"]["outcome"] == "skipped_ambiguous"
    assert not safe.exists()
    assert ambiguous.exists()


def test_data_purge_continues_after_one_lock_failure(
    spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = spotter_home / "labels"
    labels.mkdir(parents=True)
    row = json.dumps({"schema": "spotter.label", "schema_version": 6}) + "\n"
    blocked = labels / "blocked.jsonl"
    blocked.write_text(row)
    blocked.with_suffix(".jsonl.lock").symlink_to(blocked)
    removable = labels / "removable.jsonl"
    removable.write_text(row)

    assert main(["purge", "--data", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    resources = {resource["resource_id"]: resource for resource in payload["resources"]}
    assert resources["labels/blocked.jsonl"]["outcome"] == "failed_retryable"
    assert resources["labels/blocked.jsonl.lock"]["outcome"] == "skipped_ambiguous"
    assert resources["labels/removable.jsonl"]["outcome"] == "removed"
    assert blocked.exists()
    assert not removable.exists()


def test_data_purge_preview_surfaces_unknown_or_corrupt_data(
    spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = spotter_home / "labels"
    labels.mkdir(parents=True)
    (labels / "broken.jsonl").write_text("{")
    source = spotter_home / "task-sources"
    source.mkdir()
    (source / "foreign.txt").write_text("keep")

    assert main(["purge", "--data", "--dry-run", "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    resources = {resource["resource_id"]: resource for resource in payload["resources"]}
    assert set(resources) == {"labels/broken.jsonl", "task-sources"}
    assert {resource["group"] for resource in resources.values()} == {"AMBIGUOUS"}
    assert (source / "foreign.txt").read_text() == "keep"


def test_data_purge_preview_excludes_other_scope_roots(
    spotter_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    logs = spotter_home / "logs"
    logs.mkdir(parents=True)
    (logs / "foreign.log").write_text("keep")
    (spotter_home / "spotter.toml").write_text("[main_agent]\nadapter = 'codex'\n")

    assert main(["purge", "--data", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["resources"] == []
    assert (logs / "foreign.log").read_text() == "keep"

    assert main(["purge", "--data", "--json"]) == 0
    capsys.readouterr()
    assert (logs / "foreign.log").read_text() == "keep"
    assert (spotter_home / "spotter.toml").exists()


def test_integration_purge_preview_is_non_mutating(
    spotter_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir()
    hook = {"type": "command", "command": "/bin/spotter hook"}
    owned = {"event": "PreToolUse", "matcher": ".*", "hook": hook}
    hooks_path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [hook]}]}})
    )
    manifest_path = spotter_home / "integrations/codex.json"
    IntegrationManifest(
        schema=MANIFEST_SCHEMA,
        state="ready",
        agent="codex",
        setup_by="test",
        agent_path="/bin/codex",
        agent_version="codex 1.0",
        codex_home=str(codex_home),
        app_server_strategy="pending-external",
        app_server_endpoint=None,
        runtime_mode="portable",
        service_registration=None,
        service_owned=False,
        hooks_file=str(hooks_path),
        hooks_file_created=True,
        owned_hooks=[owned],
    ).save(manifest_path)
    (manifest_path.parent / "codex.lock").touch()
    before = hooks_path.read_bytes()

    assert main(["purge", "--integration", "--dry-run", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "integration"
    assert payload["deletion_supported"] is False
    assert {resource["group"] for resource in payload["resources"]} == {"SAFE_OWNED"}
    assert manifest_path.exists()
    assert hooks_path.read_bytes() == before


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
        main(["purge", "--all", "--snapshots", "--dry-run"])
    with pytest.raises(SystemExit):
        main(["purge", "--logs", "--snapshots", "--dry-run"])
    with pytest.raises(SystemExit):
        main(["purge", "--data", "--logs", "--dry-run"])
    with pytest.raises(SystemExit):
        main(["purge", "--integration", "--data", "--dry-run"])
    with pytest.raises(SystemExit):
        main(["purge", "--snapshots", "--apply"])
    with pytest.raises(SystemExit):
        main(["purge", "--logs", "--apply"])
    with pytest.raises(SystemExit):
        main(["purge", "--integration"])
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

    assert main(["purge", "--snapshots", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert {resource["outcome"] for resource in payload["resources"]} == {"skipped_ambiguous"}
    assert _ref_exists(repo, sha)


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
