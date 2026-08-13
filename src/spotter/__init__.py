"""Spotter runtime supervision prototype."""

from spotter.build_identity import current_build_identity
from spotter.config import SpotterConfig
from spotter.core import SpotterRuntime

__version__ = current_build_identity().version

__all__ = ["SpotterConfig", "SpotterRuntime", "__version__"]
