#!/usr/bin/env python3
"""Build immutable Spotter package artifacts from an exact version tag."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path
from typing import Any

PACKAGE_NAME = "spotter-agent"
TAG_PATTERN = re.compile(r"v(?P<version>\d+\.\d+\.\d+)")
RELEASE_MANIFEST_SCHEMA = 1


class ReleaseBuildError(RuntimeError):
    """The requested release cannot produce a trustworthy artifact set."""


@dataclass(frozen=True)
class ReleaseContext:
    tag: str
    version: str
    commit: str
    source_date_epoch: int

    @property
    def build_id(self) -> str:
        return f"{self.tag}@{self.commit}"


@dataclass(frozen=True)
class ReleaseArtifact:
    filename: str
    kind: str
    sha256: str
    size: int

    def to_json(self) -> dict[str, str | int]:
        return {
            "filename": self.filename,
            "kind": self.kind,
            "sha256": self.sha256,
            "size": self.size,
        }


def parse_version_tag(tag: str) -> str:
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ReleaseBuildError(f"unsupported release tag {tag!r}; expected vMAJOR.MINOR.PATCH")
    return match.group("version")


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ReleaseBuildError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def resolve_release_context(repo: Path, tag: str) -> ReleaseContext:
    """Resolve identity only through refs/tags, never a branch or working tree."""
    version = parse_version_tag(tag)
    tag_ref = f"refs/tags/{tag}"
    commit = _git(repo, "rev-parse", "--verify", f"{tag_ref}^{{commit}}")
    timestamp_text = _git(repo, "show", "-s", "--format=%ct", commit)
    try:
        timestamp = int(timestamp_text)
    except ValueError as error:
        raise ReleaseBuildError(f"tag {tag!r} has an invalid commit timestamp") from error
    return ReleaseContext(tag, version, commit, timestamp)


def _archive_tag(repo: Path, context: ReleaseContext, destination: Path) -> None:
    archive = destination.parent / "source.tar"
    with archive.open("wb") as output:
        result = subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", f"refs/tags/{context.tag}"],
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ReleaseBuildError(f"could not archive tag {context.tag!r}: {detail}")
    destination.mkdir()
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")


def _protocol_version(source: Path) -> int:
    protocol = runpy.run_path(str(source / "src/spotter/protocol.py")).get(
        "CONTROL_PROTOCOL_VERSION"
    )
    if not isinstance(protocol, int) or isinstance(protocol, bool):
        raise ReleaseBuildError("tagged source has no integer control protocol version")
    return protocol


def _write_generated_identity(source: Path, context: ReleaseContext) -> str:
    version_source = (
        f'"""Generated release version. Do not edit."""\n\n__version__ = {context.version!r}\n'
    )
    (source / "src/spotter/_version.py").write_text(version_source)
    content = (
        '"""Generated release identity. Do not edit."""\n\n'
        f"VERSION = {context.version!r}\n"
        f"RELEASE_TAG = {context.tag!r}\n"
        f"COMMIT = {context.commit!r}\n"
        f"BUILD_ID = {context.build_id!r}\n"
    )
    path = source / "src/spotter/_generated_build.py"
    path.write_text(content)
    return content


def _build_packages(source: Path, output: Path, context: ReleaseContext) -> None:
    environment = {
        **os.environ,
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": str(context.source_date_epoch),
        "SPOTTER_BUILD_VERSION": context.version,
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--wheel",
            "--outdir",
            str(output),
            str(source),
        ],
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise ReleaseBuildError("Python package build failed")


def _wheel_metadata(archive: zipfile.ZipFile, suffix: str) -> str:
    names = [name for name in archive.namelist() if name.endswith(suffix)]
    if len(names) != 1:
        raise ReleaseBuildError(f"wheel must contain exactly one {suffix}")
    return archive.read(names[0]).decode()


def _verify_wheel(path: Path, context: ReleaseContext, generated_identity: str) -> None:
    with zipfile.ZipFile(path) as wheel:
        try:
            embedded = wheel.read("spotter/_generated_build.py").decode()
        except KeyError as error:
            raise ReleaseBuildError("wheel does not contain embedded build identity") from error
        if embedded != generated_identity:
            raise ReleaseBuildError("wheel build identity differs from release tag context")
        version_source = wheel.read("spotter/_version.py").decode()
        if f"__version__ = {context.version!r}\n" not in version_source:
            raise ReleaseBuildError("wheel version source differs from release tag context")

        metadata = Parser().parsestr(_wheel_metadata(wheel, ".dist-info/METADATA"))
        if metadata.get("Name") != PACKAGE_NAME or metadata.get("Version") != context.version:
            raise ReleaseBuildError("wheel package metadata differs from release tag context")

        entry_points = configparser.ConfigParser()
        entry_points.read_string(_wheel_metadata(wheel, ".dist-info/entry_points.txt"))
        expected = {
            "spotter": "spotter.cli:main",
            "spotterd": "spotter.daemon:main",
        }
        actual = dict(entry_points.items("console_scripts"))
        if actual != expected:
            raise ReleaseBuildError(f"wheel console entry points differ: {actual!r}")


def _verify_sdist(path: Path, context: ReleaseContext, generated_identity: str) -> None:
    with tarfile.open(path, "r:gz") as sdist:
        names = sdist.getnames()
        identity_names = [
            name for name in names if name.endswith("/src/spotter/_generated_build.py")
        ]
        version_names = [name for name in names if name.endswith("/src/spotter/_version.py")]
        metadata_names = [name for name in names if name.endswith("/PKG-INFO")]
        if len(identity_names) != 1 or len(version_names) != 1 or len(metadata_names) != 1:
            raise ReleaseBuildError("sdist is missing build identity or package metadata")
        identity_file = sdist.extractfile(identity_names[0])
        version_file = sdist.extractfile(version_names[0])
        metadata_file = sdist.extractfile(metadata_names[0])
        if identity_file is None or identity_file.read().decode() != generated_identity:
            raise ReleaseBuildError("sdist build identity differs from release tag context")
        if version_file is None or (
            f"__version__ = {context.version!r}\n" not in version_file.read().decode()
        ):
            raise ReleaseBuildError("sdist version source differs from release tag context")
        if metadata_file is None:
            raise ReleaseBuildError("sdist package metadata is unreadable")
        metadata = Parser().parsestr(metadata_file.read().decode())
        if metadata.get("Name") != PACKAGE_NAME or metadata.get("Version") != context.version:
            raise ReleaseBuildError("sdist package metadata differs from release tag context")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path, kind: str) -> ReleaseArtifact:
    return ReleaseArtifact(path.name, kind, _sha256(path), path.stat().st_size)


def _find_packages(output: Path) -> tuple[Path, Path]:
    sdists = sorted(output.glob("*.tar.gz"))
    wheels = sorted(output.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise ReleaseBuildError(
            f"expected one sdist and one wheel, found {len(sdists)} and {len(wheels)}"
        )
    return sdists[0], wheels[0]


def _write_release_metadata(
    output: Path,
    context: ReleaseContext,
    protocol: int,
    artifacts: list[ReleaseArtifact],
) -> tuple[Path, Path]:
    manifest = output / f"spotter-agent-{context.version}-release.json"
    payload: dict[str, Any] = {
        "schema": RELEASE_MANIFEST_SCHEMA,
        "package": PACKAGE_NAME,
        "version": context.version,
        "release_tag": context.tag,
        "commit": context.commit,
        "build_id": context.build_id,
        "ipc_protocol_version": protocol,
        "entry_points": {
            "cli": "spotter",
            "daemon": "spotterd",
            "hook_bridge": "spotter hook",
        },
        "artifacts": [artifact.to_json() for artifact in artifacts],
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    checksummed = [*artifacts, _artifact(manifest, "release_manifest")]
    checksums = output / f"spotter-agent-{context.version}-SHA256SUMS"
    checksums.write_text(
        "".join(
            f"{artifact.sha256}  {artifact.filename}\n"
            for artifact in sorted(checksummed, key=lambda item: item.filename)
        )
    )
    return manifest, checksums


def _publish(source_paths: Sequence[Path], output: Path) -> tuple[Path, ...]:
    output.mkdir(parents=True, exist_ok=True)
    destinations = tuple(output / source.name for source in source_paths)
    existing = [path for path in destinations if path.exists()]
    if existing:
        raise ReleaseBuildError(
            "refusing to overwrite immutable release artifact(s): "
            + ", ".join(path.name for path in existing)
        )

    created: list[Path] = []
    try:
        for source, destination in zip(source_paths, destinations, strict=True):
            with source.open("rb") as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            created.append(destination)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return destinations


def build_release(repo: Path, tag: str, output: Path) -> tuple[Path, ...]:
    repo = repo.resolve()
    context = resolve_release_context(repo, tag)
    with tempfile.TemporaryDirectory(prefix="spotter-release-") as temporary:
        root = Path(temporary)
        source = root / "source"
        built = root / "built"
        built.mkdir()
        _archive_tag(repo, context, source)
        protocol = _protocol_version(source)
        generated_identity = _write_generated_identity(source, context)
        _build_packages(source, built, context)
        sdist, wheel = _find_packages(built)
        _verify_sdist(sdist, context, generated_identity)
        _verify_wheel(wheel, context, generated_identity)
        artifacts = [_artifact(sdist, "sdist"), _artifact(wheel, "wheel")]
        manifest, checksums = _write_release_metadata(built, context, protocol, artifacts)
        return _publish((sdist, wheel, manifest, checksums), output.resolve())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build Spotter release artifacts from an exact vMAJOR.MINOR.PATCH tag"
    )
    parser.add_argument(
        "--tag",
        help="version tag to archive (defaults to GITHUB_REF_NAME)",
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Git repository")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist/release"),
        help="new artifact destination",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    tag = arguments.tag or os.environ.get("GITHUB_REF_NAME")
    if not tag:
        parser.error("--tag is required outside a tag-triggered GitHub workflow")
    try:
        artifacts = build_release(arguments.repo, tag, arguments.output_dir)
    except (OSError, ReleaseBuildError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"release build failed: {error}", file=sys.stderr)
        return 1
    for artifact in artifacts:
        print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
