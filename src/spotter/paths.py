"""Where Spotter stores things, and how it names them.

Extracted from the hook module because the budget ledger needs the same
answers and importing the hook to get them created a cycle. Location and
naming are lower-level than anything that observes.
"""

import os
import re
from contextlib import suppress
from pathlib import Path


def spotter_home() -> Path:
    return Path(os.environ.get("SPOTTER_HOME", Path.home() / ".spotter"))


def secure_dir(path: Path) -> Path:
    """Create a directory only its owner can read.

    Journals hold command history; the default 0755 made that history readable
    by every process on the machine (issue #39).
    """
    path.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.chmod(0o700)
    return path


def sanitize_session(session_id: object) -> str:
    """External input headed into a filename — never let it carry a path."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id or "unknown"))
