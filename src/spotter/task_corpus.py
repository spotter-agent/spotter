"""Versioned, dependency-free task corpus manifests."""

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

TASK_SCHEMA_VERSION = 1
TASK_SET_SCHEMA_VERSION = 1


class TaskCorpusError(ValueError):
    """A task corpus cannot be reproduced from its declared inputs."""


@dataclass(frozen=True)
class CommandSpec:
    command: str
    timeout_s: int


@dataclass(frozen=True)
class CheckSpec:
    id: str
    command: CommandSpec
    required: bool


@dataclass(frozen=True)
class TaskManifest:
    task_id: str
    path: Path
    source: Path
    prompt: str
    setup: CommandSpec
    precheck: CommandSpec
    checks: tuple[CheckSpec, ...]
    known_good: CommandSpec | None


@dataclass(frozen=True)
class TaskSetManifest:
    task_set_id: str
    version: int
    split: str
    tasks: tuple[TaskManifest, ...]


class PreflightClassification(StrEnum):
    READY = "READY"
    SETUP_FAIL = "SETUP_FAIL"
    TIMEOUT_CHECK = "TIMEOUT_CHECK"
    CHECK_ERROR = "CHECK_ERROR"
    UNJUDGEABLE = "UNJUDGEABLE"


@dataclass(frozen=True)
class CommandResult:
    phase: str
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class TaskPreflight:
    task_id: str
    classification: PreflightClassification
    commands: tuple[CommandResult, ...]


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
    files: list[Path] = []
    if path.is_symlink():
        raise TaskCorpusError(f"{path}: fixture symlinks are unsupported")
    for current, directories, names in os.walk(
        path, followlinks=False, onerror=_fixture_walk_error
    ):
        base = Path(current)
        for name in [*directories, *names]:
            candidate = base / name
            if candidate.is_symlink():
                raise TaskCorpusError(f"{candidate}: fixture symlinks are unsupported")
        files.extend(base / name for name in names)
    files.sort()
    if not files:
        raise TaskCorpusError(f"{path}: fixture must contain at least one file")
    for candidate in files:
        digest.update(candidate.relative_to(path).as_posix().encode())
        digest.update(b"\0")
        digest.update(candidate.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _fixture_walk_error(error: OSError) -> None:
    raise TaskCorpusError(f"fixture cannot be read: {error}") from error


def preflight_task_set(path: Path) -> tuple[TaskSetManifest, tuple[TaskPreflight, ...]]:
    """Execute frozen scorer fixtures in isolated temporary copies."""

    task_set = validate_task_set(path)
    return task_set, tuple(_preflight_task(task) for task in task_set.tasks)


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

    setup = _command(_table(data, "setup", path), path)
    precheck = _table(data, "precheck", path)
    precheck_command = _command(precheck, path)
    if _text(precheck, "expected", path) != "failure":
        raise TaskCorpusError(f"{path}: precheck.expected must be failure")
    checks = data.get("checks")
    if not isinstance(checks, list) or not checks:
        raise TaskCorpusError(f"{path}: checks must be a non-empty array of tables")
    check_ids: set[str] = set()
    parsed_checks: list[CheckSpec] = []
    for check in checks:
        if not isinstance(check, dict):
            raise TaskCorpusError(f"{path}: each check must be a table")
        check_id = _text(check, "id", path)
        if check_id in check_ids:
            raise TaskCorpusError(f"{path}: duplicate check id {check_id!r}")
        check_ids.add(check_id)
        command = _command(check, path)
        if not isinstance(check.get("required"), bool):
            raise TaskCorpusError(f"{path}: check {check_id!r} requires a boolean required")
        parsed_checks.append(CheckSpec(check_id, command, check["required"]))
    if not any(check.required for check in parsed_checks):
        raise TaskCorpusError(f"{path}: at least one check must be required")

    known_good_raw = data.get("known_good")
    known_good = None
    if known_good_raw is not None:
        if not isinstance(known_good_raw, dict):
            raise TaskCorpusError(f"{path}: known_good must be a table")
        known_good = _command(known_good_raw, path)

    budget = _table(data, "budget", path)
    _positive_int(budget, "wall_time_s", path)
    _positive_int(budget, "max_turns", path)
    metadata = _table(data, "metadata", path)
    for key in ("family", "difficulty", "provenance"):
        _text(metadata, key, path)
    return TaskManifest(
        task_id,
        path,
        fixture,
        prompt,
        setup,
        precheck_command,
        tuple(parsed_checks),
        known_good,
    )


def _command(data: dict[str, Any], where: object) -> CommandSpec:
    return CommandSpec(_text(data, "command", where), _positive_int(data, "timeout_s", where))


def _preflight_task(task: TaskManifest) -> TaskPreflight:
    results: list[CommandResult] = []
    with tempfile.TemporaryDirectory(prefix="spotter-task-") as scratch:
        workspace = Path(scratch) / "workspace"
        shutil.copytree(task.source, workspace)
        setup = _run_command("setup", task.setup, workspace)
        results.append(setup)
        if setup.timed_out or setup.returncode != 0:
            return TaskPreflight(task.task_id, PreflightClassification.SETUP_FAIL, tuple(results))

        precheck = _run_command("precheck", task.precheck, workspace)
        results.append(precheck)
        if precheck.timed_out:
            return TaskPreflight(
                task.task_id, PreflightClassification.TIMEOUT_CHECK, tuple(results)
            )
        if precheck.returncode is None:
            return TaskPreflight(task.task_id, PreflightClassification.CHECK_ERROR, tuple(results))
        if precheck.returncode == 0:
            return TaskPreflight(task.task_id, PreflightClassification.UNJUDGEABLE, tuple(results))

        negative = [
            _run_command(f"negative:{check.id}", check.command, workspace) for check in task.checks
        ]
        results.extend(negative)
        required_negative = [
            result for check, result in zip(task.checks, negative, strict=True) if check.required
        ]
        if any(result.timed_out for result in required_negative):
            return TaskPreflight(
                task.task_id, PreflightClassification.TIMEOUT_CHECK, tuple(results)
            )
        if any(result.returncode is None for result in required_negative):
            return TaskPreflight(task.task_id, PreflightClassification.CHECK_ERROR, tuple(results))
        if all(result.returncode == 0 for result in required_negative):
            return TaskPreflight(task.task_id, PreflightClassification.UNJUDGEABLE, tuple(results))

        if task.known_good is None:
            return TaskPreflight(task.task_id, PreflightClassification.UNJUDGEABLE, tuple(results))
        known_good = _run_command("known_good", task.known_good, workspace)
        results.append(known_good)
        if known_good.timed_out or known_good.returncode != 0:
            return TaskPreflight(task.task_id, PreflightClassification.CHECK_ERROR, tuple(results))

        positive = [
            _run_command(f"positive:{check.id}", check.command, workspace) for check in task.checks
        ]
        results.extend(positive)
        required_positive = [
            result for check, result in zip(task.checks, positive, strict=True) if check.required
        ]
        if any(result.timed_out for result in required_positive):
            return TaskPreflight(
                task.task_id, PreflightClassification.TIMEOUT_CHECK, tuple(results)
            )
        if any(result.returncode is None for result in required_positive):
            return TaskPreflight(task.task_id, PreflightClassification.CHECK_ERROR, tuple(results))
        classification = (
            PreflightClassification.READY
            if all(result.returncode == 0 for result in required_positive)
            else PreflightClassification.UNJUDGEABLE
        )
        return TaskPreflight(task.task_id, classification, tuple(results))


_OUTPUT_LIMIT = 4000


def _run_command(phase: str, spec: CommandSpec, workspace: Path) -> CommandResult:
    try:
        process = subprocess.Popen(
            spec.command,
            shell=True,
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as error:
        return CommandResult(phase, None, "", str(error)[:_OUTPUT_LIMIT])
    try:
        stdout, stderr = process.communicate(timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        return CommandResult(
            phase,
            None,
            _bounded_output(stdout),
            _bounded_output(stderr),
            timed_out=True,
        )
    return CommandResult(
        phase,
        process.returncode,
        stdout[-_OUTPUT_LIMIT:],
        stderr[-_OUTPUT_LIMIT:],
    )


def _bounded_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return (value or "")[-_OUTPUT_LIMIT:]


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
