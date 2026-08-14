"""Versioned, dependency-free task corpus manifests."""

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import tempfile
import tomllib
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from spotter.experiment import CONTROL_PROMPT, ArmClassification
from spotter.paths import sanitize_session, secure_dir, spotter_home
from spotter.replay import ReplayError, find_rollout
from spotter.snapshot import SnapshotError, StepJournal

TASK_SCHEMA_VERSION = 1
TASK_SET_SCHEMA_VERSION = 1
TASK_BATCH_SCHEMA_VERSION = 1


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
    source_sha256: str
    prompt: str
    setup: CommandSpec
    precheck: CommandSpec
    checks: tuple[CheckSpec, ...]
    known_good: CommandSpec | None
    wall_time_s: int
    max_turns: int


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


@dataclass(frozen=True)
class TaskArmResult:
    run_id: str
    experiment_pair_id: str
    task_set_id: str
    task_set_version: int
    task_id: str
    arm: str
    classification: ArmClassification
    fixture_sha256: str
    wall_time_s: int
    max_turns: int
    setup: CommandResult
    checks: tuple[CommandResult, ...]
    agent_exit: int | None
    agent_stdout: str
    agent_stderr: str
    started_at: str
    ended_at: str
    replay_source_requested: bool = False
    replay_source_session_id: str | None = None
    replay_source_error: str | None = None
    workspace: str | None = None
    result_schema_version: int = TASK_BATCH_SCHEMA_VERSION


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


def run_task_batch(
    path: Path,
    guidance: str,
    *,
    resume: Path | None = None,
    model: str | None = None,
    sandbox: str = "workspace-write",
    keep_artifacts: bool = False,
    capture_replay_sources: bool = False,
) -> tuple[Path, tuple[TaskArmResult, ...]]:
    """Run control/guidance arms from clean fixture copies, resuming completed rows."""

    if not guidance.strip():
        raise TaskCorpusError("task batch guidance must be non-empty")
    set_path = path.resolve()
    task_set, preflight = preflight_task_set(set_path)
    not_ready = [
        result for result in preflight if result.classification != PreflightClassification.READY
    ]
    if not_ready:
        detail = ", ".join(f"{row.task_id}={row.classification}" for row in not_ready)
        raise TaskCorpusError(f"task batch preflight failed: {detail}")

    set_sha256 = file_digest(set_path)
    if resume is None:
        run_id = str(uuid.uuid4())
        output = task_batch_path(task_set, run_id)
        header = {
            "meta": True,
            "result_schema_version": TASK_BATCH_SCHEMA_VERSION,
            "run_id": run_id,
            "task_set_id": task_set.task_set_id,
            "task_set_version": task_set.version,
            "task_set_sha256": set_sha256,
            "split": task_set.split,
            "guidance": guidance,
            "model": model or "codex-config-default",
            "sandbox": sandbox,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "codex_version": _codex_version(),
            "codex_home": str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()),
            "capture_replay_sources": capture_replay_sources,
            "started_at": datetime.now(UTC).isoformat(),
        }
        _append_json(output, header)
        existing: list[TaskArmResult] = []
    else:
        output = resume.resolve()
        header, existing, was_complete = _read_task_batch(output)
        _validate_resume(
            output,
            header,
            task_set,
            set_sha256=set_sha256,
            guidance=guidance,
            model=model,
            sandbox=sandbox,
            capture_replay_sources=capture_replay_sources,
            existing=existing,
        )
        run_id = str(header["run_id"])
        if was_complete and len(existing) == len(task_set.tasks) * 2:
            return output, tuple(existing)

    completed = {(row.task_id, row.arm) for row in existing}
    results = list(existing)
    for index, task in enumerate(task_set.tasks):
        arms = [
            ("control", f"{task.prompt}\n\n{CONTROL_PROMPT}"),
            ("guidance", f"{task.prompt}\n\n{CONTROL_PROMPT} {guidance}"),
        ]
        if (index + uuid.UUID(run_id).int) % 2:
            arms.reverse()
        for arm, prompt in arms:
            if (task.task_id, arm) in completed:
                continue
            result = _run_task_arm(
                run_id,
                task_set,
                task,
                arm,
                prompt,
                model=model,
                sandbox=sandbox,
                keep_artifacts=keep_artifacts,
                capture_replay_source=capture_replay_sources,
            )
            _append_json(output, asdict(result))
            results.append(result)
    _append_json(
        output,
        {
            "complete": True,
            "run_id": run_id,
            "results": len(results),
            "finished_at": datetime.now(UTC).isoformat(),
        },
    )
    return output, tuple(results)


def task_batch_path(task_set: TaskSetManifest, run_id: str) -> Path:
    base = spotter_home() / "experiments" / "task-batches"
    base.mkdir(parents=True, exist_ok=True)
    safe_set_id = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in task_set.task_set_id
    )
    return base / f"{safe_set_id}-v{task_set.version}-{run_id}.jsonl"


def summarize_task_batch(results: tuple[TaskArmResult, ...]) -> str:
    task_count = len({row.task_id for row in results})
    lines = [f"task batch: {len(results)} arm(s), {task_count} task(s)"]
    for arm in ("control", "guidance"):
        arm_results = tuple(row for row in results if row.arm == arm)
        counts = ", ".join(
            f"{classification}={sum(row.classification == classification for row in arm_results)}"
            for classification in ArmClassification
            if any(row.classification == classification for row in arm_results)
        )
        lines.append(f"{arm}: {counts or 'no results'}")

    guidance_better = control_better = tied = complete = 0
    for pair_id in {row.experiment_pair_id for row in results}:
        pair = {row.arm: row for row in results if row.experiment_pair_id == pair_id}
        if set(pair) != {"control", "guidance"} or any(
            row.classification not in {ArmClassification.PASS, ArmClassification.TASK_FAIL}
            for row in pair.values()
        ):
            continue
        complete += 1
        control_passed = pair["control"].classification == ArmClassification.PASS
        guidance_passed = pair["guidance"].classification == ArmClassification.PASS
        guidance_better += guidance_passed and not control_passed
        control_better += control_passed and not guidance_passed
        tied += guidance_passed == control_passed
    lines.append(
        f"pairs: n={complete}/{task_count} mechanically judgeable; "
        f"guidance better={guidance_better}, control better={control_better}, tied={tied}"
    )
    requested = tuple(result for result in results if result.replay_source_requested)
    if requested:
        captured = sum(result.replay_source_session_id is not None for result in requested)
        lines.append(f"replay sources: {captured}/{len(requested)} captured")
    return "\n".join(lines)


def _run_task_arm(
    run_id: str,
    task_set: TaskSetManifest,
    task: TaskManifest,
    arm: str,
    prompt: str,
    *,
    model: str | None,
    sandbox: str,
    keep_artifacts: bool,
    capture_replay_source: bool,
) -> TaskArmResult:
    started_at = datetime.now(UTC).isoformat()
    if capture_replay_source:
        source_dir = secure_dir(spotter_home() / "task-sources")
        scratch = Path(tempfile.mkdtemp(prefix=f"{run_id}-{arm}-", dir=source_dir))
    else:
        scratch = Path(tempfile.mkdtemp(prefix="spotter-task-arm-"))
    workspace = scratch / "workspace"
    checks: tuple[CommandResult, ...] = ()
    agent_exit: int | None = None
    agent_stdout = ""
    agent_stderr = ""
    replay_source_session_id: str | None = None
    replay_source_error: str | None = None
    try:
        try:
            shutil.copytree(task.source, workspace)
            setup = _run_command("setup", task.setup, workspace)
        except OSError as error:
            setup = CommandResult("setup", None, "", _bounded_output(str(error)))

        if setup.timed_out or setup.returncode != 0:
            classification = ArmClassification.SETUP_FAIL
        else:
            if capture_replay_source:
                replay_source_error = _prepare_replay_repo(workspace)
            if replay_source_error is not None:
                classification = ArmClassification.SETUP_FAIL
            else:
                try:
                    if capture_replay_source:
                        completed = _run_task_agent(
                            workspace,
                            prompt,
                            model=model,
                            sandbox=sandbox,
                            timeout=task.wall_time_s,
                            capture_replay_source=True,
                        )
                    else:
                        completed = _run_task_agent(
                            workspace,
                            prompt,
                            model=model,
                            sandbox=sandbox,
                            timeout=task.wall_time_s,
                        )
                    agent_exit = completed.returncode
                    agent_stdout = _bounded_output(completed.stdout)
                    agent_stderr = _bounded_output(completed.stderr)
                    if capture_replay_source:
                        replay_source_session_id, replay_source_error = _replay_source(
                            completed.stdout
                        )
                    if agent_exit != 0:
                        classification = ArmClassification.INFRA_FAIL
                    else:
                        checks = tuple(
                            _run_command(f"check:{check.id}", check.command, workspace)
                            for check in task.checks
                        )
                        required = tuple(
                            result
                            for check, result in zip(task.checks, checks, strict=True)
                            if check.required
                        )
                        if any(result.timed_out for result in required):
                            classification = ArmClassification.TIMEOUT_CHECK
                        elif any(result.returncode is None for result in required):
                            classification = ArmClassification.CHECK_ERROR
                        elif all(result.returncode == 0 for result in required):
                            classification = ArmClassification.PASS
                        else:
                            classification = ArmClassification.TASK_FAIL
                except subprocess.TimeoutExpired as error:
                    classification = ArmClassification.TIMEOUT_AGENT
                    agent_stdout = _bounded_output(error.stdout)
                    agent_stderr = _bounded_output(error.stderr)
                    if capture_replay_source:
                        replay_source_session_id, replay_source_error = _replay_source(error.stdout)
                except OSError as error:
                    classification = ArmClassification.INFRA_FAIL
                    agent_stderr = _bounded_output(str(error))
        return TaskArmResult(
            run_id=run_id,
            experiment_pair_id=f"{run_id}:{task.task_id}",
            task_set_id=task_set.task_set_id,
            task_set_version=task_set.version,
            task_id=task.task_id,
            arm=arm,
            classification=classification,
            fixture_sha256=task.source_sha256,
            wall_time_s=task.wall_time_s,
            max_turns=task.max_turns,
            setup=setup,
            checks=checks,
            agent_exit=agent_exit,
            agent_stdout=agent_stdout,
            agent_stderr=agent_stderr,
            started_at=started_at,
            ended_at=datetime.now(UTC).isoformat(),
            replay_source_requested=capture_replay_source,
            replay_source_session_id=replay_source_session_id,
            replay_source_error=replay_source_error,
            workspace=str(workspace) if keep_artifacts or capture_replay_source else None,
        )
    finally:
        if not keep_artifacts and not capture_replay_source:
            shutil.rmtree(scratch, ignore_errors=True)


def _run_task_agent(
    workspace: Path,
    prompt: str,
    *,
    model: str | None,
    sandbox: str,
    timeout: int,
    capture_replay_source: bool = False,
) -> subprocess.CompletedProcess[str]:
    args = ["codex", "exec", "-C", str(workspace)]
    if model:
        args += ["--model", model]
    args += ["--skip-git-repo-check", "--sandbox", sandbox]
    if capture_replay_source:
        args.append("--json")
    args.append(prompt)
    env = {**os.environ}
    if capture_replay_source:
        env.pop("SPOTTER_DISABLE", None)
        env["SPOTTER_CAPTURE_ONLY"] = "1"
    else:
        env.pop("SPOTTER_CAPTURE_ONLY", None)
        env["SPOTTER_DISABLE"] = "1"
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as initial_timeout:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired as escaped_child:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            raise subprocess.TimeoutExpired(
                args,
                timeout,
                output=escaped_child.stdout or initial_timeout.stdout,
                stderr=escaped_child.stderr or initial_timeout.stderr,
            ) from escaped_child
        raise subprocess.TimeoutExpired(
            args, timeout, output=stdout, stderr=stderr
        ) from initial_timeout
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _prepare_replay_repo(workspace: Path) -> str | None:
    commands = (
        ("git", "init", "-q"),
        ("git", "config", "--local", "user.name", "Spotter fixture"),
        ("git", "config", "--local", "user.email", "fixture@spotter.invalid"),
        ("git", "add", "-A"),
        ("git", "commit", "-qm", "fixture baseline"),
    )
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return f"replay-source Git setup failed: {error}"
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            return f"replay-source Git setup failed: {detail[:300]}"
    return None


def _replay_source(stdout: str | bytes | None) -> tuple[str | None, str | None]:
    if isinstance(stdout, bytes):
        stdout = stdout.decode(errors="replace")
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        session_id = event.get("thread_id")
        if not isinstance(session_id, str) or not session_id:
            break
        journal = spotter_home() / "sessions" / f"{sanitize_session(session_id)}.jsonl"
        try:
            records = StepJournal.load(journal)
        except (OSError, SnapshotError):
            return None, f"Spotter journal missing or unreadable for session {session_id}"
        if not any(record.snapshot for record in records):
            return None, f"Spotter journal has no replay snapshot for session {session_id}"
        try:
            codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            find_rollout(session_id, codex_home)
        except ReplayError:
            return None, f"Codex rollout missing for session {session_id}"
        return session_id, None
    return None, "Codex JSON stream did not report a thread.started session"


def _append_json(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps(row) + "\n")
        sink.flush()
        os.fsync(sink.fileno())


def _read_task_batch(path: Path) -> tuple[dict[str, Any], list[TaskArmResult], bool]:
    try:
        lines = path.read_text().splitlines(keepends=True)
    except OSError as error:
        raise TaskCorpusError(f"cannot resume task batch {path}: {error}") from error
    if not lines:
        raise TaskCorpusError(f"cannot resume empty task batch {path}")
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            if index == len(lines) - 1:
                with path.open("w", encoding="utf-8") as sink:
                    sink.write("".join(lines[:index]))
                    sink.flush()
                    os.fsync(sink.fileno())
                break
            raise TaskCorpusError(f"task batch {path} has corrupt row {index + 1}") from error
        if not isinstance(row, dict):
            raise TaskCorpusError(f"task batch {path} row {index + 1} is not an object")
        rows.append(row)
    if not rows or rows[0].get("meta") is not True:
        raise TaskCorpusError(f"task batch {path} has no metadata header")

    results: list[TaskArmResult] = []
    seen: set[tuple[str, str]] = set()
    for row in rows[1:]:
        if "task_id" not in row:
            continue
        try:
            key = (str(row["task_id"]), str(row["arm"]))
            if key in seen:
                raise TaskCorpusError(f"task batch {path} has duplicate result {key}")
            seen.add(key)
            results.append(
                TaskArmResult(
                    run_id=str(row["run_id"]),
                    experiment_pair_id=str(row["experiment_pair_id"]),
                    task_set_id=str(row["task_set_id"]),
                    task_set_version=int(row["task_set_version"]),
                    task_id=key[0],
                    arm=key[1],
                    classification=ArmClassification(row["classification"]),
                    fixture_sha256=str(row["fixture_sha256"]),
                    wall_time_s=int(row["wall_time_s"]),
                    max_turns=int(row["max_turns"]),
                    setup=CommandResult(**row["setup"]),
                    checks=tuple(CommandResult(**check) for check in row["checks"]),
                    agent_exit=row["agent_exit"],
                    agent_stdout=str(row["agent_stdout"]),
                    agent_stderr=str(row["agent_stderr"]),
                    started_at=str(row["started_at"]),
                    ended_at=str(row["ended_at"]),
                    replay_source_requested=bool(row.get("replay_source_requested", False)),
                    replay_source_session_id=(
                        str(row["replay_source_session_id"])
                        if row.get("replay_source_session_id") is not None
                        else None
                    ),
                    replay_source_error=(
                        str(row["replay_source_error"])
                        if row.get("replay_source_error") is not None
                        else None
                    ),
                    workspace=row.get("workspace"),
                    result_schema_version=int(row["result_schema_version"]),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise TaskCorpusError(f"task batch {path} contains an invalid result") from error
    if any(result.result_schema_version != TASK_BATCH_SCHEMA_VERSION for result in results):
        raise TaskCorpusError(f"task batch {path} contains an unsupported result schema")
    return rows[0], results, any(row.get("complete") is True for row in rows[1:])


def _validate_resume(
    path: Path,
    header: dict[str, Any],
    task_set: TaskSetManifest,
    *,
    set_sha256: str,
    guidance: str,
    model: str | None,
    sandbox: str,
    capture_replay_sources: bool,
    existing: list[TaskArmResult],
) -> None:
    expected = {
        "result_schema_version": TASK_BATCH_SCHEMA_VERSION,
        "task_set_id": task_set.task_set_id,
        "task_set_version": task_set.version,
        "task_set_sha256": set_sha256,
        "guidance": guidance,
        "model": model or "codex-config-default",
        "sandbox": sandbox,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "codex_version": _codex_version(),
        "codex_home": str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()),
    }
    for key, value in expected.items():
        if header.get(key) != value:
            raise TaskCorpusError(f"cannot resume {path}: {key} does not match")
    if bool(header.get("capture_replay_sources", False)) != capture_replay_sources:
        raise TaskCorpusError(f"cannot resume {path}: capture_replay_sources does not match")
    try:
        uuid.UUID(str(header["run_id"]))
    except (KeyError, ValueError) as error:
        raise TaskCorpusError(f"cannot resume {path}: invalid run_id") from error
    manifests = {task.task_id: task for task in task_set.tasks}
    for row in existing:
        manifest = manifests.get(row.task_id)
        if (
            manifest is None
            or row.run_id != header["run_id"]
            or row.experiment_pair_id != f"{row.run_id}:{row.task_id}"
            or row.task_set_id != task_set.task_set_id
            or row.task_set_version != task_set.version
            or row.arm not in {"control", "guidance"}
            or row.fixture_sha256 != manifest.source_sha256
            or row.wall_time_s != manifest.wall_time_s
            or row.max_turns != manifest.max_turns
        ):
            raise TaskCorpusError(f"cannot resume {path}: result provenance does not match")


def _codex_version() -> str | None:
    try:
        result = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


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
    source_sha256 = _text(source, "sha256", path)
    if fixture_digest(fixture) != source_sha256:
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
    wall_time_s = _positive_int(budget, "wall_time_s", path)
    max_turns = _positive_int(budget, "max_turns", path)
    metadata = _table(data, "metadata", path)
    for key in ("family", "difficulty", "provenance"):
        _text(metadata, key, path)
    return TaskManifest(
        task_id,
        path,
        fixture,
        source_sha256,
        prompt,
        setup,
        precheck_command,
        tuple(parsed_checks),
        known_good,
        wall_time_s,
        max_turns,
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
    except subprocess.TimeoutExpired as initial_timeout:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired as escaped_child:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            return CommandResult(
                phase,
                None,
                _bounded_output(escaped_child.stdout or initial_timeout.stdout),
                _bounded_output(escaped_child.stderr or initial_timeout.stderr),
                timed_out=True,
            )
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
