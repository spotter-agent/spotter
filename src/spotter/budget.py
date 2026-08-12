"""Review spend accounting (issue #52).

A cadence without a ceiling is an unbounded background bill the user cannot
see. The counters live next to the journals so they survive process exit, and
exhaustion is journaled rather than silently observed: a supervisor that stops
working must never look like a supervisor with nothing to say.
"""

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

from spotter.paths import sanitize_session, secure_dir, spotter_home


@dataclass(frozen=True)
class Spend:
    session: int
    day: int
    tokens: int


def _ledger_path() -> Path:
    return spotter_home() / "review-spend.json"


def _today(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


class LedgerCorrupt(RuntimeError):
    """The spend ledger could not be read.

    Distinct from "no spend yet". Treating corruption as zero spend lifts
    every ceiling at once and lets the next write erase the history that
    proved they were reached (PR #58 review, P0).
    """


def _load(path: Path) -> dict[str, object]:
    """Read the ledger. Missing means zero; unreadable means unknown."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise LedgerCorrupt(f"{path.name} is unreadable: {error}") from error
    if not isinstance(data, dict):
        raise LedgerCorrupt(f"{path.name} is not an object")
    return data


def read(session: str, now: float | None = None) -> Spend:
    """Spend so far. Raises LedgerCorrupt when the ledger cannot be trusted."""
    data = _load(_ledger_path())
    sessions = data.get("sessions")
    day = data.get("day")
    count = 0
    tokens = 0
    if isinstance(sessions, dict):
        entry = sessions.get(sanitize_session(session))
        if isinstance(entry, dict):
            count = _as_int(entry.get("reviews"))
            tokens = _as_int(entry.get("tokens"))
    day_count = 0
    if isinstance(day, dict) and day.get("date") == _today(now):
        day_count = _as_int(day.get("reviews"))
    return Spend(count, day_count, tokens)


@contextmanager
def _locked() -> Iterator[Path]:
    """Serialize ledger reads and writes across processes."""
    home = secure_dir(spotter_home())
    with (home / "review-spend.lock").open("w") as lock:
        flock(lock, LOCK_EX)
        try:
            yield _ledger_path()
        finally:
            flock(lock, LOCK_UN)


def _project(
    data: dict[str, object], session: str, now: float | None
) -> tuple[dict[str, dict[str, int]], Spend]:
    """Current counters, normalised, plus the sessions map to write back."""
    raw_sessions = data.get("sessions")
    sessions: dict[str, dict[str, int]] = {}
    if isinstance(raw_sessions, dict):
        for name, value in raw_sessions.items():
            if isinstance(value, dict):
                sessions[str(name)] = {
                    "reviews": _as_int(value.get("reviews")),
                    "tokens": _as_int(value.get("tokens")),
                }
    entry = sessions.get(sanitize_session(session), {"reviews": 0, "tokens": 0})
    day = data.get("day")
    carried = 0
    if isinstance(day, dict) and day.get("date") == _today(now):
        carried = _as_int(day.get("reviews"))
    return sessions, Spend(entry["reviews"], carried, entry["tokens"])


def _write(
    path: Path,
    sessions: dict[str, dict[str, int]],
    session: str,
    reviews: int,
    tokens: int,
    day_reviews: int,
    now: float | None,
) -> None:
    """Replace the ledger atomically.

    A torn write is what makes a ledger unreadable, and an unreadable ledger
    is now refused rather than treated as zero — so the write must not be the
    thing that creates that state.
    """
    sessions[sanitize_session(session)] = {"reviews": reviews, "tokens": tokens}
    payload = {"sessions": sessions, "day": {"date": _today(now), "reviews": day_reviews}}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload))
    os.replace(temporary, path)


def reserve(
    session: str,
    max_per_session: int,
    max_per_day: int,
    now: float | None = None,
) -> tuple[bool, str]:
    """Atomically take a review slot, or refuse.

    Checking a ceiling and then spending against it in a separate step is not
    a ceiling: concurrent sessions all read the same remaining budget and all
    proceed. The check and the increment happen under one lock here, so the
    last slot can only be taken once.
    """
    with _locked() as path:
        try:
            data = _load(path)
        except LedgerCorrupt as error:
            # Fail closed on the spending path: an unreadable ledger is not
            # evidence of remaining budget.
            return False, f"ledger unreadable, refusing to spend ({error})"
        sessions, spend = _project(data, session, now)
        if max_per_session and spend.session >= max_per_session:
            return False, f"session cap reached ({spend.session}/{max_per_session})"
        if max_per_day and spend.day >= max_per_day:
            return False, f"daily cap reached ({spend.day}/{max_per_day})"
        _write(path, sessions, session, spend.session + 1, spend.tokens, spend.day + 1, now)
        return True, ""


def cancel(session: str, now: float | None = None) -> None:
    """Return a reserved slot that was never used.

    Reservation happens before the work, so every path that gives up before
    the model call must hand the slot back — otherwise ordinary operation
    leaks budget: two cadence children for one session both reserve, and the
    one that loses the in-flight lock exits without ever reviewing
    (PR #58 review, P1). Paths that gave up *after* a model call keep the
    slot, because that spend really happened.
    """
    with _locked() as path:
        try:
            data = _load(path)
        except LedgerCorrupt:
            return  # nothing trustworthy to decrement
        sessions, spend = _project(data, session, now)
        _write(
            path,
            sessions,
            session,
            max(0, spend.session - 1),
            spend.tokens,
            max(0, spend.day - 1),
            now,
        )


def settle(session: str, tokens: int, now: float | None = None) -> Spend:
    """Attach the measured cost to a slot already reserved.

    Reservation happens before the work and cannot know the price; this adds
    it afterwards without touching the counts, so a crash between the two
    loses the cost figure but never the ceiling.
    """
    with _locked() as path:
        try:
            data = _load(path)
        except LedgerCorrupt:
            return Spend(0, 0, 0)
        sessions, spend = _project(data, session, now)
        _write(
            path, sessions, session, spend.session, spend.tokens + max(0, tokens), spend.day, now
        )
        return Spend(spend.session, spend.day, spend.tokens + max(0, tokens))


def charge(session: str, tokens: int = 0, now: float | None = None) -> Spend:
    """Record one review and its cost in a single step.

    Used by the manual path, where check and spend are not separated by a
    subprocess boundary.
    """
    with _locked() as path:
        # Fail closed here as well: the manual path spends real tokens before
        # charging, so swallowing corruption both pays and erases the history
        # that proved a ceiling was reached (PR #58 review, P0).
        data = _load(path)
        sessions, spend = _project(data, session, now)
        _write(
            path,
            sessions,
            session,
            spend.session + 1,
            spend.tokens + max(0, tokens),
            spend.day + 1,
            now,
        )
        return Spend(spend.session + 1, spend.day + 1, spend.tokens + max(0, tokens))


def exhausted(session: str, max_per_session: int, max_per_day: int) -> str | None:
    """Which ceiling, if any, is already reached.

    Read-only; the spending path must use reserve(), which cannot race.
    """
    try:
        spend = read(session)
    except LedgerCorrupt as error:
        return f"ledger unreadable, refusing to spend ({error})"
    if max_per_session and spend.session >= max_per_session:
        return f"session cap reached ({spend.session}/{max_per_session})"
    if max_per_day and spend.day >= max_per_day:
        return f"daily cap reached ({spend.day}/{max_per_day})"
    return None


def spend_totals(now: float | None = None) -> dict[str, int] | None:
    """Aggregate spend for reporting, or None when nothing was ever recorded.

    Raises LedgerCorrupt so a diagnostic caller can say so instead of dying on
    a traceback (PR #58 review, P1).
    """
    path = _ledger_path()
    if not path.exists():
        return None
    data = _load(path)
    sessions = data.get("sessions")
    tokens = 0
    if isinstance(sessions, dict):
        tokens = sum(_as_int(v.get("tokens")) for v in sessions.values() if isinstance(v, dict))
    day = data.get("day")
    today = (
        _as_int(day.get("reviews"))
        if isinstance(day, dict) and day.get("date") == _today(now)
        else 0
    )
    return {"day": today, "tokens": tokens}
