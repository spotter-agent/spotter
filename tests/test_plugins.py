import json
import os
import subprocess
from pathlib import Path

from spotter.snapshot import StepJournal


def test_plugin_manifests_and_hooks_are_well_formed() -> None:
    codex = json.loads(Path(".codex-plugin/plugin.json").read_text())
    claude = json.loads(Path(".claude-plugin/plugin.json").read_text())
    claude_marketplace = json.loads(Path(".claude-plugin/marketplace.json").read_text())
    codex_marketplace = json.loads(Path(".agents/plugins/marketplace.json").read_text())
    hooks = json.loads(Path("hooks/hooks.json").read_text())["hooks"]

    assert codex["name"] == claude["name"] == "spotter"
    assert codex["version"] == claude["version"]
    assert claude_marketplace["plugins"][0]["source"] == "./"
    assert codex_marketplace["plugins"][0]["source"]["path"] == "./plugins/spotter"
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}


def test_bundled_hook_runs_without_installing_package(tmp_path: Path) -> None:
    home = tmp_path / "home"
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "plugin-test",
        "cwd": str(tmp_path),
        "tool_name": "Bash",
        "tool_input": {"command": "printf safe"},
    }
    result = subprocess.run(
        [str(Path("scripts/spotter-hook").resolve())],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env={**os.environ, "SPOTTER_HOME": str(home)},
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    records = StepJournal.load(home / "sessions" / "plugin-test.jsonl")
    assert records[0].event.kind == "tool_proposal"
