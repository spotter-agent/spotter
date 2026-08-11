import json
from pathlib import Path

import pytest

from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.hook import event_from_hook, journal_path, run_hook
from spotter.snapshot import StepJournal


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


def test_unknown_events_still_journal(spotter_home: Path) -> None:
    payload = {"hook_event_name": "SessionStart", "session_id": "s2"}
    assert run_hook(payload, _config(observation_only=True)) is None
    records = StepJournal.load(journal_path(payload))
    assert records[0].event.kind == "sessionstart"
