"""Packaged build identity and independently versioned runtime contracts."""

from importlib.metadata import PackageNotFoundError, version

# These identifiers describe compatibility, not the package release. Bump one
# only when its corresponding reader/writer contract changes incompatibly.
IPC_PROTOCOL_VERSION = 1
CONFIG_SCHEMA_VERSION = 1
INTEGRATION_MANIFEST_VERSION = 1


def package_version() -> str:
    """Return the identity embedded in the installed distribution."""
    try:
        return version("spotter-agent")
    except PackageNotFoundError:
        # Source checkouts do not necessarily have generated VCS metadata.
        try:
            from spotter._version import __version__
        except ImportError:
            return "0.1.0.dev0+source"
        return __version__


def version_banner(program: str) -> str:
    return f"{program} {package_version()}"
