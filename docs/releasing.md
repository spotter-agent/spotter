# Release artifacts and build identity

Spotter's repository-owned release builder produces package-manager-neutral Python artifacts from an
exact tag. GitHub Release publication is a separate automation step tracked by
[#105](https://github.com/spotter-agent/spotter/issues/105); Homebrew Formula layout remains owned by
the dedicated tap.

## Artifact contract

For a supported tag `vX.Y.Z`, the release set is:

| Artifact | Purpose |
| --- | --- |
| `spotter_agent-X.Y.Z.tar.gz` | Source distribution consumed by source-based packagers such as the initial Homebrew Formula |
| `spotter_agent-X.Y.Z-py3-none-any.whl` | Platform-independent Python wheel containing the runtime package |
| `spotter-agent-X.Y.Z-release.json` | Machine-readable tag, commit, build, protocol, entry-point, size, and digest metadata |
| `spotter-agent-X.Y.Z-SHA256SUMS` | SHA256 digests for the sdist, wheel, and release manifest |

The wheel declares `spotter` and `spotterd` console entry points. The minimal enforcement bridge is
the packaged `spotter hook` command, so no plugin checkout or copied Python source is needed after
standalone installation. The release manifest records those three component entry points explicitly.

The artifacts contain no Homebrew prefix, Cellar path, or Formula layout. A Formula can consume the
sdist URL and SHA256 and install its declared Python/runtime dependencies without consulting `main`.

## Build a release set

Install the development tools, create the release tag, and run:

```bash
python -m pip install -e '.[dev]'
python scripts/build_release.py --tag vX.Y.Z
```

In a tag-triggered workflow, `--tag` may be omitted when `GITHUB_REF_NAME` is set. The default output
directory is `dist/release`; use `--output-dir` for an isolated validation build.

The builder deliberately does not build the working tree or a branch name. It:

1. accepts only `vMAJOR.MINOR.PATCH`;
2. resolves `refs/tags/<tag>` to its commit;
3. archives that tag directly, ignoring moving or dirty working-tree content;
4. derives the Python package version from the tag and the build ID from `<tag>@<full-commit>`;
5. uses the tagged commit timestamp as `SOURCE_DATE_EPOCH`;
6. builds and validates one sdist and one wheel;
7. verifies embedded identity, package metadata, and the `spotter` / `spotterd` entry points;
8. emits a release manifest and checksums; and
9. refuses to overwrite an existing versioned release set.

An unknown tag, branch name, malformed tag, failed package build, identity mismatch, or incomplete
entry-point set aborts before artifacts are published to the requested output directory.

## Runtime identity contract

Packaged runtime components share four independent facts:

```text
spotter_version       # Python package/release version
build_id              # exact release tag + commit
component             # cli | daemon | hook_bridge
ipc_protocol_version  # local control compatibility identifier
```

The user-facing commands report the same packaged release identity:

```console
$ spotter --version
spotter X.Y.Z (build vX.Y.Z@<commit>; ipc 1)

$ spotterd --version
spotterd X.Y.Z (build vX.Y.Z@<commit>; ipc 1)
```

CLI and Hook-bridge control requests identify their component/build, and successful daemon responses
identify the running daemon build. Build equality and protocol compatibility are deliberately
separate; negotiation, stale-daemon classification, and upgrade policy remain governed by
[#90](https://github.com/spotter-agent/spotter/issues/90).

Source/editable installations use the explicit build identity `source` rather than masquerading as
a tagged release, even when their semantic version matches a release.
