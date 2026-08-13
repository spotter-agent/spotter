"""Versioned, dependency-free task corpus manifests."""

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASK_SCHEMA_VERSION = 1
TASK_SET_SCHEMA_VERSION = 1


class TaskCorpusError(ValueError):
    """A task corpus cannot be reproduced from its declared inputs."""


@dataclass(frozen=True)
class TaskManifest:
    task_id: str
    path: Path
    source: Path
    prompt: str


@dataclass(frozen=True)
class TaskSetManifest:
    task_set_id: str
    version: int
    split: str
    tasks: tuple[TaskManifest, ...]


def validate_task_set(path: Path) -> TaskSetManifest:
    """Validate immutable task/set identities without executing task commands."""

    set_path = path.resolve()
    data = _load_toml(set_path)
    _schema(data, "task_set_schema_version", TASK_SET_SCHEMA_VERSION, set_path)
    task_set_id = _text(data, "task_set_id", set_path)
    version = _positive_int(data, "version", set_path)
    split = _text(data, "split", set_path)
    if split not in {"dev", "validation"}:
        raise TaskCorpusError(f"{set_path}: split must be dev or validation")
    refs = data.get("tasks")
    if not isinstance(refs, list) or not refs:
        raise TaskCorpusError(f"{set_path}: tasks must be a non-empty array of tables")

    tasks: list[TaskManifest] = []
    seen: set[str] = set()
    for index, raw in enumerate(refs):
        where = f"{set_path}: tasks[{index}]"
        if not isinstance(raw, dict):
            raise TaskCorpusError(f"{where} must be a table")
        task_id = _text(raw, "task_id", where)
        if task_id in seen:
            raise TaskCorpusError(f"{set_path}: duplicate task_id {task_id!r}")
        seen.add(task_id)
        manifest = _contained_file(set_path.parent, _text(raw, "manifest", where), where)
        expected = _text(raw, "sha256", where)
        if file_digest(manifest) != expected:
            raise TaskCorpusError(f"{where}: manifest sha256 mismatch")
        task = _validate_task(manifest, set_path.parent)
        if task.task_id != task_id:
            raise TaskCorpusError(f"{where}: task_id does not match {manifest}")
        tasks.append(task)
    return TaskSetManifest(task_set_id, version, split, tuple(tasks))


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_digest(path: Path) -> str:
    """Hash a fixture's paths and bytes; symlinks are deliberately unsupported."""

    digest = hashlib.sha256()
    files = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
    if not files:
        raise TaskCorpusError(f"{path}: fixture must contain at least one file")
    for candidate in files:
        if candidate.is_symlink():
            raise TaskCorpusError(f"{candidate}: fixture symlinks are unsupported")
        digest.update(candidate.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_task(path: Path, corpus_root: Path) -> TaskManifest:
    data = _load_toml(path)
    _schema(data, "task_schema_version", TASK_SCHEMA_VERSION, path)
    task_id = _text(data, "task_id", path)
    prompt = _text(data, "prompt", path)

    source = _table(data, "source", path)
    if _text(source, "kind", path) != "fixture":
        raise TaskCorpusError(f"{path}: source.kind must be fixture")
    fixture = _contained_path(corpus_root, _text(source, "path", path), path)
    if not fixture.is_dir():
        raise TaskCorpusError(f"{path}: fixture source does not exist: {fixture}")
    if fixture_digest(fixture) != _text(source, "sha256", path):
        raise TaskCorpusError(f"{path}: fixture sha256 mismatch")

    _command(_table(data, "setup", path), path)
    precheck = _table(data, "precheck", path)
    _command(precheck, path)
    if _text(precheck, "expected", path) != "failure":
        raise TaskCorpusError(f"{path}: precheck.expected must be failure")
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        raise TaskCorpusError(f"{path}: checks must be a non-empty array of tables")
    check_ids: set[str] = set()
    for check in checks:
        if not isinstance(check, dict):
            raise TaskCorpusError(f"{path}: each check must be a table")
        check_id = _text(check, "id", path)
        if check_id in check_ids:
            raise TaskCorpusError(f"{path}: duplicate check id {check_id!r}")
        check_ids.add(check_id)
        _command(check, path)
        if not isinstance(check.get("required"), bool):
            raise TaskCorpusError(f"{path}: check {check_id!r} requires a boolean required")

    budget = _table(data, "budget", path)
    _positive_int(budget, "wall_time_s", path)
    _positive_int(budget, "max_turns", path)
    metadata = _table(data, "metadata", path)
    for key in ("family", "difficulty", "provenance"):
        _text(metadata, key, path)
    return TaskManifest(task_id, path, fixture, prompt)


def _command(data: dict[str, Any], where: object) -> None:
    _text(data, "command", where)
    _positive_int(data, "timeout_s", where)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise TaskCorpusError(f"{path}: {error}") from error


def _schema(data: dict[str, Any], key: str, expected: int, where: object) -> None:
    if data.get(key) != expected:
        raise TaskCorpusError(f"{where}: {key} must be {expected}")


def _table(data: dict[str, Any], key: str, where: object) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise TaskCorpusError(f"{where}: {key} must be a table")
    return value


def _text(data: dict[str, Any], key: str, where: object) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TaskCorpusError(f"{where}: {key} must be non-empty text")
    return value.strip()


def _positive_int(data: dict[str, Any], key: str, where: object) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TaskCorpusError(f"{where}: {key} must be a positive integer")
    return value


def _contained_file(root: Path, value: str, where: object) -> Path:
    path = _contained_path(root, value, where)
    if not path.is_file():
        raise TaskCorpusError(f"{where}: manifest does not exist: {path}")
    return path


def _contained_path(root: Path, value: str, where: object) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise TaskCorpusError(f"{where}: path escapes the corpus: {value}")
    return path
