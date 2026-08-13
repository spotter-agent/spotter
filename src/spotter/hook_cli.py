"""Dedicated executable for the packaged Codex hook bridge."""

import sys
from collections.abc import Sequence

from spotter.cli import main as cli_main


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    return cli_main(["hook", *args])
