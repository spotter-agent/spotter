import json
import shlex
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from spotter.hook import journal_path
from spotter.identity import IdentityProvenance, RuntimeIdentity, ThreadId, TurnId
from spotter.labels import add_label, labels_path
from spotter.replay import (
    BranchCoverageStatus,
    EnvironmentDrift,
    ForkStatus,
    ReplayError,
    branch_coverage,
    branch_coverage_to_json,
    compare_environments,
    fingerprint_environment,
    fork,
    fork_rollout,
    load_fork_manifest,
)
from spotter.sampling import sample_signal_silence
from spotter.snapshot import SnapshotError, StepJournal, snapshot_worktree
from spotter.trace import TraceEvent

OLD_ID = "aaaa1111-bbbb-2222-cccc-333344445555"


@pytest.fixture(autouse=True)
def spotter_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "spotter"))
    return tmp_path / "spotter"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("v1")
    return repo


@pytest.fixture()
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex"
    day = home / "sessions" / "2026" / "08" / "11"
    day.mkdir(parents=True)
    lines = [
        {
            "ordinal": 0,
            "type": "session_meta",
            "payload": {"session_id": OLD_ID, "id": OLD_ID, "cli_version": "test-1"},
        },
        {
            "ordinal": 1,
            "type": "response_item",
            "payload": {"call_id": "call_A", "name": "exec", "model": "gpt-test"},
        },
        {"ordinal": 2, "type": "response_item", "payload": {"call_id": "call_A", "output": "ok"}},
        {
            "ordinal": 3,
            "type": "turn_context",
            "payload": {
                "model": "gpt-test",
                "effort": "low",
                "approval_policy": "never",
                "personality": "pragmatic",
                "sandbox_policy": {"type": "workspace-write", "writable_roots": ["secret"]},
                "collaboration_mode": {"mode": "default", "developer_instructions": "secret"},
            },
        },
        {"ordinal": 4, "type": "response_item", "payload": {"call_id": "call_B", "name": "exec"}},
    ]
    rollout = day / f"rollout-2026-08-11T10-00-00-{OLD_ID}.jsonl"
    rollout.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return home


def _journal(session: str, records: list[tuple[TraceEvent, str | None]]) -> None:
    journal = StepJournal(journal_path({"session_id": session}))
    for event, snapshot in records:
        journal.record(event, snapshot=snapshot)


def _commit_baseline(repo: Path) -> None:
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)


def test_fork_rollout_truncates_and_renames(codex_home: Path) -> None:
    rollout = next((codex_home / "sessions").rglob("*.jsonl"))
    forked = fork_rollout(rollout, "call_B", "new-id-1234")
    lines = forked.read_text().splitlines()
    assert len(lines) == 4  # cut strictly before call_B
    assert OLD_ID not in forked.name and "new-id-1234" in forked.name
    assert all(OLD_ID not in line for line in lines)
    assert rollout.read_text().count("call_B") == 1  # original untouched


def test_branch_coverage_classifies_state_context_effects_and_gaps(
    repo: Path, codex_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_A",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                None,
            ),
            (TraceEvent("sessionstart"), sha),
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "missing-call",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                None,
            ),
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_A",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                None,
            ),
            (TraceEvent("external_effect", {"result": "succeeded"}), None),
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_B",
                        "cwd": str(repo),
                        "reversibility_class": "B",
                    },
                ),
                None,
            ),
            (TraceEvent("observation_gap"), None),
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_B",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                None,
            ),
        ],
    )

    report = branch_coverage(OLD_ID, codex_home)

    assert [point.status for point in report.points] == [
        BranchCoverageStatus.NOT_FORKABLE_STATE,
        BranchCoverageStatus.NOT_FORKABLE_CONTEXT,
        BranchCoverageStatus.FORKABLE_EXACT,
        BranchCoverageStatus.UNSAFE_EXTERNAL_EFFECT,
        BranchCoverageStatus.OBSERVATION_GAP,
    ]
    assert report.earliest_forkable_step == 3
    assert (report.pre_mutation_forkable, report.pre_mutation_candidates) == (1, 3)
    assert report.counts["FORKABLE_EXACT"] == 1
    assert json.loads(branch_coverage_to_json(report))["points"][2]["status"] == "FORKABLE_EXACT"

    from spotter.cli import main

    assert main(["fork-coverage", "--session", OLD_ID]) == 0
    cli_report = json.loads(capsys.readouterr().out)
    assert cli_report["candidates"] == 5


def test_branch_coverage_does_not_treat_legacy_effects_as_clean(
    repo: Path, codex_home: Path
) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (TraceEvent("sessionstart"), sha),
            (
                TraceEvent("tool_proposal", {"tool_use_id": "call_A", "cwd": str(repo)}),
                None,
            ),
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_B",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                None,
            ),
        ],
    )

    report = branch_coverage(OLD_ID, codex_home)

    assert report.points[0].status == BranchCoverageStatus.FORKABLE_EXACT
    assert report.points[1].status == BranchCoverageStatus.UNSAFE_EXTERNAL_EFFECT


def test_branch_coverage_reports_signal_trigger_followups(repo: Path, codex_home: Path) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_A",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                sha,
            ),
            (
                TraceEvent(
                    "signal_candidate",
                    {
                        "signal_id": "signal-1",
                        "signal_type": "failure_streak",
                        "status": "active",
                        "source_event_id": "result-2",
                    },
                ),
                None,
            ),
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_B",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                None,
            ),
            (
                TraceEvent(
                    "signal_candidate",
                    {
                        "signal_id": "signal-2",
                        "signal_type": "failure_streak",
                        "status": "active",
                        "source_event_id": "result-4",
                    },
                ),
                None,
            ),
            (
                TraceEvent(
                    "signal_candidate",
                    {
                        "signal_id": "signal-2",
                        "signal_type": "failure_streak",
                        "status": "resolved",
                        "source_event_id": "result-5",
                    },
                ),
                None,
            ),
        ],
    )
    records = StepJournal.load(journal_path({"session_id": OLD_ID}))
    add_label(OLD_ID, 1, "tp", "signal opportunity", records)
    add_label(OLD_ID, 3, "fp", "late signal", records)

    report = branch_coverage(OLD_ID, codex_home)

    assert (report.signal_triggers, report.signal_trigger_followups) == (2, 1)
    assert report.signal_trigger_followups_forkable == 1
    assert report.signal_trigger_points[0].proposal_step == 2
    assert report.signal_trigger_points[0].status == BranchCoverageStatus.FORKABLE_EXACT
    assert report.signal_trigger_points[1].proposal_step is None
    assert report.labeled_opportunities == report.labeled_opportunities_current == 2
    assert report.labeled_opportunity_branch_points == 1
    assert [point.proposal_step for point in report.labeled_opportunity_points] == [2, None]
    rendered = json.loads(branch_coverage_to_json(report))
    assert rendered["signal_trigger_points"][0]["status"] == "FORKABLE_EXACT"


def test_branch_coverage_reports_labeled_intervention_opportunities(
    repo: Path, codex_home: Path
) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_A",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                    event_id="source-A",
                    identity=RuntimeIdentity(
                        ThreadId("thread-1"),
                        TurnId("turn-1"),
                        None,
                        IdentityProvenance("codex", "thread-1", "turn-1"),
                    ),
                ),
                sha,
            ),
            (TraceEvent("gate_shadow_block", {"tool_use_id": "call_A"}), None),
            (TraceEvent("reviewer_decision", {"decision": "nudge"}), None),
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_B",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                None,
            ),
            (TraceEvent("reviewer_decision", {"decision": "verify"}), None),
        ],
    )
    records = StepJournal.load(journal_path({"session_id": OLD_ID}))
    sample_signal_silence(OLD_ID, records, "failure_streak", ("tool_proposal",), 1)
    add_label(
        OLD_ID,
        0,
        "miss",
        "signal opportunity",
        records,
        signal_type="failure_streak",
    )
    add_label(OLD_ID, 1, "tp", "gate opportunity", records)
    add_label(OLD_ID, 2, "tp", "reviewer opportunity", records)
    add_label(OLD_ID, 3, "miss", "missed proposal opportunity", records)
    stale = add_label(OLD_ID, 4, "unclear", "no follow-up", records)
    with labels_path(OLD_ID).open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(asdict(replace(stale, fingerprint="stale"))) + "\n")

    report = branch_coverage(OLD_ID, codex_home)

    assert (report.labeled_opportunities, report.labeled_opportunities_current) == (5, 4)
    assert report.labeled_opportunity_branch_points == 4
    assert report.labeled_opportunity_branch_points_forkable == 4
    assert [point.proposal_step for point in report.labeled_opportunity_points] == [
        3,
        0,
        3,
        3,
        None,
    ]
    assert report.labeled_opportunity_points[4].stale is True
    assert report.labeled_opportunity_points[0].scope == "signal:failure_streak"
    rendered = json.loads(branch_coverage_to_json(report))
    assert rendered["labeled_opportunity_points"][1]["status"] == "FORKABLE_EXACT"


def test_fork_rollout_only_rewrites_session_metadata(codex_home: Path) -> None:
    rollout = next((codex_home / "sessions").rglob("*.jsonl"))
    lines = rollout.read_text().splitlines()
    lines.insert(
        1,
        json.dumps({"type": "event", "payload": {"message": f"keep literal session {OLD_ID}"}}),
    )
    rollout.write_text("\n".join(lines) + "\n")
    forked = fork_rollout(rollout, "call_B", "new-id")
    records = [json.loads(line) for line in forked.read_text().splitlines()]
    assert records[0]["payload"]["session_id"] == "new-id"
    assert records[1]["payload"]["message"].endswith(OLD_ID)


def test_fork_rollout_unknown_call_id_fails_loudly(codex_home: Path) -> None:
    rollout = next((codex_home / "sessions").rglob("*.jsonl"))
    with pytest.raises(ReplayError, match="call_id call_X not found"):
        fork_rollout(rollout, "call_X", "new-id")


def test_fork_rollout_invalid_json_fails_cleanly(codex_home: Path) -> None:
    rollout = next((codex_home / "sessions").rglob("*.jsonl"))
    lines = rollout.read_text().splitlines()
    lines.insert(3, "not-json")
    rollout.write_text("\n".join(lines) + "\n")
    with pytest.raises(ReplayError, match="invalid rollout JSON on line 4"):
        fork_rollout(rollout, "call_B", "new-id")


def test_fork_end_to_end(repo: Path, codex_home: Path) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (TraceEvent("sessionstart"), None),
            (
                TraceEvent(
                    "tool_proposal",
                    {"tool": "apply_patch", "tool_use_id": "call_B", "cwd": str(repo)},
                ),
                sha,
            ),
        ],
    )
    plan = fork(OLD_ID, 1, codex_home=codex_home, guidance="check the stack trace first")
    assert Path(plan.worktree, "a.txt").read_text() == "v1"
    assert plan.session_id in plan.command and str(plan.worktree) in plan.command
    assert "check the stack trace first" in plan.command
    assert Path(plan.rollout).exists()
    assert plan.manifest and plan.prefix_id and plan.environment_fingerprint
    manifest = load_fork_manifest(Path(plan.manifest))
    assert manifest.status == ForkStatus.READY
    assert manifest.prefix.prefix_id == plan.prefix_id
    assert manifest.prefix.snapshot_sha == sha
    assert manifest.prefix.rollout_prefix_sha256
    assert manifest.prefix.context_source == "truncated_codex_rollout"
    assert manifest.prefix.agent == "codex"
    assert manifest.prefix.model == "gpt-test"
    assert manifest.prefix.runtime_version == "test-1"
    assert json.loads(manifest.prefix.agent_config) == {
        "approval_policy": "never",
        "collaboration_mode": "default",
        "effort": "low",
        "personality": "pragmatic",
        "sandbox_policy": "workspace-write",
    }
    assert "secret" not in manifest.prefix.agent_config
    assert manifest.environment is not None
    assert manifest.environment.snapshot_sha == sha
    assert manifest.environment.fingerprint_sha256 == plan.environment_fingerprint
    assert Path(plan.manifest).stat().st_mode & 0o777 == 0o600
    assert shlex.split(plan.command) == [
        "codex",
        "exec",
        "-C",
        plan.worktree,
        "resume",
        "--json",
        plan.session_id,
        "check the stack trace first",
    ]


def test_fork_command_shell_quotes_guidance(repo: Path, codex_home: Path) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent("tool_proposal", {"tool_use_id": "call_B", "cwd": str(repo)}),
                sha,
            )
        ],
    )
    guidance = "$(touch /tmp/should-not-run)"
    plan = fork(OLD_ID, 0, codex_home=codex_home, guidance=guidance)
    assert shlex.split(plan.command)[-1] == guidance


def test_fork_removes_rollout_when_restore_fails(
    repo: Path,
    codex_home: Path,
    spotter_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent("tool_proposal", {"tool_use_id": "call_B", "cwd": str(repo)}),
                sha,
            )
        ],
    )
    original = set((codex_home / "sessions").rglob("*.jsonl"))

    def fail_restore(*args: object) -> Path:
        raise SnapshotError("restore failed")

    monkeypatch.setattr("spotter.replay.restore_snapshot", fail_restore)
    with pytest.raises(SnapshotError, match="restore failed"):
        fork(OLD_ID, 0, codex_home=codex_home)
    assert set((codex_home / "sessions").rglob("*.jsonl")) == original
    manifests = list((spotter_home / "fork-manifests").glob("*.json"))
    assert len(manifests) == 1
    failed = load_fork_manifest(manifests[0])
    assert failed.status == ForkStatus.FAILED
    assert failed.failure == "restore failed"
    assert failed.rollout is None


def test_fork_without_snapshot_names_the_missing_ingredient(codex_home: Path) -> None:
    _journal(
        OLD_ID,
        [(TraceEvent("tool_proposal", {"tool_use_id": "call_B", "cwd": "/x"}), None)],
    )
    with pytest.raises(ReplayError, match="no snapshot at or before"):
        fork(OLD_ID, 0, codex_home=codex_home)


def test_fork_without_tool_use_id_names_the_missing_ingredient(codex_home: Path) -> None:
    _journal(OLD_ID, [(TraceEvent("tool_proposal", {"cwd": "/x"}), "deadbeef")])
    with pytest.raises(ReplayError, match="no tool_use_id"):
        fork(OLD_ID, 0, codex_home=codex_home)


def test_fork_rejects_non_proposal_steps(codex_home: Path) -> None:
    _journal(OLD_ID, [(TraceEvent("tool_result"), None)])
    with pytest.raises(ReplayError, match="fork at a tool_proposal"):
        fork(OLD_ID, 0, codex_home=codex_home)


def test_fork_rollout_matches_event_msg_item_ids(codex_home: Path) -> None:
    """Real hooks journal harness ids (exec-…) that appear as event_msg
    payload.item.id, not response_item call_id — both must match."""
    rollout = next((codex_home / "sessions").rglob("*.jsonl"))
    lines = rollout.read_text().splitlines()
    lines.append(
        json.dumps(
            {
                "ordinal": 4,
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"id": "exec-1234", "type": "FileChange"},
                },
            }
        )
    )
    rollout.write_text("\n".join(lines) + "\n")
    forked = fork_rollout(rollout, "exec-1234", "new-id")
    assert len(forked.read_text().splitlines()) == 5  # cut before the event_msg


def test_code_mode_correlates_hook_ids_to_exact_pre_call_cut(repo: Path, codex_home: Path) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (TraceEvent("sessionstart"), sha),
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "exec-read",
                        "proposal_number": 1,
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                None,
            ),
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "exec-patch",
                        "proposal_number": 2,
                        "cwd": str(repo),
                        "reversibility_class": "B",
                    },
                ),
                None,
            ),
        ],
    )
    rollout = next((codex_home / "sessions").rglob("*.jsonl"))
    metadata = json.loads(rollout.read_text().splitlines()[0])
    calls = [
        {
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "call_id": call_id, "name": "exec"},
        }
        for call_id in ("call-read", "call-patch")
    ]
    rollout.write_text("\n".join(json.dumps(line) for line in [metadata, *calls]) + "\n")

    report = branch_coverage(OLD_ID, codex_home)
    assert [point.status for point in report.points] == [
        BranchCoverageStatus.FORKABLE_EXACT,
        BranchCoverageStatus.FORKABLE_EXACT,
    ]
    assert report.pre_mutation_forkable == 1

    plan = fork(OLD_ID, 1, codex_home=codex_home)
    assert len(Path(plan.rollout).read_text().splitlines()) == 1
    assert load_fork_manifest(Path(plan.manifest or "")).prefix.tool_use_id == "call-read"


def test_code_mode_rejects_incomplete_sequence_correlation(repo: Path, codex_home: Path) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "exec-read",
                        "proposal_number": 1,
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                sha,
            )
        ],
    )
    rollout = next((codex_home / "sessions").rglob("*.jsonl"))
    with rollout.open("a") as stream:
        for call_id in ("call-observed", "call-unobserved"):
            stream.write(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "call_id": call_id,
                            "name": "exec",
                        },
                    }
                )
                + "\n"
            )
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "item_completed", "item": {"id": "exec-read"}},
                }
            )
            + "\n"
        )

    assert branch_coverage(OLD_ID, codex_home).points[0].status == (
        BranchCoverageStatus.NOT_FORKABLE_CONTEXT
    )
    with pytest.raises(ReplayError, match="no exact rollout call correlation"):
        fork(OLD_ID, 0, codex_home=codex_home)


def test_two_forks_share_prefix_and_equivalent_captured_environments(
    repo: Path, codex_home: Path
) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent("tool_proposal", {"tool_use_id": "call_B", "cwd": str(repo)}),
                sha,
            )
        ],
    )

    first = fork(OLD_ID, 0, codex_home=codex_home)
    second = fork(OLD_ID, 0, codex_home=codex_home)
    first_manifest = load_fork_manifest(Path(first.manifest or ""))
    second_manifest = load_fork_manifest(Path(second.manifest or ""))

    assert first.session_id != second.session_id
    assert first.prefix_id == second.prefix_id
    assert first_manifest.environment and second_manifest.environment
    assert compare_environments(first_manifest.environment, second_manifest.environment).equivalent

    Path(second.worktree, "a.txt").write_text("drifted")
    drifted = fingerprint_environment(Path(second.worktree))
    comparison = compare_environments(first_manifest.environment, drifted)
    assert comparison.equivalent is False
    assert comparison.drift == (EnvironmentDrift.TRACKED_STATE_MISMATCH,)

    Path(first.worktree, "a.txt").write_text("first content")
    Path(second.worktree, "a.txt").write_text("second content")
    first_changed = fingerprint_environment(Path(first.worktree))
    second_changed = fingerprint_environment(Path(second.worktree))
    assert first_changed.status_sha256 == second_changed.status_sha256
    assert first_changed.tracked_diff_sha256 != second_changed.tracked_diff_sha256
    assert compare_environments(first_changed, second_changed).drift == (
        EnvironmentDrift.TRACKED_STATE_MISMATCH,
    )


def test_environment_comparison_classifies_tool_and_platform_drift(
    repo: Path,
) -> None:
    sha = snapshot_worktree(repo)
    worktree = repo.parent / "fingerprint"
    subprocess.run(["git", "worktree", "add", "--detach", str(worktree), sha], cwd=repo, check=True)
    baseline = fingerprint_environment(worktree)
    changed = replace(baseline, python_version="different", platform_machine="different")

    comparison = compare_environments(baseline, changed)

    assert comparison.drift == (
        EnvironmentDrift.TOOL_VERSION_DRIFT,
        EnvironmentDrift.UNKNOWN_ENVIRONMENT_DRIFT,
    )


def test_environment_comparison_classifies_submodule_drift_separately(repo: Path) -> None:
    sha = snapshot_worktree(repo)
    worktree = repo.parent / "submodule-fingerprint"
    subprocess.run(["git", "worktree", "add", "--detach", str(worktree), sha], cwd=repo, check=True)
    baseline = fingerprint_environment(worktree)
    submodule_changed = replace(baseline, submodule_status_sha256="different")

    assert compare_environments(baseline, submodule_changed).drift == (
        EnvironmentDrift.SYMLINK_OR_SUBMODULE_MISMATCH,
    )

    tracked_and_submodule_changed = replace(
        submodule_changed,
        tracked_diff_sha256="different",
    )
    assert compare_environments(baseline, tracked_and_submodule_changed).drift == (
        EnvironmentDrift.TRACKED_STATE_MISMATCH,
        EnvironmentDrift.SYMLINK_OR_SUBMODULE_MISMATCH,
    )


def test_declared_ignored_resource_is_not_hidden_by_matching_forks(
    repo: Path, codex_home: Path
) -> None:
    _commit_baseline(repo)
    (repo / ".gitignore").write_text(".env\n")
    (repo / ".env").write_text(str(repo.resolve()))
    (repo / "fixture.json").write_text('{"version": 1}')
    source = fingerprint_environment(repo, (".env",)).declared_resources[0]
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_A",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                sha,
            )
        ],
    )

    plan = fork(
        OLD_ID,
        0,
        codex_home=codex_home,
        environment_resources=(".env", "fixture.json"),
    )
    manifest = load_fork_manifest(Path(plan.manifest or ""))

    assert source.worktree_path_reference is True
    assert plan.source_environment_preflight == ("SOURCE_ENVIRONMENT_MISMATCH:MISSING_IGNORED_FILE")
    assert manifest.source_environment_preflight == plan.source_environment_preflight
    assert manifest.environment is not None
    assert tuple(resource.path for resource in manifest.environment.declared_resources) == (
        ".env",
        "fixture.json",
    )
    assert manifest.environment.declared_resources[0].state == "missing"
    assert manifest.environment.declared_resources[1].sha256 is not None


def test_declared_environment_resource_cannot_escape_worktree(repo: Path) -> None:
    with pytest.raises(ReplayError, match="relative path"):
        fingerprint_environment(repo, ("../secret",))


def test_declared_environment_variable_is_hashed_and_compared(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit_baseline(repo)
    monkeypatch.setenv("SPOTTER_FIXTURE_MODE", "source-value")

    first = fingerprint_environment(
        repo,
        environment_variables=("SPOTTER_OPTIONAL_MODE", "SPOTTER_FIXTURE_MODE"),
    )
    variables = first.declared_environment_variables

    assert tuple(variable.name for variable in variables) == (
        "SPOTTER_FIXTURE_MODE",
        "SPOTTER_OPTIONAL_MODE",
    )
    assert variables[0].state == "set"
    assert variables[0].sha256 is not None
    assert variables[1].state == "missing"
    assert "source-value" not in json.dumps(asdict(first))

    monkeypatch.setenv("SPOTTER_FIXTURE_MODE", "changed-value")
    changed = fingerprint_environment(
        repo,
        environment_variables=("SPOTTER_FIXTURE_MODE", "SPOTTER_OPTIONAL_MODE"),
    )

    assert compare_environments(first, changed).drift == (
        EnvironmentDrift.ENVIRONMENT_VARIABLE_MISMATCH,
    )


def test_declared_environment_variable_requires_posix_name(repo: Path) -> None:
    with pytest.raises(ReplayError, match="POSIX name"):
        fingerprint_environment(repo, environment_variables=("NOT-VALID",))


def test_fork_rejects_declared_values_that_keep_the_source_worktree_path(
    repo: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = repo / "fixture.json"
    config.write_text(json.dumps({"cache": str(repo.resolve() / ".cache")}))
    monkeypatch.setenv("SPOTTER_CACHE_DIR", str(repo.resolve() / ".cache"))
    subprocess.run(["git", "add", "a.txt", "fixture.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent(
                    "tool_proposal",
                    {"tool_use_id": "call_A", "cwd": str(repo), "reversibility_class": "A"},
                ),
                sha,
            )
        ],
    )

    plan = fork(
        OLD_ID,
        0,
        codex_home=codex_home,
        environment_resources=("fixture.json",),
        environment_variables=("SPOTTER_CACHE_DIR",),
    )
    manifest = load_fork_manifest(Path(plan.manifest or ""))

    assert plan.source_environment_preflight == (
        "SOURCE_ENVIRONMENT_MISMATCH:ABSOLUTE_PATH_MISMATCH"
    )
    assert manifest.environment is not None
    assert manifest.environment.declared_resources[0].worktree_path_reference is False
    assert manifest.environment.declared_environment_variables[0].worktree_path_reference is False


def test_declared_directory_fingerprint_tracks_tree_contents(repo: Path) -> None:
    _commit_baseline(repo)
    fixtures = repo / "fixtures"
    (fixtures / "nested").mkdir(parents=True)
    (fixtures / "nested" / "config.json").write_text('{"version": 1}')

    first = fingerprint_environment(repo, ("fixtures",)).declared_resources[0]
    (fixtures / "nested" / "config.json").write_text('{"version": 2}')
    second = fingerprint_environment(repo, ("fixtures",)).declared_resources[0]

    assert first.kind == "directory"
    assert first.state == "untracked"
    assert first.sha256 != second.sha256


def test_declared_directory_rejects_nested_symlink(repo: Path) -> None:
    _commit_baseline(repo)
    fixtures = repo / "fixtures"
    fixtures.mkdir()
    (fixtures / "link").symlink_to(repo / "a.txt")

    with pytest.raises(ReplayError, match="contains a symlink"):
        fingerprint_environment(repo, ("fixtures",))


def test_declared_ignored_directory_loss_is_caught_before_fork_runs(
    repo: Path, codex_home: Path
) -> None:
    _commit_baseline(repo)
    (repo / ".gitignore").write_text(".fixture-cache/\n")
    cache = repo / ".fixture-cache"
    cache.mkdir()
    (cache / "state.json").write_text('{"ready": true}')
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent(
                    "tool_proposal",
                    {"tool_use_id": "call_A", "cwd": str(repo), "reversibility_class": "A"},
                ),
                sha,
            )
        ],
    )

    source = fingerprint_environment(repo, (".fixture-cache",)).declared_resources[0]
    plan = fork(
        OLD_ID,
        0,
        codex_home=codex_home,
        environment_resources=(".fixture-cache",),
    )
    manifest = load_fork_manifest(Path(plan.manifest or ""))

    assert source.kind == "directory"
    assert source.state == "ignored"
    assert plan.source_environment_preflight == ("SOURCE_ENVIRONMENT_MISMATCH:MISSING_IGNORED_FILE")
    assert manifest.environment is not None
    assert manifest.environment.declared_resources[0].kind == "missing"


def test_declared_venv_or_cache_loss_has_specific_drift_category(
    repo: Path, codex_home: Path
) -> None:
    _commit_baseline(repo)
    (repo / ".gitignore").write_text(".venv/\n")
    venv = repo / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent(
                    "tool_proposal",
                    {"tool_use_id": "call_A", "cwd": str(repo), "reversibility_class": "A"},
                ),
                sha,
            )
        ],
    )

    plan = fork(
        OLD_ID,
        0,
        codex_home=codex_home,
        environment_venv_or_cache=(".venv",),
    )
    manifest = load_fork_manifest(Path(plan.manifest or ""))

    assert plan.source_environment_preflight == (
        "SOURCE_ENVIRONMENT_MISMATCH:MISSING_VENV_OR_CACHE"
    )
    assert manifest.environment is not None
    resource = manifest.environment.declared_resources[0]
    assert resource.path == ".venv"
    assert resource.purpose == "venv_or_cache"
    assert resource.state == "missing"


def test_fork_manifest_v1_through_v5_remain_readable(
    repo: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOTTER_FIXTURE_MODE", "safe-value")
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (
                TraceEvent(
                    "tool_proposal",
                    {
                        "tool_use_id": "call_A",
                        "cwd": str(repo),
                        "reversibility_class": "A",
                    },
                ),
                sha,
            )
        ],
    )
    plan = fork(
        OLD_ID,
        0,
        codex_home=codex_home,
        environment_resources=("a.txt",),
        environment_variables=("SPOTTER_FIXTURE_MODE",),
    )
    manifest_path = Path(plan.manifest or "")
    manifest = load_fork_manifest(manifest_path)

    assert manifest.environment is not None
    assert "safe-value" not in manifest_path.read_text()
    monkeypatch.setenv("SPOTTER_FIXTURE_MODE", "changed-value")
    current = fingerprint_environment(
        Path(plan.worktree),
        environment_resources=("a.txt",),
        environment_variables=("SPOTTER_FIXTURE_MODE",),
    )
    assert compare_environments(manifest.environment, current).drift == (
        EnvironmentDrift.ENVIRONMENT_VARIABLE_MISMATCH,
    )

    raw = json.loads(manifest_path.read_text())

    raw["schema_version"] = 5
    for resource in raw["environment"]["declared_resources"]:
        resource.pop("purpose")
    manifest_path.write_text(json.dumps(raw))

    manifest = load_fork_manifest(manifest_path)

    assert manifest.schema_version == 5
    assert manifest.environment is not None
    assert manifest.environment.declared_resources[0].purpose == "resource"

    raw["schema_version"] = 4
    for resource in raw["environment"]["declared_resources"]:
        resource.pop("worktree_path_reference")
    for variable in raw["environment"]["declared_environment_variables"]:
        variable.pop("worktree_path_reference")
    manifest_path.write_text(json.dumps(raw))

    manifest = load_fork_manifest(manifest_path)

    assert manifest.schema_version == 4
    assert manifest.environment is not None
    assert manifest.environment.declared_resources[0].worktree_path_reference is False
    assert manifest.environment.declared_environment_variables[0].worktree_path_reference is False

    raw["schema_version"] = 3
    raw["environment"].pop("declared_environment_variables")
    manifest_path.write_text(json.dumps(raw))

    manifest = load_fork_manifest(manifest_path)

    assert manifest.schema_version == 3
    assert manifest.environment is not None
    assert manifest.environment.declared_environment_variables == ()

    raw["schema_version"] = 2
    for resource in raw["environment"]["declared_resources"]:
        resource.pop("kind")
    manifest_path.write_text(json.dumps(raw))

    manifest = load_fork_manifest(manifest_path)

    assert manifest.schema_version == 2
    assert manifest.environment is not None
    assert manifest.environment.declared_resources[0].kind == "file"

    raw["schema_version"] = 1
    raw.pop("source_environment_preflight")
    raw["environment"].pop("declared_resources")
    manifest_path.write_text(json.dumps(raw))

    manifest = load_fork_manifest(manifest_path)

    assert manifest.schema_version == 1
    assert manifest.source_environment_preflight == "MATCHED"
    assert manifest.environment is not None
    assert manifest.environment.declared_resources == ()


def test_prefix_manifest_carries_gap_and_external_effect_limits(
    repo: Path, codex_home: Path
) -> None:
    sha = snapshot_worktree(repo)
    _journal(
        OLD_ID,
        [
            (TraceEvent("observation_gap", {"reason": "disconnect"}), None),
            (
                TraceEvent(
                    "external_effect",
                    {"kind": "git_remote_write", "resource": "origin", "reversible": False},
                ),
                None,
            ),
            (
                TraceEvent(
                    "tool_proposal",
                    {"tool_use_id": "call_B", "cwd": str(repo)},
                    event_id="event-B",
                    connection_epoch=3,
                ),
                sha,
            ),
        ],
    )

    plan = fork(OLD_ID, 2, codex_home=codex_home)
    manifest = load_fork_manifest(Path(plan.manifest or ""))

    assert manifest.prefix.source_event_id == "event-B"
    assert manifest.prefix.connection_epoch == 3
    assert manifest.prefix.observation_gaps == 1
    assert manifest.prefix.external_effects == (
        {"kind": "git_remote_write", "resource": "origin", "reversible": False},
    )
    assert plan.external_effects == list(manifest.prefix.external_effects)
