"""Lock scope and hook latency (#49)."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from spotter.hook import SLOW_HOOK_MS, journal_path, run_hook
from spotter.paths import spotter_home
from spotter.snapshot import StepJournal, repo_lock
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "spotter"))
    return tmp_path / "spotter"


def _hold_script() -> str:
    return (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "from spotter.snapshot import repo_lock\n"
        "with repo_lock(Path(sys.argv[1])):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(float(sys.argv[2]))\n"
    )


def _holder(repo: Path, seconds: float, home: Path) -> subprocess.Popen[str]:
    worker = subprocess.Popen(
        [sys.executable, "-c", _hold_script(), str(repo), str(seconds)],
        cwd=Path(__file__).parent.parent,
        stdout=subprocess.PIPE,
        text=True,
        env={"PYTHONPATH": "src", "PATH": os.environ["PATH"], "SPOTTER_HOME": str(home)},
    )
    assert worker.stdout is not None
    worker.stdout.readline()  # wait until the lock is actually held
    return worker


def test_different_repositories_do_not_block_each_other(tmp_path: Path, home: Path) -> None:
    """A patch in one repository used to stall the PreToolUse hook of an
    unrelated session, because the lock protecting per-repository refs was
    global."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    holder = _holder(a, 2.0, home)
    try:
        started = time.perf_counter()
        with repo_lock(b):
            waited = time.perf_counter() - started
    finally:
        holder.wait(timeout=10)
    assert waited < 0.5, f"an unrelated repository blocked for {waited:.2f}s"


def test_the_same_repository_still_serializes(tmp_path: Path, home: Path) -> None:
    """The invariant the lock exists for is per repository, so within one it
    must still hold."""
    repo = tmp_path / "same"
    repo.mkdir()
    holder = _holder(repo, 1.0, home)
    try:
        started = time.perf_counter()
        with repo_lock(repo):
            waited = time.perf_counter() - started
    finally:
        holder.wait(timeout=10)
    assert waited > 0.4, "two writers held the same repository lock at once"


def test_lock_key_is_stable_across_path_spellings(tmp_path: Path, home: Path) -> None:
    repo = tmp_path / "spelled"
    repo.mkdir()
    with repo_lock(repo):
        pass
    keys = {p.name for p in (home / "locks").glob("*.lock")}
    with repo_lock(Path(str(repo) + "/./")):
        pass
    assert {p.name for p in (home / "locks").glob("*.lock")} == keys


def test_lock_directory_is_owner_only(tmp_path: Path, home: Path) -> None:
    repo = tmp_path / "perm"
    repo.mkdir()
    with repo_lock(repo):
        pass
    assert ((home / "locks").stat().st_mode & 0o077) == 0


# --- hook latency ------------------------------------------------------------


def _payload(session: str, cwd: Path) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "cwd": str(cwd),
        "tool_name": "Bash",
        "tool_use_id": "t1",
        "tool_input": {"command": "true"},
    }


def test_a_slow_hook_leaves_a_record(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook killed at the runtime's timeout fails open, so supervision stops
    for exactly the calls that mutate files. The slow ones must be visible
    before that happens."""
    from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig

    real = time.perf_counter
    calls = iter([0.0, (SLOW_HOOK_MS + 500) / 1000])
    monkeypatch.setattr(time, "perf_counter", lambda: next(calls, real()))

    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig(), GatesConfig())
    run_hook(_payload("slow", tmp_path), config)

    records = StepJournal.load(journal_path({"session_id": "slow"}))
    slow = [r for r in records if r.event.kind == "hook_slow"]
    assert slow, "a hook well over the threshold left no trace"
    assert float(slow[0].event.payload["elapsed_ms"]) >= SLOW_HOOK_MS


def test_a_fast_hook_records_nothing_extra(tmp_path: Path, home: Path) -> None:
    from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig

    config = SpotterConfig(MainAgentConfig("codex"), ReviewerConfig(), GatesConfig())
    run_hook(_payload("fast", tmp_path), config)
    kinds = [r.event.kind for r in StepJournal.load(journal_path({"session_id": "fast"}))]
    assert "hook_slow" not in kinds


def test_analyze_surfaces_slow_hooks(home: Path) -> None:
    from spotter.cli import _slow_of

    journal = StepJournal(journal_path({"session_id": "s"}))
    journal.record(TraceEvent("hook_slow", {"kind": "tool_proposal", "elapsed_ms": 1500.0}))
    journal.record(TraceEvent("hook_slow", {"kind": "tool_result", "elapsed_ms": 2400.0}))
    line = _slow_of([r for r in StepJournal.load(journal.path) if r.event.kind == "hook_slow"])
    assert "slow_hooks=2" in line and "2400ms" in line
    assert _slow_of([]) == ""


def test_spotter_home_override_isolates_locks(tmp_path: Path) -> None:
    """Tests and concurrent installs must not contend on a shared path."""
    repo = tmp_path / "iso"
    repo.mkdir()
    other = tmp_path / "elsewhere"
    with repo_lock(repo, spotter_home_override=other):
        assert (other / "locks").exists()
    assert not (spotter_home() / "locks").exists()
