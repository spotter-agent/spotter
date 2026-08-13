"""Build-backend version source.

Release builds set ``SPOTTER_BUILD_VERSION`` from an exact version tag. The
fallback keeps source/editable builds installable without pretending that they
are release artifacts.
"""

import os

__version__ = os.environ.get("SPOTTER_BUILD_VERSION", "0.1.0")
