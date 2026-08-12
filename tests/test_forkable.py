"""Early steps must be forkable, or the instrument cannot reach the phase the
project's thesis is about (#43)."""

import subprocess
from pathlib import Path

import pytest

from spotter.cli import _forkable_of
from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.hook import journal_path, run_hook
from spotter.snapshot import StepJournal


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "spotter"))
    return tmp_path / "spotter"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(command, cwd=repo, check=True)
    (repo / "a.txt").write_text("v1")
    return repo


def _config(**kwargs: object) -> SpotterConfig:
    return SpotterConfig(MainAgentConfig("codex"), ReviewerConfig(), GatesConfig(), **kwargs)  # type: ignore[arg-type]


def _proposal(session: str, repo: Path, n: int, tool: str = "Bash") -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "cwd": str(repo),
        "tool_name": tool,
        "tool_use_id": f"call-{n}",
        "tool_input": {"command": f"sed -n '1,5p' file{n}"},
    }


def _refs(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/spotter/steps"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()


def test_the_first_exploration_step_is_forkable(repo: Path) -> None:
    """Before this, nothing was snapshotted until the first apply_patch, so the
    whole exploration phase — where the weak hypothesis forms — could not be
    branched."""
    config = _config()
    for n in range(3):
        run_hook(_proposal("early", repo, n), config)

    records = StepJournal.load(journal_path({"session_id": "early"}))
    assert records[0].snapshot, "the session's first step had no restorable state"
    assert _forkable_of(records) == "forkable=3/3"


def test_exploration_costs_one_ref_not_one_per_step(repo: Path) -> None:
    """The tree does not change while reading, so dedup should make this free
    after the first snapshot."""
    config = _config()
    for n in range(5):
        run_hook(_proposal("cheap", repo, n), config)
    assert len(_refs(repo)) == 1


def test_a_later_patch_still_snapshots(repo: Path) -> None:
    config = _config()
    run_hook(_proposal("mixed", repo, 0), config)
    (repo / "a.txt").write_text("changed")
    patch = _proposal("mixed", repo, 1, tool="apply_patch")
    patch["tool_input"] = {"command": "*** Begin Patch\n*** Update File: a.txt\n*** End Patch"}
    run_hook(patch, config)
    assert len(_refs(repo)) == 2  # start state and post-edit state are distinct


def test_a_non_git_directory_is_probed_once_not_per_call(tmp_path: Path) -> None:
    """Keying on 'no snapshot yet' would retry git on every tool call in a
    directory where it can never succeed."""
    plain = tmp_path / "plain"
    plain.mkdir()
    calls: list[object] = []
    real = subprocess.run

    def counting(command: list[str], **kwargs: object) -> object:
        if command and command[0] == "git":
            calls.append(command)
        return real(command, **kwargs)  # type: ignore[call-overload]

    config = _config()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("spotter.snapshot.subprocess.run", counting)
    try:
        for n in range(4):
            run_hook(_proposal("nogit", plain, n), config)
    finally:
        monkeypatch.undo()

    assert len(calls) <= 1, f"git was invoked {len(calls)} times in a non-repository"


def test_the_start_snapshot_can_be_turned_off(repo: Path) -> None:
    config = _config(snapshot_at_start=False)
    run_hook(_proposal("off", repo, 0), config)
    assert _refs(repo) == []


def test_forkable_ratio_reports_the_instrument_reach() -> None:
    from spotter.snapshot import StepRecord
    from spotter.trace import TraceEvent

    def proposal(step: int, snapshot: str | None) -> StepRecord:
        return StepRecord(step, TraceEvent("tool_proposal", {"tool_use_id": f"c{step}"}), snapshot)

    # a snapshot arriving only at step 2 leaves the first two unreachable
    records = [proposal(0, None), proposal(1, None), proposal(2, "abc"), proposal(3, None)]
    assert _forkable_of(records) == "forkable=2/4"
    assert _forkable_of([]) == "forkable=0/0"


def test_proposals_without_a_correlation_id_are_not_counted_forkable() -> None:
    from spotter.snapshot import StepRecord
    from spotter.trace import TraceEvent

    records = [StepRecord(0, TraceEvent("tool_proposal", {}), "abc")]
    assert _forkable_of(records) == "forkable=0/1"
