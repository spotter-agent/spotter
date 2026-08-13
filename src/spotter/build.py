"""Release and compatibility identity shared by every packaged executable."""

from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version

# These identifiers describe independent contracts. Bump one only when its
# contract changes; package releases are not a substitute for negotiation.
IPC_PROTOCOL_VERSION = 1
CONFIG_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION = 1
LABEL_SCHEMA_VERSION = 1
EXPERIMENT_SCHEMA_VERSION = 1
INTEGRATION_MANIFEST_VERSION = 1


def package_version() -> str:
    """Return the version embedded by hatch-vcs when the package was built."""
    try:
        return version("spotter-agent")
    except PackageNotFoundError:
        # Source trees without installed metadata are useful to contributors,
        # but release archives are always built and verified from a tag.
        try:
            from spotter._version import __version__
        except ImportError:
            return "0+unknown"
        return __version__


@dataclass(frozen=True)
class BuildIdentity:
    spotter_version: str
    ipc_protocol_version: int = IPC_PROTOCOL_VERSION
    config_schema_version: int = CONFIG_SCHEMA_VERSION
    journal_schema_version: int = JOURNAL_SCHEMA_VERSION
    label_schema_version: int = LABEL_SCHEMA_VERSION
    experiment_schema_version: int = EXPERIMENT_SCHEMA_VERSION
    integration_manifest_version: int = INTEGRATION_MANIFEST_VERSION

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


def build_identity() -> BuildIdentity:
    return BuildIdentity(spotter_version=package_version())


def version_string(program: str) -> str:
    return f"{program} {package_version()}"
