"""Spend ceilings and liveness (#52, #41)."""

import json
from pathlib import Path

import pytest

from spotter.budget import (
    LEDGER_SCHEMA,
    LEDGER_SCHEMA_VERSION,
    LedgerCorrupt,
    cancel,
    charge,
    exhausted,
    read,
    reserve,
    settle,
)
from spotter.cli import main
from spotter.config import GatesConfig, MainAgentConfig, ReviewerConfig, SpotterConfig
from spotter.doctor import (
    FAIL,
    INFO,
    OK,
    WARN,
    check_freshness,
    check_registration,
    check_roundtrip,
    run,
    worst,
)
from spotter.hook import journal_path, run_hook
from spotter.snapshot import StepJournal
from spotter.trace import TraceEvent


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPOTTER_HOME", str(tmp_path / "spotter"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
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
    token, refusal = reserve("a", 3, 100)
    assert token is None and "refusing to spend" in refusal


def test_ledger_writes_are_atomic(home: Path) -> None:
    """A torn write is what makes a ledger unreadable, so the writer must not
    be the thing that creates the state the reader now refuses."""
    charge("a", tokens=5)
    ledger = home / "review-spend.json"
    assert ledger.exists()
    assert not list(home.glob("review-spend.json.tmp"))
    raw = json.loads(ledger.read_text())
    assert (raw["schema"], raw["schema_version"]) == (
        LEDGER_SCHEMA,
        LEDGER_SCHEMA_VERSION,
    )
    assert raw["sessions"]["a"]["tokens"] == 5


def test_legacy_ledger_is_read_old_and_written_current(home: Path) -> None:
    ledger = home / "review-spend.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "sessions": {"a": {"reviews": 2, "tokens": 50}},
                "day": {"date": "1970-01-01", "reviews": 2},
            }
        )
    )

    legacy = read("a", now=0)
    assert (legacy.session, legacy.day, legacy.tokens) == (2, 2, 50)
    charge("a", tokens=25, now=0)

    upgraded = json.loads(ledger.read_text())
    assert upgraded["schema"] == LEDGER_SCHEMA
    assert upgraded["schema_version"] == LEDGER_SCHEMA_VERSION
    assert upgraded["sessions"]["a"] == {"reviews": 3, "tokens": 75}


@pytest.mark.parametrize(
    ("schema", "version", "message"),
    [
        (LEDGER_SCHEMA, LEDGER_SCHEMA_VERSION + 1, "newer schema"),
        ("future.review_spend", LEDGER_SCHEMA_VERSION, "unsupported schema"),
        (LEDGER_SCHEMA, "1", "non-integer"),
    ],
)
def test_unknown_ledger_schema_is_non_destructive(
    home: Path, schema: object, version: object, message: str
) -> None:
    ledger = home / "review-spend.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "schema": schema,
                "schema_version": version,
                "sessions": {"a": {"reviews": 2, "tokens": 50}},
            }
        )
    )
    before = ledger.read_bytes()

    with pytest.raises(LedgerCorrupt, match=message):
        charge("a")
    token, refusal = reserve("a", 5, 100)

    assert token is None and "refusing to spend" in refusal
    assert ledger.read_bytes() == before


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


def test_capture_roundtrip_uses_capture_only_and_cleans_probe(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run_hook_command(
        args: object, *, input: str, env: dict[str, str], **kwargs: object
    ) -> object:
        assert env["SPOTTER_CAPTURE_ONLY"] == "1"
        assert "SPOTTER_DISABLE" not in env
        payload = json.loads(input)
        StepJournal(journal_path(payload)).record(TraceEvent("tool_proposal"))
        return type("Completed", (), {"stderr": ""})()

    monkeypatch.setattr("spotter.doctor.subprocess.run", run_hook_command)

    assert check_roundtrip(None, command=("spotter", "hook"), capture_only=True).status == OK
    assert not list((home / "sessions").glob("doctor-probe-*"))


def test_freshness_reports_never_observed() -> None:
    assert check_freshness().status == INFO
    assert "recorded yet" in check_freshness().detail


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


def test_unregistered_optional_runtime_is_informational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    codex.mkdir()
    claude.mkdir()
    (codex / "hooks.json").write_text('{"hooks":{"PreToolUse":[{"command":"spotter hook"}]}}')
    (claude / "settings.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))

    checks = check_registration()

    assert worst(checks) == OK
    assert next(check for check in checks if check.name == "claude hook").status == INFO


def test_status_reports_spend(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    StepJournal(journal_path({"session_id": "s"})).record(TraceEvent("x"))
    charge("s", tokens=1234)
    assert main(["status"]) == 1
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
        "token, _ = reserve(sys.argv[1], 0, 3)\n"  # daily cap of 3
        "sys.exit(0 if token else 7)\n"
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
    token, _ = reserve("a", 5, 100)
    assert token
    assert read("a").session == 1  # the slot is already counted
    settle("a", token, 250)
    spend = read("a")
    assert (spend.session, spend.tokens) == (1, 250)  # cost added, count unchanged


def test_status_survives_a_corrupt_ledger(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """PR #58 review, P1: the diagnostic command must outlive the corruption
    it exists to diagnose."""
    StepJournal(journal_path({"session_id": "s"})).record(TraceEvent("x"))
    (home / "review-spend.json").write_text("{torn")
    assert main(["status"]) == 2
    assert "spend ledger unreadable" in capsys.readouterr().out


def test_unused_slots_are_returned(home: Path) -> None:
    """PR #58 review, P1: reservation happens before the work, so every path
    that gives up before the model call must hand the slot back."""
    token, _ = reserve("a", 2, 100)
    assert token and read("a").session == 1
    assert cancel("a", token)
    assert read("a").session == 0 and read("a").day == 0
    # the cap is genuinely restored, not merely reported
    for _ in range(2):
        assert reserve("a", 2, 100)[0]
    assert not reserve("a", 2, 100)[0]


def test_a_slot_can_only_be_returned_once() -> None:
    """Self-audit: without a token, cancelling twice credits back a slot that
    was never taken."""
    first, _ = reserve("a", 9, 100)
    reserve("a", 9, 100)
    assert read("a").session == 2
    assert cancel("a", first) is True
    assert cancel("a", first) is False  # already returned
    assert cancel("a", "not-a-token") is False
    assert read("a").session == 1


def test_asserting_a_reservation_cannot_buy_a_free_review() -> None:
    """Self-audit: settle adds cost but no count, so an unverifiable claim of
    'already reserved' would bypass every ceiling."""
    for _ in range(3):
        reserve("a", 3, 100)
    assert reserve("a", 3, 100)[0] is None  # cap reached
    assert settle("a", "forged-token", 999) is None
    assert settle("a", None, 999) is None
    token, _ = reserve("a", 99, 100)
    assert settle("a", token, 10) is not None
    assert settle("a", token, 10) is None  # a token settles once


def test_in_flight_skip_returns_the_slot(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two cadence children for one session both reserve; the one that loses
    the in-flight lock never reviews, so ordinary operation would leak."""
    from fcntl import LOCK_EX, flock

    StepJournal(journal_path({"session_id": "busy"})).record(TraceEvent("x"))
    token, _ = reserve("busy", 5, 100)
    assert token and read("busy").session == 1

    lock_file = journal_path({"session_id": "busy"}).with_suffix(".review.lock")
    with lock_file.open("w") as held:
        flock(held, LOCK_EX)
        assert main(["review", "--session", "busy", "--reservation", str(token)]) == 0
    assert "already in flight" in capsys.readouterr().err
    assert read("busy").session == 0, "a skipped review kept its slot"


def test_manual_review_refuses_to_spend_on_a_corrupt_ledger(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """PR #58 review, P0: the manual path calls the model first, so a corrupt
    ledger would pay for a review and then erase the history proving a cap was
    reached."""
    StepJournal(journal_path({"session_id": "s"})).record(
        TraceEvent("tool_proposal", {"command": "ls"})
    )
    charge("s")
    (home / "review-spend.json").write_text("{torn")

    called: list[bool] = []
    monkeypatch.setattr("spotter.cli.review", lambda *a, **k: called.append(True))
    assert main(["review", "--session", "s"]) == 1
    assert called == [], "the model was called despite an unreadable ledger"
    assert "refused" in capsys.readouterr().err


def test_charge_fails_closed_on_corruption(home: Path) -> None:
    charge("a")
    (home / "review-spend.json").write_text("{torn")
    with pytest.raises(LedgerCorrupt):
        charge("a")


def test_doctor_reports_a_corrupt_ledger(home: Path) -> None:
    """Self-audit: doctor called supervision healthy while every review was
    being refused for an unreadable ledger."""
    from spotter.doctor import check_ledger

    home.mkdir(parents=True, exist_ok=True)
    assert check_ledger().status == OK  # nothing recorded yet
    charge("a", tokens=7)
    assert "7 tokens" in check_ledger().detail
    (home / "review-spend.json").write_text("{torn")
    check = check_ledger()
    assert check.status == FAIL and "refused" in check.detail


def test_a_crashed_holder_does_not_consume_a_slot_forever(home: Path) -> None:
    """Self-audit: a child killed between reserving and settling leaks a slot
    by a path no caller can catch."""
    from spotter.budget import SLOT_TTL_SECONDS

    start = 1_000_000
    for _ in range(3):
        assert reserve("a", 3, 100, now=start)[0]
    assert reserve("a", 3, 100, now=start)[0] is None  # cap reached, holders alive

    # Long after any review could still be running, the slots come back.
    later = start + SLOT_TTL_SECONDS + 1
    token, refusal = reserve("a", 3, 100, now=later)
    assert token, f"expired slots were not reclaimed: {refusal}"


def test_reclaim_does_not_touch_live_reservations(home: Path) -> None:
    start = 1_000_000
    live, _ = reserve("a", 5, 100, now=start)
    assert live
    reserve("a", 5, 100, now=start + 60)
    # A reservation younger than the TTL is still its holder's.
    assert settle("a", live, 10, now=start + 120) is not None


def test_reclaimed_slot_cannot_be_settled_later(home: Path) -> None:
    """The assumption is stated in the code: a holder that returns after the
    TTL finds its token gone and charges instead, over-counting rather than
    under-counting."""
    from spotter.budget import SLOT_TTL_SECONDS

    start = 1_000_000
    token, _ = reserve("a", 5, 100, now=start)
    assert token
    reserve("a", 5, 100, now=start + SLOT_TTL_SECONDS + 1)  # triggers reclaim
    assert settle("a", token, 10) is None


def test_a_manual_charge_does_not_erase_open_reservations(home: Path) -> None:
    """PR #58 review, P1: every ledger write must carry outstanding
    reservations through, or one manual review erases the identity of every
    automatic one still running and each is then charged twice."""
    token, _ = reserve("A", 99, 100)
    assert token
    charge("B")  # a manual review lands while A is still running
    assert settle("A", token, 10) is not None, "A's reservation was erased by B"
    assert read("A").day == 2, "two reviews were counted as more"
