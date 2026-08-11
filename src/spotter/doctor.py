"""Is supervision actually working? (issue #41)

Every failure path in the hook fails open, which is correct — a supervision
bug must never break the supervised session. The cost of that choice is that
total failure looks exactly like a quiet session: no journal, no error, no
difference. Silence is Spotter's designed normal state, so silence cannot also
be its failure state.

This module answers the one question the rest of the tool cannot: if nothing
was recorded, was there nothing to record, or is nothing running?
"""

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from spotter.paths import spotter_home

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _codex_hooks_files() -> list[Path]:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return [home / "hooks.json", home / "config.toml"]


def _claude_settings_files() -> list[Path]:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return [home / "settings.json", home / "settings.local.json"]


def check_registration() -> list[Check]:
    """Is a hook actually wired into a runtime?

    Registration is checked by reading each runtime's config for a command
    mentioning spotter. This is deliberately shallow — the alternative is
    modelling every runtime's config schema and staying correct as they change
    — but shallow beats the current situation, which is no check at all.
    """
    checks: list[Check] = []
    for label, files in (("codex", _codex_hooks_files()), ("claude", _claude_settings_files())):
        present = [p for p in files if p.exists()]
        if not present:
            checks.append(Check(f"{label} config", WARN, "no config found; runtime not installed?"))
            continue
        wired = [p for p in present if "spotter" in p.read_text(errors="replace")]
        if wired:
            checks.append(Check(f"{label} hook", OK, f"registered in {wired[0].name}"))
        else:
            checks.append(
                Check(
                    f"{label} hook",
                    WARN,
                    f"not registered in {', '.join(p.name for p in present)}",
                )
            )
    return checks


def check_interpreter() -> Check:
    """Report the interpreter; do not pretend to gate on it.

    A version gate here would be dead code: this module uses syntax that older
    interpreters cannot import, so reaching this line already proves the
    version. What actually matters is the interpreter the *hook* resolves,
    which may differ from this one — and that is what the round-trip check
    exercises end to end.
    """
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return Check("interpreter", OK, f"python {version} (hook's own is proven by round-trip)")


def check_storage() -> list[Check]:
    home = spotter_home()
    checks: list[Check] = []
    if not home.exists():
        return [Check("storage", WARN, f"{home} does not exist yet")]
    mode = home.stat().st_mode & 0o777
    checks.append(
        Check("permissions", OK if mode & 0o077 == 0 else WARN, f"{home} mode {oct(mode)}")
    )
    probe = home / ".doctor-probe"
    try:
        probe.write_text("probe")
        probe.unlink()
        checks.append(Check("writable", OK, str(home)))
    except OSError as error:
        checks.append(Check("writable", FAIL, f"{home}: {error}"))
    free = shutil.disk_usage(home).free
    checks.append(Check("disk", OK if free > 100e6 else FAIL, f"{free / 1e9:.1f} GB free"))
    return checks


def check_roundtrip(config_path: Path | None) -> Check:
    """Feed the real CLI a synthetic payload and confirm a record appears.

    Nothing else in the tool proves the whole path works; every component
    could be healthy while the wiring between them is not.
    """
    session = f"doctor-probe-{int(time.time())}"
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "cwd": str(Path.cwd()),
        "tool_name": "Bash",
        "tool_use_id": "doctor",
        "tool_input": {"command": "true"},
    }
    args = [sys.executable, "-m", "spotter", "hook"]
    if config_path is not None:
        args += ["--config", str(config_path)]
    try:
        result = subprocess.run(
            args,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            env={**os.environ, "SPOTTER_DISABLE": "1"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        return Check("round-trip", FAIL, f"hook could not run: {error}")
    journal = spotter_home() / "sessions" / f"{session}.jsonl"
    if not journal.exists():
        stderr = result.stderr.strip()[:200]
        return Check("round-trip", FAIL, f"no journal written{': ' + stderr if stderr else ''}")
    journal.unlink(missing_ok=True)
    for suffix in (".state", ".lock"):
        journal.with_suffix(journal.suffix + suffix).unlink(missing_ok=True)
    return Check("round-trip", OK, "synthetic payload recorded and cleaned up")


def check_freshness(max_idle_hours: float = 24.0) -> Check:
    sessions = spotter_home() / "sessions"
    journals = sorted(sessions.glob("*.jsonl")) if sessions.exists() else []
    real = [p for p in journals if not p.stem.startswith("doctor-probe")]
    if not real:
        return Check("observations", WARN, "no session has ever been recorded")
    age = (time.time() - max(p.stat().st_mtime for p in real)) / 3600
    status = OK if age <= max_idle_hours else WARN
    return Check("observations", status, f"last recorded {age:.1f}h ago")


def run(config_path: Path | None = None) -> list[Check]:
    checks = [check_interpreter()]
    checks.extend(check_registration())
    checks.extend(check_storage())
    checks.append(check_roundtrip(config_path))
    checks.append(check_freshness())
    return checks


def worst(checks: list[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    if any(c.status == WARN for c in checks):
        return WARN
    return OK
