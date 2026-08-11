"""Review spend accounting (issue #52).

A cadence without a ceiling is an unbounded background bill the user cannot
see. The counters live next to the journals so they survive process exit, and
exhaustion is journaled rather than silently observed: a supervisor that stops
working must never look like a supervisor with nothing to say.
"""

import json
import time
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path

from spotter.paths import sanitize_session, spotter_home


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


def _load(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def read(session: str, now: float | None = None) -> Spend:
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


def charge(session: str, tokens: int = 0, now: float | None = None) -> Spend:
    """Record one review against both counters, atomically."""
    home = spotter_home()
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / "review-spend.lock"
    with lock_path.open("w") as lock:
        flock(lock, LOCK_EX)
        try:
            path = _ledger_path()
            data = _load(path)
            raw_sessions = data.get("sessions")
            sessions: dict[str, dict[str, int]] = {}
            if isinstance(raw_sessions, dict):
                for name, value in raw_sessions.items():
                    if isinstance(value, dict):
                        sessions[str(name)] = {
                            "reviews": _as_int(value.get("reviews")),
                            "tokens": _as_int(value.get("tokens")),
                        }
            key = sanitize_session(session)
            previous_entry = sessions.get(key, {"reviews": 0, "tokens": 0})
            reviews = previous_entry["reviews"] + 1
            spent = previous_entry["tokens"] + max(0, tokens)
            sessions[key] = {"reviews": reviews, "tokens": spent}

            previous_day = data.get("day")
            carried = 0
            if isinstance(previous_day, dict) and previous_day.get("date") == _today(now):
                carried = _as_int(previous_day.get("reviews"))
            day_reviews = carried + 1

            path.write_text(
                json.dumps(
                    {"sessions": sessions, "day": {"date": _today(now), "reviews": day_reviews}}
                )
            )
            return Spend(reviews, day_reviews, spent)
        finally:
            flock(lock, LOCK_UN)


def exhausted(session: str, max_per_session: int, max_per_day: int) -> str | None:
    """Which ceiling, if any, is already reached."""
    spend = read(session)
    if max_per_session and spend.session >= max_per_session:
        return f"session cap reached ({spend.session}/{max_per_session})"
    if max_per_day and spend.day >= max_per_day:
        return f"daily cap reached ({spend.day}/{max_per_day})"
    return None
