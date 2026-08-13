import subprocess
from pathlib import Path

import pytest

from scripts.build_release import (
    ReleaseBuildError,
    _archive_tag,
    _write_generated_identity,
    parse_version_tag,
    resolve_release_context,
)


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _tagged_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Spotter Tests")
    _git(repo, "config", "user.email", "tests@spotter.invalid")
    (repo / "identity.txt").write_text("tagged\n")
    _git(repo, "add", "identity.txt")
    _git(repo, "commit", "--quiet", "-m", "tagged source")
    _git(repo, "tag", "v1.2.3")
    return repo


@pytest.mark.parametrize("tag", ["main", "1.2.3", "v1.2", "v1.2.3-rc1"])
def test_release_tags_have_one_supported_unambiguous_shape(tag: str) -> None:
    with pytest.raises(ReleaseBuildError, match="vMAJOR.MINOR.PATCH"):
        parse_version_tag(tag)


def test_release_context_is_derived_from_exact_tag(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path)
    expected_commit = _git(repo, "rev-parse", "HEAD")

    context = resolve_release_context(repo, "v1.2.3")

    assert context.version == "1.2.3"
    assert context.commit == expected_commit
    assert context.build_id == f"v1.2.3@{expected_commit}"
    assert context.source_date_epoch > 0


def test_release_archive_ignores_moving_worktree(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path)
    (repo / "identity.txt").write_text("moving branch\n")
    context = resolve_release_context(repo, "v1.2.3")
    destination = tmp_path / "archive"

    _archive_tag(repo, context, destination)

    assert (destination / "identity.txt").read_text() == "tagged\n"


def test_release_staging_pins_version_without_build_environment(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path)
    context = resolve_release_context(repo, "v1.2.3")
    source = tmp_path / "source"
    (source / "src/spotter").mkdir(parents=True)

    generated = _write_generated_identity(source, context)

    assert (source / "src/spotter/_version.py").read_text().endswith("__version__ = '1.2.3'\n")
    assert f"BUILD_ID = {context.build_id!r}" in generated


def test_unknown_version_tag_is_rejected(tmp_path: Path) -> None:
    repo = _tagged_repo(tmp_path)

    with pytest.raises(ReleaseBuildError, match="rev-parse"):
        resolve_release_context(repo, "v9.9.9")
