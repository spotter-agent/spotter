"""Suite-wide isolation from the developer's own Spotter home.

Several durable paths resolve `spotter_home()` from the environment — the global
lock, journals, the repository registry, and the cached Git index snapshotting
reuses. A test that reached the real home would mutate a live 200-session store
and, worse, read state it did not create.

Tests that need a specific home still set `SPOTTER_HOME` themselves; monkeypatch
in the test wins over this default.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_spotter_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    home = tmp_path_factory.mktemp("spotter-home")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SPOTTER_HOME", str(home))
        yield home
