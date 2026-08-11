import json
from pathlib import Path

import pytest

from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.hook import event_from_hook, journal_path, run_hook
from spotter.snapshot import StepJournal, restore_snapshot


@pytest.fixture(autouse=True)
def spotter_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path))
    return tmp_path


def _payload(command: str) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "cwd": "/repo",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def _config(observation_only: bool) -> SpotterConfig:
    return SpotterConfig(
        MainAgentConfig("codex"),
        ReviewerConfig(),
        GatesConfig(),
        observation_only=observation_only,
    )


def test_active_mode_emits_deny_json() -> None:
    output = run_hook(_payload("rm -rf /"), _config(observation_only=False))
    assert output is not None
    decision = json.loads(output)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "rm_root" in decision["permissionDecisionReason"]


def test_shadow_mode_allows_but_journals_the_block(spotter_home: Path) -> None:
    payload = _payload("git push --force")
    assert run_hook(payload, _config(observation_only=True)) is None
    records = StepJournal.load(journal_path(payload))
    assert [r.event.kind for r in records] == ["tool_proposal", "gate_shadow_block"]
    assert records[1].event.payload["rule"] == "git_push_force"


def test_safe_command_allows_silently() -> None:
    assert run_hook(_payload("pytest tests/"), _config(observation_only=False)) is None


def test_session_id_is_sanitized_for_filenames() -> None:
    path = journal_path({"session_id": "../../etc/passwd"})
    assert path.name == "______etc_passwd.jsonl"  # no separators, no dots


def test_file_paths_extracted_from_tool_input() -> None:
    event = event_from_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"path": "src/a.py", "files": ["src/b.py"]},
        }
    )
    assert event.payload["files"] == ["src/a.py", "src/b.py"]


def test_apply_patch_paths_are_gated() -> None:
    payload = {
        **_payload("*** Begin Patch\n*** Update File: pyproject.toml\n*** End Patch"),
        "tool_name": "apply_patch",
    }
    config = SpotterConfig(
        MainAgentConfig("codex"),
        ReviewerConfig(),
        GatesConfig(block_dependency_changes=True),
        observation_only=False,
    )
    assert "dependency_change" in (run_hook(payload, config) or "")

    payload["tool_input"] = {
        "command": (
            "*** Begin Patch\n*** Update File: src/key\n*** Move to: secrets/key\n*** End Patch"
        )
    }
    forbidden = SpotterConfig(
        MainAgentConfig("codex"),
        ReviewerConfig(),
        GatesConfig(forbidden_paths=("secrets/*",)),
        observation_only=False,
    )
    assert "forbidden_path" in (run_hook(payload, forbidden) or "")


def test_post_tool_use_preserves_evidence() -> None:
    event = event_from_hook(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_use_id": "call-1",
            "tool_input": {"command": "pytest"},
            "tool_response": {"exit_code": 1, "output": "failed"},
        }
    )
    assert event.payload["tool_use_id"] == "call-1"
    assert event.payload["tool_response"] == {"exit_code": 1, "output": "failed"}


def test_unknown_events_still_journal(spotter_home: Path) -> None:
    payload = {"hook_event_name": "SessionStart", "session_id": "s2"}
    assert run_hook(payload, _config(observation_only=True)) is None
    records = StepJournal.load(journal_path(payload))
    assert records[0].event.kind == "sessionstart"


def test_apply_patch_takes_snapshot_for_fork(tmp_path: Path, spotter_home: Path) -> None:
    import subprocess

    repo = tmp_path / "hookrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "x.txt").write_text("x")

    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "snap1",
        "cwd": str(repo),
        "tool_name": "apply_patch",
        "tool_use_id": "call_1",
        "tool_input": {"command": "*** Begin Patch\n*** Update File: x.txt\n*** End Patch"},
    }
    assert run_hook(payload, _config(observation_only=True)) is None
    record = StepJournal.load(journal_path(payload))[0]
    assert record.snapshot  # repo state pinned at the commit boundary
    assert record.event.payload["tool_use_id"] == "call_1"
    assert record.event.payload["cwd"] == str(repo)

    (repo / "x.txt").write_text("patched")
    post = {**payload, "hook_event_name": "PostToolUse", "tool_response": {"ok": True}}
    assert run_hook(post, _config(observation_only=True)) is None
    post_record = StepJournal.load(journal_path(payload))[1]
    assert post_record.snapshot and post_record.snapshot != record.snapshot

    restored = tmp_path / "restored"
    restore_snapshot(repo, post_record.snapshot, restored)
    assert (restored / "x.txt").read_text() == "patched"


def test_snapshot_failure_fails_open(tmp_path: Path, spotter_home: Path) -> None:
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "snap2",
        "cwd": str(tmp_path / "not-a-git-repo"),
        "tool_name": "apply_patch",
        "tool_use_id": "call_1",
        "tool_input": {"command": "*** Begin Patch\n*** End Patch"},
    }
    assert run_hook(payload, _config(observation_only=True)) is None  # session unharmed
    assert StepJournal.load(journal_path(payload))[0].snapshot is None
