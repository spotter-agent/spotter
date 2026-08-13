"""Entry point for the packaged Spotter daemon."""

import argparse
from collections.abc import Sequence

from spotter.build import version_banner


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spotterd", description="Spotter supervision daemon")
    parser.add_argument("--version", action="version", version=version_banner("spotterd"))
    parser.parse_args(argv)
    return 0
