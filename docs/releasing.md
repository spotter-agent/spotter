# Releases

Spotter releases are built only when an immutable semantic-version tag matching
`vMAJOR.MINOR.PATCH` is pushed. The tag is the single source of the package
version; Hatch VCS embeds it into both the wheel and source distribution.

Each GitHub release contains:

- a platform-independent Python wheel containing the `spotter` and `spotterd`
  entry points and the `spotter hook` bridge;
- a source distribution;
- `SHA256SUMS`, with a SHA-256 digest for both distributions.

The wheel uses ordinary Python console-script entry points and contains no
Homebrew prefix or Cellar path. A package manager may therefore choose its own
installation prefix. `spotter --version` and `spotterd --version` expose the
identity embedded at build time; compatibility contracts remain independently
versioned in `spotter.build`.
