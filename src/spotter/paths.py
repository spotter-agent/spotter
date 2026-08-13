"""Package, integration, runtime, and durable-data layout discovery.

All install-method and platform path decisions belong here.  Callers may use
the compatibility helpers at the bottom, but they must not rediscover package
executables or derive mutable paths from ``sys.prefix``, ``__file__``, or the
current working directory themselves.
"""

import hashlib
import os
import re
import shutil
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

_SAFE_UNIX_PATH_BYTES = 100


class RuntimeLayoutError(RuntimeError):
    """A persistent integration cannot safely use the discovered layout."""


def _absolute(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _executable(value: str | Path | None, *, search_path: str) -> Path | None:
    if value is None or not str(value).strip():
        return None
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or len(candidate.parts) > 1:
        return _absolute(candidate)
    discovered = shutil.which(str(candidate), path=search_path)
    return Path(discovered) if discovered is not None else None


@dataclass(frozen=True)
class RuntimeLayout:
    """Resolved ownership boundaries for one Spotter installation.

    Executable paths deliberately preserve their stable symlink spelling.  In
    particular, resolving a Homebrew ``opt`` or ``bin`` link would turn it into
    the versioned Cellar path that persistent Hooks and services must avoid.
    """

    cli_executable: Path | None
    daemon_executable: Path | None
    package_assets_dir: Path
    user_config_dir: Path
    user_data_dir: Path
    integration_dir: Path
    runtime_dir: Path
    log_dir: Path
    user_home: Path
    system_config_home: Path

    @classmethod
    def discover(
        cls,
        *,
        cli_executable: str | Path | None = None,
        daemon_executable: str | Path | None = None,
        package_assets_dir: Path | None = None,
        spotter_root: Path | None = None,
        user_home: Path | None = None,
        argv0: str | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimeLayout":
        environment = os.environ if environ is None else environ
        search_path = environment.get("PATH", "")
        account_home = _absolute(user_home or Path.home())
        configured_root = spotter_root
        if configured_root is None:
            raw_root = environment.get("SPOTTER_HOME")
            configured_root = Path(raw_root) if raw_root else account_home / ".spotter"
        root = _absolute(configured_root)

        explicit_cli = cli_executable or environment.get("SPOTTER_CLI_EXECUTABLE")
        cli = _executable(explicit_cli, search_path=search_path)
        invoked_as = argv0 if argv0 is not None else sys.argv[0]
        invoked = Path(invoked_as)
        module_invocation = invoked.name == "__main__.py" and invoked.parent.name == "spotter"
        if cli is None and invoked.name == "spotter":
            cli = _executable(invoked_as, search_path=search_path)
        if cli is None and not module_invocation:
            cli = _executable("spotter", search_path=search_path)

        explicit_daemon = daemon_executable or environment.get("SPOTTER_DAEMON_EXECUTABLE")
        daemon = _executable(explicit_daemon, search_path=search_path)
        if daemon is None and cli is not None:
            # The active package boundary wins over PATH.  A missing sibling is
            # diagnosed later instead of silently selecting an older install.
            daemon = cli.with_name("spotterd")
        if daemon is None and not module_invocation:
            daemon = _executable("spotterd", search_path=search_path)

        raw_system_config = environment.get("XDG_CONFIG_HOME")
        system_config = _absolute(
            Path(raw_system_config) if raw_system_config else account_home / ".config"
        )
        return cls(
            cli_executable=cli,
            daemon_executable=daemon,
            package_assets_dir=(package_assets_dir or Path(__file__).parent).absolute(),
            user_config_dir=root,
            user_data_dir=root,
            integration_dir=root / "integrations",
            runtime_dir=root / "runtime",
            log_dir=root / "logs",
            user_home=account_home,
            system_config_home=system_config,
        )

    @property
    def cli_command(self) -> tuple[str, ...]:
        if self.cli_executable is not None:
            return (str(self.cli_executable),)
        return (sys.executable, "-m", "spotter")

    @property
    def daemon_command(self) -> tuple[str, ...]:
        if self.daemon_executable is not None:
            return (str(self.daemon_executable),)
        return (sys.executable, "-m", "spotter.daemon")

    @property
    def bridge_command(self) -> tuple[str, ...]:
        return (*self.cli_command, "hook")

    @property
    def control_socket(self) -> Path:
        candidate = self.runtime_dir / "spotterd.sock"
        if len(os.fsencode(candidate)) <= _SAFE_UNIX_PATH_BYTES:
            return candidate
        digest = hashlib.sha256(os.fsencode(self.user_data_dir)).hexdigest()[:12]
        return Path("/tmp") / f"spotter-{os.getuid()}-{digest}" / "spotterd.sock"

    @property
    def integration_manifest(self) -> Path:
        return self.integration_dir / "codex.json"

    def service_registration(self, platform: str, label: str) -> Path:
        if platform == "darwin":
            return self.user_home / "Library" / "LaunchAgents" / f"{label}.plist"
        if platform.startswith("linux"):
            return self.system_config_home / "systemd" / "user" / "spotterd.service"
        raise RuntimeLayoutError(f"managed services are unsupported on {platform}")

    def validate_persistent(self) -> None:
        """Reject commands that cannot survive a package version replacement."""
        for name, path in (
            ("spotter", self.cli_executable),
            ("spotterd", self.daemon_executable),
        ):
            self._validate_persistent_executable(name, path)

    def persistent_daemon_command(self) -> tuple[str, ...]:
        self._validate_persistent_executable("spotterd", self.daemon_executable)
        assert self.daemon_executable is not None
        return (str(self.daemon_executable),)

    @staticmethod
    def _validate_persistent_executable(name: str, path: Path | None) -> None:
        if path is None:
            raise RuntimeLayoutError(
                f"{name} has no stable executable; install the package entry points or set "
                f"SPOTTER_{'CLI' if name == 'spotter' else 'DAEMON'}_EXECUTABLE"
            )
        if not path.is_absolute():
            raise RuntimeLayoutError(f"{name} executable must be an absolute stable path")
        if "Cellar" in path.parts:
            raise RuntimeLayoutError(
                f"{name} executable is inside a versioned Cellar; use a stable bin/opt link"
            )

    def integration_record(self) -> dict[str, object]:
        """Serializable stable references and mutable ownership roots.

        ``package_assets_dir`` is intentionally absent: it may be versioned and
        must never become a persistent integration reference.
        """
        return {
            "cli_executable": (
                str(self.cli_executable) if self.cli_executable is not None else None
            ),
            "daemon_executable": (
                str(self.daemon_executable) if self.daemon_executable is not None else None
            ),
            "bridge_command": list(self.bridge_command),
            "user_config_dir": str(self.user_config_dir),
            "user_data_dir": str(self.user_data_dir),
            "integration_dir": str(self.integration_dir),
            "runtime_dir": str(self.runtime_dir),
            "log_dir": str(self.log_dir),
        }


def spotter_home() -> Path:
    """Compatibility root for existing state while callers migrate to RuntimeLayout."""
    return RuntimeLayout.discover().user_data_dir


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
