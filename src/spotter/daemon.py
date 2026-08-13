"""Entry point for the packaged Spotter daemon executable."""

import argparse
from collections.abc import Sequence

from spotter.build import version_string


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spotterd", description="Spotter supervision daemon")
    parser.add_argument("--version", action="version", version=version_string("spotterd"))
    parser.parse_args(argv)
    parser.error("the standalone daemon runtime is not implemented in this release")
