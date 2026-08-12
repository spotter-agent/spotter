"""Review spend accounting (issue #52).

A cadence without a ceiling is an unbounded background bill the user cannot
see. The counters live next to the journals so they survive process exit, and
exhaustion is journaled rather than silently observed: a supervisor that stops
working must never look like a supervisor with nothing to say.
"""

import json
import os
import time
import uuid
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


SLOT_TTL_SECONDS = 3600  # far beyond the reviewer's own 300s timeout


def _open_slots(data: dict[str, object]) -> dict[str, dict[str, object]]:
    """Reservations taken but not yet settled or cancelled."""
    raw = data.get("open_slots")
    if not isinstance(raw, dict):
        return {}
    slots: dict[str, dict[str, object]] = {}
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(value.get("session"), str):
            slots[str(key)] = {"session": value["session"], "at": _as_int(value.get("at"))}
    return slots


def _reclaim(
    data: dict[str, object],
    sessions: dict[str, dict[str, int]],
    slots: dict[str, dict[str, object]],
    now: float | None,
) -> int:
    """Drop reservations whose holder died, crediting their counts back.

    A child killed between reserving and settling would otherwise consume a
    slot forever — the same leak class as the in-flight skip, arriving by a
    path no caller can catch. The TTL is an order of magnitude beyond the
    reviewer's own timeout, so an expired slot means the holder is gone, not
    slow. The assumption is stated because it is an assumption: a review that
    somehow ran for an hour and then settled would find its token consumed and
    fall back to charging, which over-counts rather than under-counts.
    """
    stamp = int(now if now is not None else time.time())
    expired = [t for t, slot in slots.items() if stamp - _as_int(slot.get("at")) > SLOT_TTL_SECONDS]
    reclaimed_today = 0
    for token in expired:
        slot = slots.pop(token)
        name = str(slot.get("session"))
        entry = sessions.get(name)
        if entry:
            entry["reviews"] = max(0, entry["reviews"] - 1)
        day = data.get("day")
        if isinstance(day, dict) and day.get("date") == _today(now):
            reclaimed_today += 1
    return reclaimed_today


def _write(
    path: Path,
    sessions: dict[str, dict[str, int]],
    session: str,
    reviews: int,
    tokens: int,
    day_reviews: int,
    now: float | None,
    open_slots: dict[str, dict[str, object]],
) -> None:
    """Replace the ledger atomically.

    A torn write is what makes a ledger unreadable, and an unreadable ledger
    is now refused rather than treated as zero — so the write must not be the
    thing that creates that state.

    ``open_slots`` is required rather than optional. As a default it was a
    trap: charge() omitted it and every write therefore erased the identity of
    reservations other processes were still holding, so their settle() failed
    and the fallback charged a second time for one review (PR #58 review, P1).
    A caller that must think about outstanding reservations cannot forget to.
    """
    sessions[sanitize_session(session)] = {"reviews": reviews, "tokens": tokens}
    payload: dict[str, object] = {
        "sessions": sessions,
        "day": {"date": _today(now), "reviews": day_reviews},
    }
    if open_slots:
        payload["open_slots"] = open_slots
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload))
    os.replace(temporary, path)


def reserve(
    session: str,
    max_per_session: int,
    max_per_day: int,
    now: float | None = None,
) -> tuple[str | None, str]:
    """Atomically take a review slot, returning its token.

    Checking a ceiling and then spending against it in a separate step is not
    a ceiling: concurrent sessions all read the same remaining budget and all
    proceed. The check and the increment happen under one lock here, so the
    last slot can only be taken once.

    The token exists because the slot is consumed in one process and settled
    in another. Without it, "already reserved" is an unverifiable claim: any
    caller could assert it and review for free, and a cancel could be applied
    more times than a slot was ever taken.
    """
    with _locked() as path:
        try:
            data = _load(path)
        except LedgerCorrupt as error:
            # Fail closed on the spending path: an unreadable ledger is not
            # evidence of remaining budget.
            return None, f"ledger unreadable, refusing to spend ({error})"
        sessions, spend = _project(data, session, now)
        # Reclaim before judging the ceiling: a dead holder's slot must not
        # keep a live session out.
        open_slots = _open_slots(data)
        reclaimed = _reclaim(data, sessions, open_slots, now)
        if reclaimed:
            spend = Spend(
                sessions.get(sanitize_session(session), {"reviews": 0})["reviews"],
                max(0, spend.day - reclaimed),
                spend.tokens,
            )
        if max_per_session and spend.session >= max_per_session:
            return None, f"session cap reached ({spend.session}/{max_per_session})"
        if max_per_day and spend.day >= max_per_day:
            return None, f"daily cap reached ({spend.day}/{max_per_day})"
        token = uuid.uuid4().hex
        open_slots[token] = {"session": sanitize_session(session), "at": int(now or time.time())}
        _write(
            path,
            sessions,
            session,
            spend.session + 1,
            spend.tokens,
            spend.day + 1,
            now,
            open_slots,
        )
        return token, ""


def cancel(session: str, token: str | None, now: float | None = None) -> bool:
    """Return a reserved slot that was never used. Idempotent by token.

    Reservation happens before the work, so every path that gives up before
    the model call must hand the slot back — otherwise ordinary operation
    leaks budget: two cadence children for one session both reserve, and the
    one that loses the in-flight lock exits without ever reviewing
    (PR #58 review, P1). Paths that gave up *after* a model call keep the
    slot, because that spend really happened.

    Consuming the token is what makes a repeat call harmless; without it,
    cancelling twice credits back a slot that was never taken.
    """
    if not token:
        return False
    with _locked() as path:
        try:
            data = _load(path)
        except LedgerCorrupt:
            return False  # nothing trustworthy to decrement
        open_slots = _open_slots(data)
        slot = open_slots.pop(token, None)
        if slot is None or slot.get("session") != sanitize_session(session):
            return False  # unknown or already settled/cancelled
        sessions, spend = _project(data, session, now)
        _write(
            path,
            sessions,
            session,
            max(0, spend.session - 1),
            spend.tokens,
            max(0, spend.day - 1),
            now,
            open_slots,
        )
        return True


def settle(session: str, token: str | None, tokens: int, now: float | None = None) -> Spend | None:
    """Attach the measured cost to a slot already reserved.

    Reservation happens before the work and cannot know the price; this adds
    it afterwards without touching the counts, so a crash between the two
    loses the cost figure but never the ceiling.

    An unknown token returns None so the caller charges normally. Without that
    check, asserting "already reserved" would be enough to review for free:
    settle adds cost but no count, so every ceiling would be bypassed by a
    flag anyone can pass.
    """
    if not token:
        return None
    with _locked() as path:
        try:
            data = _load(path)
        except LedgerCorrupt:
            return None
        open_slots = _open_slots(data)
        slot = open_slots.pop(token, None)
        if slot is None or slot.get("session") != sanitize_session(session):
            return None
        sessions, spend = _project(data, session, now)
        _write(
            path,
            sessions,
            session,
            spend.session,
            spend.tokens + max(0, tokens),
            spend.day,
            now,
            open_slots,
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
            _open_slots(data),  # other processes' reservations are not ours to drop
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
