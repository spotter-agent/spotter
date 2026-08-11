import json
import shlex
import subprocess
from pathlib import Path

import pytest

from spotter.hook import journal_path
from spotter.replay import ReplayError, fork, fork_rollout
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
        {"ordinal": 0, "type": "session_meta", "payload": {"session_id": OLD_ID, "id": OLD_ID}},
        {"ordinal": 1, "type": "response_item", "payload": {"call_id": "call_A", "name": "exec"}},
        {"ordinal": 2, "type": "response_item", "payload": {"call_id": "call_A", "output": "ok"}},
        {"ordinal": 3, "type": "response_item", "payload": {"call_id": "call_B", "name": "exec"}},
    ]
    rollout = day / f"rollout-2026-08-11T10-00-00-{OLD_ID}.jsonl"
    rollout.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return home


def _journal(session: str, records: list[tuple[TraceEvent, str | None]]) -> None:
    journal = StepJournal(journal_path({"session_id": session}))
    for event, snapshot in records:
        journal.record(event, snapshot=snapshot)


def test_fork_rollout_truncates_and_renames(codex_home: Path) -> None:
    rollout = next((codex_home / "sessions").rglob("*.jsonl"))
    forked = fork_rollout(rollout, "call_B", "new-id-1234")
    lines = forked.read_text().splitlines()
    assert len(lines) == 3  # cut strictly before call_B
    assert OLD_ID not in forked.name and "new-id-1234" in forked.name
    assert all(OLD_ID not in line for line in lines)
    assert rollout.read_text().count("call_B") == 1  # original untouched


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
    repo: Path, codex_home: Path, monkeypatch: pytest.MonkeyPatch
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
