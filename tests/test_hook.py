import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.daemon import DaemonClient, DaemonProtocolError, DaemonServer, DaemonTimeout
from spotter.hook import event_from_hook, journal_path, run_hook
from spotter.replay import fork
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


@pytest.fixture()
def daemon() -> Iterator[None]:
    ready = threading.Event()
    thread_error: list[BaseException] = []

    def run() -> None:
        async def serve() -> None:
            server = DaemonServer()
            await server.start()
            ready.set()
            try:
                await server.wait_for_shutdown()
            finally:
                await server.close()

        try:
            import asyncio

            asyncio.run(serve())
        except BaseException as error:
            thread_error.append(error)
            ready.set()

    worker = threading.Thread(target=run)
    worker.start()
    assert ready.wait(1)
    assert not thread_error
    try:
        yield
    finally:
        import asyncio

        asyncio.run(DaemonClient().shutdown())
        worker.join(1)
        assert not worker.is_alive()
        assert not thread_error


def test_active_mode_emits_deny_json(daemon: None) -> None:
    output = run_hook(_payload("rm -rf /"), _config(observation_only=False))
    assert output is not None
    decision = json.loads(output)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "rm_root" in decision["permissionDecisionReason"]


def test_shadow_mode_allows_but_journals_the_block(spotter_home: Path, daemon: None) -> None:
    payload = _payload("git push --force")
    assert run_hook(payload, _config(observation_only=True)) is None
    records = StepJournal.load(journal_path(payload))
    assert [r.event.kind for r in records] == [
        "tool_proposal",
        "gate_shadow_block",
        "gate_ipc",
    ]
    assert records[1].event.payload["rule"] == "git_push_force"
    assert records[2].event.payload["status"] == "ok"
    assert records[2].event.payload["ipc_ms"] >= 0
    assert records[2].event.payload["hook_ms"] >= records[2].event.payload["ipc_ms"]
    sample = records[2].event.payload["runtime_sample"]
    assert sample["sample_seq"] == 1
    assert sample["cpu_seconds"] >= 0
    assert sample["peak_rss_bytes"] >= 0


def test_capture_only_mode_never_enforces_or_calls_the_daemon(
    spotter_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_gate(*args: object, **kwargs: object) -> object:
        raise AssertionError("capture-only hooks must not call spotterd")

    monkeypatch.setattr("spotter.hook.DaemonClient.gate", unexpected_gate)
    monkeypatch.setenv("SPOTTER_CAPTURE_ONLY", "1")
    payload = _payload("rm -rf /")
    config = SpotterConfig(
        MainAgentConfig("codex"),
        ReviewerConfig(every_steps=1, on_signals=True),
        observation_only=False,
    )

    assert run_hook(payload, config) is None

    records = StepJournal.load(journal_path(payload))
    assert [record.event.kind for record in records] == [
        "tool_proposal",
        "gate_shadow_block",
        "gate_ipc",
    ]
    assert records[2].event.payload["status"] == "capture_only"


def test_safe_command_allows_silently(daemon: None) -> None:
    assert run_hook(_payload("pytest tests/"), _config(observation_only=False)) is None


def test_missing_daemon_uses_local_gate_with_telemetry() -> None:
    payload = _payload("rm -rf /")
    assert "rm_root" in (run_hook(payload, _config(observation_only=False)) or "")

    records = StepJournal.load(journal_path(payload))
    assert [record.event.kind for record in records] == [
        "tool_proposal",
        "gate_block",
        "gate_ipc",
    ]
    assert records[1].event.payload["rule"] == "rm_root"
    assert records[2].event.payload["status"] == "unavailable"


def test_timeout_uses_local_gate_with_diagnosable_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def timeout(*args: object, **kwargs: object) -> object:
        raise DaemonTimeout("deadline")

    monkeypatch.setattr("spotter.hook.DaemonClient.gate", timeout)
    payload = _payload("rm -rf /")
    assert "rm_root" in (run_hook(payload, _config(observation_only=False)) or "")

    records = StepJournal.load(journal_path(payload))
    assert records[1].event.payload["rule"] == "rm_root"
    assert records[2].event.payload["status"] == "timeout"


def test_protocol_error_fails_open_with_diagnosable_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def protocol_error(*args: object, **kwargs: object) -> object:
        raise DaemonProtocolError("incompatible control protocol")

    monkeypatch.setattr("spotter.hook.DaemonClient.gate", protocol_error)
    payload = _payload("rm -rf /")
    assert run_hook(payload, _config(observation_only=False)) is None

    records = StepJournal.load(journal_path(payload))
    assert records[1].event.payload["rule"] == "daemon_protocol_error"
    assert records[2].event.payload["status"] == "protocol_error"


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


def test_apply_patch_paths_are_gated(daemon: None) -> None:
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
    assert event.payload["reversibility_class"] == "A"


def test_git_push_records_class_and_external_effect(spotter_home: Path) -> None:
    proposal = {**_payload("git push origin feature"), "tool_use_id": "push-1"}
    assert run_hook(proposal, _config(observation_only=True)) is None
    result = {
        **proposal,
        "hook_event_name": "PostToolUse",
        "tool_response": {"exit_code": 0},
    }
    assert run_hook(result, _config(observation_only=True)) is None

    records = StepJournal.load(journal_path(proposal))
    assert records[0].event.payload["reversibility_class"] == "C"
    effect = records[-1].event
    assert effect.kind == "external_effect"
    assert effect.payload["resource"] == "origin"
    assert effect.payload["result"] == "succeeded"
    assert effect.payload["tool_use_id"] == "push-1"


def test_unknown_events_still_journal(spotter_home: Path) -> None:
    payload = {"hook_event_name": "SessionStart", "session_id": "s2"}
    assert run_hook(payload, _config(observation_only=True)) is None
    records = StepJournal.load(journal_path(payload))
    assert records[0].event.kind == "sessionstart"
    assert records[0].event.identity is not None
    assert records[0].event.identity.thread_id is None
    assert records[0].event.identity.provenance.legacy_session_id == "s2"
    assert records[0].event.provenance is not None
    assert records[0].event.provenance.source == "codex_hook"


def test_session_start_takes_baseline_snapshot_for_early_forks(
    tmp_path: Path, spotter_home: Path
) -> None:
    import subprocess

    repo = tmp_path / "baseline-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "x.txt").write_text("baseline")
    payload = {"hook_event_name": "SessionStart", "session_id": "baseline", "cwd": str(repo)}

    assert run_hook(payload, _config(observation_only=True)) is None

    record = StepJournal.load(journal_path(payload))[0]
    assert record.event.kind == "sessionstart"
    assert record.snapshot
    restored = tmp_path / "baseline-restored"
    restore_snapshot(repo, record.snapshot, restored)
    assert (restored / "x.txt").read_text() == "baseline"

    proposal = {
        "hook_event_name": "PreToolUse",
        "session_id": "baseline",
        "cwd": str(repo),
        "tool_name": "Bash",
        "tool_use_id": "early-call",
        "tool_input": {"command": "sed -n '1,20p' x.txt"},
    }
    assert run_hook(proposal, _config(observation_only=True)) is None
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "rollout-baseline.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "baseline", "id": "baseline"},
                    }
                ),
                json.dumps({"type": "response_item", "payload": {"call_id": "early-call"}}),
            ]
        )
        + "\n"
    )
    plan = fork("baseline", 1, codex_home=codex_home)
    assert Path(plan.worktree, "x.txt").read_text() == "baseline"


def test_session_start_snapshot_failure_fails_open(tmp_path: Path, spotter_home: Path) -> None:
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "baseline-failure",
        "cwd": str(tmp_path / "not-a-repo"),
    }

    assert run_hook(payload, _config(observation_only=True)) is None
    assert StepJournal.load(journal_path(payload))[0].snapshot is None


def test_apply_patch_takes_snapshot_for_fork(
    tmp_path: Path, spotter_home: Path, daemon: None
) -> None:
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
    post_record = next(
        record
        for record in StepJournal.load(journal_path(payload))
        if record.event.kind == "tool_result"
    )
    assert post_record.snapshot and post_record.snapshot != record.snapshot

    restored = tmp_path / "restored"
    restore_snapshot(repo, post_record.snapshot, restored)
    assert (restored / "x.txt").read_text() == "patched"


def test_snapshot_failure_fails_open(tmp_path: Path, spotter_home: Path, daemon: None) -> None:
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
