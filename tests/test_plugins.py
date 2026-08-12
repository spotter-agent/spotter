import json
import os
import subprocess
import sys
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
    assert claude_marketplace["name"] == codex_marketplace["name"] == "spotter"
    assert claude_marketplace["plugins"][0]["source"] == "./"
    assert codex_marketplace["plugins"][0]["source"]["path"] == "./plugins/spotter"
    source_path = codex_marketplace["plugins"][0]["source"]["path"]
    assert Path(source_path).resolve() == Path.cwd().resolve()
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"}


def test_readme_uses_remote_marketplace_install_commands() -> None:
    readme = Path("README.md").read_text()

    assert "codex plugin marketplace add bogyie/spotter" in readme
    assert "codex plugin add spotter@spotter" in readme
    assert "claude plugin marketplace add bogyie/spotter" in readme
    assert "claude plugin install spotter@spotter" in readme


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


def _fake_bin(tmp_path: Path, name: str, body: str) -> Path:
    """A directory containing one executable, for building a hermetic PATH."""
    bin_dir = tmp_path / f"bin-{name}"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / name
    script.write_text(body)
    script.chmod(0o755)
    return bin_dir


def test_wrapper_fails_open_when_no_usable_python_exists(tmp_path: Path) -> None:
    """The system python3 on macOS is 3.9 and cannot import tomllib, so calling
    it unqualified failed every hook invocation — with exit 1 and no journal,
    which is the one outcome a supervisor must never produce silently."""
    unusable = _fake_bin(tmp_path, "python3", "#!/bin/sh\nexit 1\n")
    home = tmp_path / "home"
    result = subprocess.run(
        [str(Path("scripts/spotter-hook").resolve())],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "no-python",
                "cwd": str(tmp_path),
                "tool_name": "Bash",
                "tool_input": {"command": "true"},
            }
        ),
        text=True,
        capture_output=True,
        env={"PATH": str(unusable), "HOME": str(home), "SPOTTER_HOME": str(home)},
        check=False,
    )

    assert result.returncode == 0, "a supervisor that cannot start must not fail the session"
    assert "NOT being supervised" in result.stderr
    assert "SPOTTER_PYTHON" in result.stderr  # says how to fix it
    assert not (home / "sessions").exists()


def test_wrapper_runs_with_an_empty_path(tmp_path: Path) -> None:
    """Not even dirname is guaranteed to be present; this script exists because
    such assumptions fail quietly."""
    home = tmp_path / "home"
    result = subprocess.run(
        [str(Path("scripts/spotter-hook").resolve())],
        input="{}",
        text=True,
        capture_output=True,
        env={"PATH": str(tmp_path / "empty"), "HOME": str(home), "SPOTTER_HOME": str(home)},
        check=False,
    )
    assert result.returncode == 0


def test_wrapper_finds_a_usable_python_by_explicit_override(tmp_path: Path) -> None:
    unusable = _fake_bin(tmp_path, "python3", "#!/bin/sh\nexit 1\n")
    override = tmp_path / "python with spaces"
    override.symlink_to(sys.executable)
    home = tmp_path / "home"
    result = subprocess.run(
        [str(Path("scripts/spotter-hook").resolve())],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "override",
                "cwd": str(tmp_path),
                "tool_name": "Bash",
                "tool_input": {"command": "true"},
            }
        ),
        text=True,
        capture_output=True,
        env={
            "PATH": str(unusable),
            "HOME": str(home),
            "SPOTTER_HOME": str(home),
            "SPOTTER_PYTHON": str(override),
        },
        check=False,
    )
    assert result.returncode == 0
    assert (home / "sessions" / "override.jsonl").exists()


def test_wrapper_keeps_observing_when_the_config_is_invalid(tmp_path: Path) -> None:
    """The bad config is handled inside the hook rather than by the wrapper's
    catch-all, so supervision degrades to defaults instead of stopping."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "spotter.toml").write_text("not valid toml")

    result = subprocess.run(
        [str(Path("scripts/spotter-hook").resolve())],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "bad-toml",
                "cwd": str(tmp_path),
                "tool_name": "Bash",
                "tool_input": {"command": "true"},
            }
        ),
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home), "SPOTTER_HOME": str(home)},
        check=False,
    )

    assert result.returncode == 0
    assert "using defaults" in result.stderr
    assert StepJournal.load(home / "sessions" / "bad-toml.jsonl")


def test_wrapper_converts_an_unexpected_crash_into_fail_open(tmp_path: Path) -> None:
    """Nothing in the hook path should exit non-zero any more, which is exactly
    why the wrapper's catch-all must stay: it covers the failures nobody
    predicted, such as an interpreter that dies on import."""
    crasher = tmp_path / "bin-crash"
    crasher.mkdir()
    fake = crasher / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        "  [ \"$arg\" = '-m' ] && exit 3\n"
        "done\n"
        "exit 0\n"  # the capability probe succeeds
    )
    fake.chmod(0o755)
    home = tmp_path / "home"

    result = subprocess.run(
        [str(Path("scripts/spotter-hook").resolve())],
        input="{}",
        text=True,
        capture_output=True,
        env={"PATH": str(crasher), "HOME": str(home), "SPOTTER_HOME": str(home)},
        check=False,
    )

    assert result.returncode == 0
    assert "allowed unsupervised" in result.stderr


def test_wrapper_reads_the_documented_home_config(tmp_path: Path) -> None:
    """An installed plugin used to ignore ~/.spotter/spotter.toml, so the gates
    a user had configured were silently absent."""
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    (home / "spotter.toml").write_text(
        'observation_only = false\n[main_agent]\nadapter = "codex"\n'
        '[gates]\nforbidden_paths = ["secrets/*"]\n'
    )
    result = subprocess.run(
        [str(Path("scripts/spotter-hook").resolve())],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "cfg",
                "cwd": str(tmp_path),
                "tool_name": "apply_patch",
                "tool_input": {"path": "secrets/key.pem"},
            }
        ),
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home), "SPOTTER_HOME": str(home)},
        check=False,
    )
    assert result.returncode == 0
    assert "forbidden_path" in result.stdout, "the configured gate never reached the hook"


def test_hook_command_uses_the_runtime_plugin_root_variable() -> None:
    """Codex and Claude Code expand CLAUDE_PLUGIN_ROOT; PLUGIN_ROOT is the
    Copilot spelling, and an unexpanded variable makes the command an absolute
    path that does not exist."""
    hooks = json.loads(Path("hooks/hooks.json").read_text())
    commands = [
        entry["command"]
        for event in hooks["hooks"].values()
        for matcher in event
        for entry in matcher["hooks"]
    ]
    assert commands
    for command in commands:
        assert "${CLAUDE_PLUGIN_ROOT" in command


def test_a_malformed_config_never_blocks_the_session(tmp_path: Path) -> None:
    """A PreToolUse hook exits 2 to mean *deny*, and argparse.error() exits 2.
    Loading config outside the fail-open boundary therefore turned a typo in
    spotter.toml into a block on every tool call."""
    home = tmp_path / "home"
    home.mkdir()
    broken = home / "spotter.toml"
    broken.write_bytes(b"\xff")

    result = subprocess.run(
        [sys.executable, "-m", "spotter", "hook", "--config", str(broken)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "broken-config",
                "cwd": str(tmp_path),
                "tool_name": "Bash",
                "tool_input": {"command": "true"},
            }
        ),
        text=True,
        capture_output=True,
        env={**os.environ, "SPOTTER_HOME": str(home), "PYTHONPATH": "src"},
        check=False,
    )

    assert result.returncode == 0, "a bad config denied the tool call"
    assert result.stdout == ""  # no deny decision emitted either
    assert "using defaults" in result.stderr
    # supervision continues on defaults rather than stopping
    assert StepJournal.load(home / "sessions" / "broken-config.jsonl")


def test_a_missing_config_also_fails_open(tmp_path: Path) -> None:
    home = tmp_path / "home"
    result = subprocess.run(
        [sys.executable, "-m", "spotter", "hook", "--config", str(tmp_path / "absent.toml")],
        input=json.dumps({"hook_event_name": "SessionStart", "session_id": "absent"}),
        text=True,
        capture_output=True,
        env={**os.environ, "SPOTTER_HOME": str(home), "PYTHONPATH": "src"},
        check=False,
    )
    assert result.returncode == 0
    assert "using defaults" in result.stderr


def test_other_commands_still_reject_a_bad_config(tmp_path: Path) -> None:
    """Only the hook fails open. Everything else should still refuse to run on
    a config it cannot parse, rather than silently using different settings."""
    broken = tmp_path / "spotter.toml"
    broken.write_text("nope = = =\n")
    result = subprocess.run(
        [sys.executable, "-m", "spotter", "observe", "--config", str(broken)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "src"},
        check=False,
    )
    assert result.returncode == 2
