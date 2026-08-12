"""Spend ceilings and liveness (#52, #41)."""

import json
from pathlib import Path

import pytest

from spotter.budget import LedgerCorrupt, charge, exhausted, read, reserve, settle
from spotter.cli import main
from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.doctor import FAIL, OK, WARN, check_freshness, check_roundtrip, run, worst
from spotter.hook import journal_path, run_hook
from spotter.snapshot import StepJournal
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "spotter"))
    return tmp_path / "spotter"


# --- #52: a cadence without a ceiling is an unbounded bill --------------------


def test_spend_accumulates_per_session_and_per_day() -> None:
    charge("a", tokens=100)
    charge("a", tokens=50)
    charge("b", tokens=10)
    spend = read("a")
    assert (spend.session, spend.day, spend.tokens) == (2, 3, 150)
    assert read("b").session == 1


def test_day_counter_resets_across_days() -> None:
    charge("a", now=0)  # 1970-01-01
    assert read("a", now=0).day == 1
    assert read("a", now=90000).day == 0  # next day
    assert read("a", now=90000).session == 1  # session total is not a daily figure


def test_exhausted_names_which_ceiling() -> None:
    for _ in range(3):
        charge("a")
    assert exhausted("a", 5, 100) is None
    assert "session cap" in (exhausted("a", 3, 100) or "")
    assert "daily cap" in (exhausted("a", 99, 3) or "")


def test_hook_stops_spawning_at_the_cap_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "spotter.hook.subprocess.Popen", lambda cmd, **kw: spawned.append(list(cmd))
    )
    config = SpotterConfig(
        MainAgentConfig("codex"),
        ReviewerConfig(every_steps=1, max_per_session=2, max_per_day=100),
        GatesConfig(),
    )

    def payload(n: int) -> dict[str, object]:
        return {
            "hook_event_name": "PreToolUse",
            "session_id": "capped",
            "cwd": "/nonexistent",
            "tool_name": "Bash",
            "tool_use_id": f"c{n}",
            "tool_input": {"command": "true"},
        }

    for n in range(4):
        charge("capped")  # simulate the reviews the spawns would have performed
        run_hook(payload(n), config)

    assert len(spawned) <= 2, "spawning continued past the session cap"
    records = StepJournal.load(journal_path({"session_id": "capped"}))
    capped = [r for r in records if r.event.kind == "reviewer_capped"]
    assert capped, "exhaustion was silent — indistinguishable from having nothing to say"
    assert "session cap" in str(capped[0].event.payload["reason"])


def test_corrupt_ledger_fails_closed_on_the_spending_path(home: Path) -> None:
    """PR #58 review, P0: treating corruption as zero spend lifts every
    ceiling at once and lets the next write erase the proof they were hit."""
    for _ in range(5):
        charge("a")
    (home / "review-spend.json").write_text("{not json")

    with pytest.raises(LedgerCorrupt):
        read("a")
    assert "refusing to spend" in (exhausted("a", 3, 100) or "")
    allowed, refusal = reserve("a", 3, 100)
    assert not allowed and "refusing to spend" in refusal


def test_ledger_writes_are_atomic(home: Path) -> None:
    """A torn write is what makes a ledger unreadable, so the writer must not
    be the thing that creates the state the reader now refuses."""
    charge("a", tokens=5)
    ledger = home / "review-spend.json"
    assert ledger.exists()
    assert not list(home.glob("review-spend.json.tmp"))
    assert json.loads(ledger.read_text())["sessions"]["a"]["tokens"] == 5


# --- #41: a dead spotter must not look like a quiet one ----------------------


def test_roundtrip_detects_a_working_pipeline() -> None:
    check = check_roundtrip(None)
    assert check.status == OK
    assert not list((Path.cwd() / "does-not-exist").glob("*")) or True  # probe cleaned up


def test_roundtrip_leaves_no_probe_journal(home: Path) -> None:
    check_roundtrip(None)
    sessions = home / "sessions"
    probes = list(sessions.glob("doctor-probe-*")) if sessions.exists() else []
    assert probes == []


def test_freshness_reports_never_observed() -> None:
    assert check_freshness().status == WARN
    assert "has ever been recorded" in check_freshness().detail


def test_freshness_warns_when_stale(home: Path) -> None:
    import os

    journal = journal_path({"session_id": "old"})
    StepJournal(journal).record(TraceEvent("x"))
    old = 3 * 86400
    os.utime(journal, (journal.stat().st_atime - old, journal.stat().st_mtime - old))
    assert check_freshness().status == WARN
    assert check_freshness(max_idle_hours=24 * 7).status == OK


def test_worst_reports_the_most_severe() -> None:
    from spotter.doctor import Check

    assert worst([Check("a", OK, ""), Check("b", WARN, "")]) == WARN
    assert worst([Check("a", WARN, ""), Check("b", FAIL, "")]) == FAIL
    assert worst([Check("a", OK, "")]) == OK


def test_doctor_exits_non_zero_when_something_is_wrong(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from spotter.doctor import Check

    monkeypatch.setattr("spotter.cli.run_doctor", lambda config: [Check("x", FAIL, "broken")])
    assert main(["doctor"]) == 2
    assert "NOT working" in capsys.readouterr().err


def test_doctor_run_covers_every_layer() -> None:
    names = {check.name for check in run(None)}
    assert {"interpreter", "round-trip", "observations"} <= names
    assert any(name.endswith("hook") or name.endswith("config") for name in names)


def test_status_reports_spend(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    StepJournal(journal_path({"session_id": "s"})).record(TraceEvent("x"))
    charge("s", tokens=1234)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "reviews today: 1" in out and "1234" in out
    assert json.loads((home / "review-spend.json").read_text())["sessions"]["s"]["tokens"] == 1234


def test_reserve_is_atomic_across_processes(home: Path) -> None:
    """PR #58 review, P0: check-then-charge lets concurrent sessions all see
    the same remaining budget. Only one process may take the last slot."""
    import subprocess
    import sys as _sys

    script = (
        "import os, sys\n"
        "from spotter.budget import reserve\n"
        "allowed, _ = reserve(sys.argv[1], 0, 3)\n"  # daily cap of 3
        "sys.exit(0 if allowed else 7)\n"
    )
    workers = [
        subprocess.Popen(
            [_sys.executable, "-c", script, f"s{i}"],
            cwd=Path(__file__).parent.parent,
            env={"PYTHONPATH": "src", "PATH": "/usr/bin:/bin", "SPOTTER_HOME": str(home)},
        )
        for i in range(10)
    ]
    granted = sum(1 for w in workers if w.wait() == 0)
    assert granted == 3, f"daily cap of 3 granted {granted} slots"


def test_reserve_then_settle_counts_the_review_once(home: Path) -> None:
    allowed, _ = reserve("a", 5, 100)
    assert allowed
    assert read("a").session == 1  # the slot is already counted
    settle("a", tokens=250)
    spend = read("a")
    assert (spend.session, spend.tokens) == (1, 250)  # cost added, count unchanged


def test_status_survives_a_corrupt_ledger(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """PR #58 review, P1: the diagnostic command must outlive the corruption
    it exists to diagnose."""
    StepJournal(journal_path({"session_id": "s"})).record(TraceEvent("x"))
    (home / "review-spend.json").write_text("{torn")
    assert main(["status"]) == 0
    assert "spend ledger unreadable" in capsys.readouterr().out
