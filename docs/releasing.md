# Releases

Spotter releases are built only when a `vMAJOR.MINOR.PATCH` tag is pushed. The
tag is the single source of the package version: Hatch VCS writes that identity
into both the wheel and source distribution. Do not edit a second version file.

Each GitHub Release contains the platform-independent wheel, source archive,
and `SHA256SUMS`. Both archives include the `spotter`, `spotterd`, and
`spotter-hook` entry points. Package managers should install those entry points
into their normal executable directory; Spotter does not depend on Homebrew
Cellar or `/opt/homebrew` paths.

The CLI and daemon expose the same package identity with `--version`.
Compatibility contracts (IPC and persisted schemas) remain independently
versioned in `spotter.build`; consumers must not infer protocol compatibility
from the release version alone.
