"""Fork a supervised Codex session at a journal step — the replay half of P0.

Mechanism (approximate replay, the plan's fallback path):
1. Codex persists every session as a rollout JSONL under ~/.codex/sessions.
2. The Spotter journal records tool_use_id per proposal, which matches the
   rollout's call_id — that is the branch-point correlation key.
3. Fork = copy the rollout truncated strictly before the branch call, rewrite
   its session id to a fresh one, restore the nearest repo snapshot into a
   detached worktree, and hand back a ready `codex exec resume` invocation.
4. Run the same fork twice — once with injected guidance, once without — and
   the pair is a same-prefix counterfactual (plan Q3).

Honest limits, stated rather than papered over:
- Resume compatibility VERIFIED 2026-08-11: `codex exec resume` accepted a
  truncated rollout (fork of a real 104-step session at step 98). The agent
  located its context at the branch point and reported the cut call as "my
  last action was interrupted" — the exact branch semantics wanted. The cut
  leaves one dangling call; Codex logs a warning and proceeds.
- Sessions journaled before snapshots/tool_use_id existed cannot be forked;
  the errors below say exactly which ingredient is missing.
- This does not execute anything itself: launching costs money and runs an
  agent, so the human stays on the trigger.
"""

import hashlib
import json
import os
import platform
import shlex
import subprocess
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from fcntl import LOCK_EX, flock
from pathlib import Path

from spotter.effects import external_effects
from spotter.hook import journal_path
from spotter.labels import load_all_labels, matches
from spotter.paths import secure_dir, spotter_home
from spotter.redact import redact_text
from spotter.snapshot import StepJournal, StepRecord, restore_snapshot


class ReplayError(RuntimeError):
    """Raised when a fork cannot be assembled from the recorded ingredients."""


FORK_MANIFEST_SCHEMA = "spotter.fork_manifest"
FORK_MANIFEST_SCHEMA_VERSION = 6


class ForkStatus(StrEnum):
    CREATING = "CREATING"
    READY = "READY"
    FAILED = "FAILED"


class EnvironmentDrift(StrEnum):
    TRACKED_STATE_MISMATCH = "TRACKED_STATE_MISMATCH"
    MISSING_IGNORED_FILE = "MISSING_IGNORED_FILE"
    MISSING_UNTRACKED_FILE = "MISSING_UNTRACKED_FILE"
    MISSING_VENV_OR_CACHE = "MISSING_VENV_OR_CACHE"
    ENVIRONMENT_VARIABLE_MISMATCH = "ENVIRONMENT_VARIABLE_MISMATCH"
    ABSOLUTE_PATH_MISMATCH = "ABSOLUTE_PATH_MISMATCH"
    SYMLINK_OR_SUBMODULE_MISMATCH = "SYMLINK_OR_SUBMODULE_MISMATCH"
    TOOL_VERSION_DRIFT = "TOOL_VERSION_DRIFT"
    UNKNOWN_ENVIRONMENT_DRIFT = "UNKNOWN_ENVIRONMENT_DRIFT"


class BranchCoverageStatus(StrEnum):
    FORKABLE_EXACT = "FORKABLE_EXACT"
    NOT_FORKABLE_STATE = "NOT_FORKABLE_STATE"
    NOT_FORKABLE_CONTEXT = "NOT_FORKABLE_CONTEXT"
    UNSAFE_EXTERNAL_EFFECT = "UNSAFE_EXTERNAL_EFFECT"
    OBSERVATION_GAP = "OBSERVATION_GAP"


@dataclass(frozen=True)
class BranchPointCoverage:
    step: int
    tool_use_id: str | None
    status: BranchCoverageStatus
    snapshot: str | None
    before_first_mutation: bool


@dataclass(frozen=True)
class SignalBranchCoverage:
    signal_id: str
    signal_type: str
    trigger_step: int
    source_event_id: str | None
    proposal_step: int | None
    status: BranchCoverageStatus | None


@dataclass(frozen=True)
class LabeledBranchCoverage:
    label_step: int
    verdict: str
    scope: str
    target_kind: str
    proposal_step: int | None
    status: BranchCoverageStatus | None
    stale: bool


@dataclass(frozen=True)
class BranchCoverageReport:
    session_id: str
    candidates: int
    counts: dict[str, int]
    earliest_forkable_step: int | None
    pre_mutation_candidates: int
    pre_mutation_forkable: int
    points: tuple[BranchPointCoverage, ...]
    signal_triggers: int
    signal_trigger_followups: int
    signal_trigger_followups_forkable: int
    signal_trigger_points: tuple[SignalBranchCoverage, ...]
    labeled_opportunities: int
    labeled_opportunities_current: int
    labeled_opportunity_branch_points: int
    labeled_opportunity_branch_points_forkable: int
    labeled_opportunity_points: tuple[LabeledBranchCoverage, ...]


@dataclass(frozen=True)
class DeclaredResourceFingerprint:
    path: str
    state: str
    sha256: str | None
    kind: str = "file"
    worktree_path_reference: bool = False
    purpose: str = "resource"


@dataclass(frozen=True)
class DeclaredEnvironmentVariableFingerprint:
    name: str
    state: str
    sha256: str | None
    worktree_path_reference: bool = False


@dataclass(frozen=True)
class EnvironmentFingerprint:
    worktree: str
    snapshot_sha: str
    tree_sha: str
    status_sha256: str
    index_diff_sha256: str
    tracked_diff_sha256: str
    submodule_status_sha256: str
    git_version: str
    python_version: str
    platform_system: str
    platform_machine: str
    ignored_state: str
    environment_variables: str
    fingerprint_sha256: str
    declared_resources: tuple[DeclaredResourceFingerprint, ...] = ()
    declared_environment_variables: tuple[DeclaredEnvironmentVariableFingerprint, ...] = ()


@dataclass(frozen=True)
class EnvironmentComparison:
    equivalent: bool
    drift: tuple[EnvironmentDrift, ...]


@dataclass(frozen=True)
class PrefixManifest:
    prefix_id: str
    source_session_id: str
    branch_step: int
    source_event_id: str | None
    source_turn_id: str | None
    connection_epoch: int | None
    journal_schema_version: int
    tool_use_id: str
    repository_path: str
    repository_id: str
    snapshot_sha: str
    snapshot_tree_sha: str
    rollout_prefix_sha256: str
    agent: str
    model: str | None
    runtime_version: str | None
    agent_config: str
    context_source: str
    context_limitations: tuple[str, ...]
    external_effects: tuple[dict[str, object], ...]
    observation_gaps: int
    created_at: str


@dataclass(frozen=True)
class ForkManifest:
    schema: str | None
    schema_version: int
    fork_id: str
    status: ForkStatus
    prefix: PrefixManifest
    worktree: str
    rollout: str | None
    environment: EnvironmentFingerprint | None
    created_at: str
    updated_at: str
    failure: str | None = None
    source_environment_preflight: str = "MATCHED"


@dataclass(frozen=True)
class ForkPlan:
    session_id: str  # fresh id of the forked session
    branch_step: int
    worktree: str
    rollout: str
    command: str  # suggested invocation; the human runs it
    # Kept backward-compatible for callers constructing plans directly.
    external_effects: list[dict[str, object]] = field(default_factory=list)
    manifest: str | None = None
    prefix_id: str | None = None
    environment_fingerprint: str | None = None
    source_environment_preflight: str = "MATCHED"


def find_rollout(session_id: str, codex_home: Path | None = None) -> Path:
    home = codex_home or Path.home() / ".codex"
    matches = sorted(
        path
        for path in (home / "sessions").rglob("rollout-*.jsonl")
        if path.stem.endswith(session_id)
    )
    if not matches:
        raise ReplayError(f"no rollout found for session {session_id} under {home}/sessions")
    return matches[-1]


def branch_coverage(session_id: str, codex_home: Path | None = None) -> BranchCoverageReport:
    """Classify every journaled proposal using currently available replay artifacts."""

    records = StepJournal.load(journal_path({"session_id": session_id}))
    try:
        rollout = find_rollout(session_id, codex_home)
        rollout_records = _read_rollout(rollout)
        rollout_ids = {
            identifier
            for record in rollout_records
            for identifier in _record_ids(record)
            if isinstance(identifier, str)
        }
        code_mode_ids = _code_mode_call_ids(records, rollout_records)
    except (OSError, ReplayError):
        rollout_ids = set()
        code_mode_ids = None
    mutation_steps = [
        record.step
        for record in records
        if record.event.kind == "tool_proposal"
        and (
            record.event.payload.get("reversibility_class") == "B"
            or record.event.payload.get("tool") == "apply_patch"
        )
    ]
    first_mutation = min(mutation_steps, default=None)
    snapshot: str | None = None
    gap_seen = False
    external_effect_seen = False
    object_cache: dict[tuple[Path, str], bool] = {}
    points: list[BranchPointCoverage] = []
    for record in records:
        snapshot = record.snapshot or snapshot
        gap_seen = gap_seen or record.event.kind == "observation_gap"
        external_effect_seen = external_effect_seen or record.event.kind == "external_effect"
        if record.event.kind != "tool_proposal":
            continue
        tool_use = record.event.payload.get("tool_use_id")
        tool_use_id = tool_use if isinstance(tool_use, str) and tool_use else None
        repo = _recorded_repo(records, record.step)
        if gap_seen:
            status = BranchCoverageStatus.OBSERVATION_GAP
        elif external_effect_seen:
            status = BranchCoverageStatus.UNSAFE_EXTERNAL_EFFECT
        elif tool_use_id is None or (
            record.step not in code_mode_ids
            if code_mode_ids is not None
            else tool_use_id not in rollout_ids
        ):
            status = BranchCoverageStatus.NOT_FORKABLE_CONTEXT
        elif snapshot is None or repo is None or not _snapshot_exists(repo, snapshot, object_cache):
            status = BranchCoverageStatus.NOT_FORKABLE_STATE
        else:
            status = BranchCoverageStatus.FORKABLE_EXACT
        points.append(
            BranchPointCoverage(
                record.step,
                tool_use_id,
                status,
                snapshot,
                first_mutation is None or record.step < first_mutation,
            )
        )
        # The branch is immediately before this proposal, so the proposal's
        # possible effect contaminates later points, not its own branch point.
        # Current Class C results create explicit external_effect records;
        # legacy proposals without a class cannot prove that they were local.
        external_effect_seen = (
            external_effect_seen or record.event.payload.get("reversibility_class") is None
        )
    counts = {
        status.value: sum(point.status == status for point in points)
        for status in BranchCoverageStatus
    }
    pre_mutation = [point for point in points if point.before_first_mutation]
    forkable = [point for point in points if point.status == BranchCoverageStatus.FORKABLE_EXACT]
    signal_points = _signal_branch_coverage(records, points)
    labeled_points = _labeled_branch_coverage(session_id, records, points)
    return BranchCoverageReport(
        session_id=session_id,
        candidates=len(points),
        counts=counts,
        earliest_forkable_step=forkable[0].step if forkable else None,
        pre_mutation_candidates=len(pre_mutation),
        pre_mutation_forkable=sum(
            point.status == BranchCoverageStatus.FORKABLE_EXACT for point in pre_mutation
        ),
        points=tuple(points),
        signal_triggers=len(signal_points),
        signal_trigger_followups=sum(point.proposal_step is not None for point in signal_points),
        signal_trigger_followups_forkable=sum(
            point.status == BranchCoverageStatus.FORKABLE_EXACT for point in signal_points
        ),
        signal_trigger_points=signal_points,
        labeled_opportunities=len(labeled_points),
        labeled_opportunities_current=sum(not point.stale for point in labeled_points),
        labeled_opportunity_branch_points=sum(
            point.proposal_step is not None for point in labeled_points
        ),
        labeled_opportunity_branch_points_forkable=sum(
            point.status == BranchCoverageStatus.FORKABLE_EXACT for point in labeled_points
        ),
        labeled_opportunity_points=labeled_points,
    )


def _signal_branch_coverage(
    records: list[StepRecord], points: list[BranchPointCoverage]
) -> tuple[SignalBranchCoverage, ...]:
    point_by_step = {point.step: point for point in points}
    triggers: list[SignalBranchCoverage] = []
    for record in records:
        payload = record.event.payload
        signal_id = payload.get("signal_id")
        signal_type = payload.get("signal_type")
        if (
            record.event.kind != "signal_candidate"
            or payload.get("status") != "active"
            or not isinstance(signal_id, str)
            or not isinstance(signal_type, str)
        ):
            continue
        proposal: BranchPointCoverage | None = None
        for later in records[record.step + 1 :]:
            later_payload = later.event.payload
            if (
                later.event.kind == "signal_candidate"
                and later_payload.get("signal_id") == signal_id
                and later_payload.get("status") in {"resolved", "stale"}
            ):
                break
            if later.event.kind == "tool_proposal":
                proposal = point_by_step[later.step]
                break
        source_event_id = payload.get("source_event_id")
        triggers.append(
            SignalBranchCoverage(
                signal_id,
                signal_type,
                record.step,
                source_event_id if isinstance(source_event_id, str) else None,
                proposal.step if proposal else None,
                proposal.status if proposal else None,
            )
        )
    return tuple(triggers)


def _labeled_branch_coverage(
    session_id: str, records: list[StepRecord], points: list[BranchPointCoverage]
) -> tuple[LabeledBranchCoverage, ...]:
    """Map current human judgments to the proposal where an intervention could branch."""

    point_by_step = {point.step: point for point in points}
    opportunities: list[LabeledBranchCoverage] = []
    labels = (
        label for (step, _scope), label in load_all_labels(session_id).items() if step is not None
    )
    for label in sorted(labels, key=lambda label: label.step or 0):
        assert label.step is not None
        target = records[label.step] if 0 <= label.step < len(records) else None
        target_kind = target.event.kind if target else "missing"
        stale = not matches(label, records)
        proposal: BranchPointCoverage | None = None
        if not stale and target is not None:
            if label.scope.startswith("signal:"):
                proposal = next(
                    (
                        point_by_step[record.step]
                        for record in records[label.step + 1 :]
                        if record.event.kind == "tool_proposal"
                    ),
                    None,
                )
            elif target_kind in {"gate_shadow_block", "gate_block"}:
                tool_use_id = target.event.payload.get("tool_use_id")
                if isinstance(tool_use_id, str) and tool_use_id:
                    proposal = next(
                        (
                            point_by_step[record.step]
                            for record in reversed(records[: label.step])
                            if record.event.kind == "tool_proposal"
                            and record.event.payload.get("tool_use_id") == tool_use_id
                        ),
                        None,
                    )
                elif label.step > 0 and records[label.step - 1].event.kind == "tool_proposal":
                    # Legacy gate events lacked the correlation id. Only adjacency
                    # is strong enough evidence to recover their branch point.
                    proposal = point_by_step[records[label.step - 1].step]
            elif target_kind == "tool_proposal":
                proposal = point_by_step.get(target.step)
            elif target_kind in {"reviewer_decision", "signal_candidate"}:
                proposal = next(
                    (
                        point_by_step[record.step]
                        for record in records[label.step + 1 :]
                        if record.event.kind == "tool_proposal"
                    ),
                    None,
                )
        opportunities.append(
            LabeledBranchCoverage(
                label.step,
                label.verdict,
                label.scope,
                target_kind,
                proposal.step if proposal else None,
                proposal.status if proposal else None,
                stale,
            )
        )
    return tuple(opportunities)


def branch_coverage_to_json(report: BranchCoverageReport) -> str:
    return json.dumps(asdict(report), indent=2)


def _environment_resource_paths(values: Sequence[str]) -> tuple[str, ...]:
    paths: set[str] = set()
    for value in values:
        path = Path(value)
        if not value or path.is_absolute() or path == Path(".") or ".." in path.parts:
            raise ReplayError(f"environment resource must be a relative path: {value!r}")
        paths.add(path.as_posix())
    return tuple(sorted(paths))


def _environment_variable_names(values: Sequence[str]) -> tuple[str, ...]:
    names: set[str] = set()
    for name in values:
        if (
            not name
            or not name.isascii()
            or not (name[0].isalpha() or name[0] == "_")
            or not all(character.isalnum() or character == "_" for character in name[1:])
        ):
            raise ReplayError(f"environment variable must be a POSIX name: {name!r}")
        names.add(name)
    return tuple(sorted(names))


def _fingerprint_environment_variables(
    names: Sequence[str],
    worktree: Path,
) -> tuple[DeclaredEnvironmentVariableFingerprint, ...]:
    variables: list[DeclaredEnvironmentVariableFingerprint] = []
    worktree_path = str(worktree.resolve())
    for name in _environment_variable_names(names):
        value = os.environ.get(name)
        variables.append(
            DeclaredEnvironmentVariableFingerprint(
                name,
                "set" if value is not None else "missing",
                _digest(os.fsencode(value)) if value is not None else None,
                value is not None and worktree_path in value,
            )
        )
    return tuple(variables)


def _resource_digest(candidate: Path, value: str, worktree: Path) -> tuple[str, str, bool]:
    worktree_path = os.fsencode(str(worktree.resolve()))
    if candidate.is_file():
        contents = candidate.read_bytes()
        return "file", _digest(contents), worktree_path in contents
    if not candidate.is_dir():
        raise ReplayError(f"environment resource must be a regular file or directory: {value!r}")
    entries: list[tuple[str, str, str | None]] = []
    references_worktree = False
    for child in sorted(
        candidate.rglob("*"), key=lambda path: path.relative_to(candidate).as_posix()
    ):
        relative = child.relative_to(candidate).as_posix()
        if child.is_symlink():
            raise ReplayError(f"environment resource contains a symlink: {value!r}/{relative}")
        if child.is_file():
            contents = child.read_bytes()
            entries.append(("file", relative, _digest(contents)))
            references_worktree = references_worktree or worktree_path in contents
        elif child.is_dir():
            entries.append(("directory", relative, None))
        else:
            raise ReplayError(
                f"environment resource contains an unsupported entry: {value!r}/{relative}"
            )
    material = json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode()
    return "directory", _digest(material), references_worktree


def _fingerprint_resources(
    worktree: Path,
    values: Sequence[str],
    venv_or_cache_values: Sequence[str] = (),
) -> tuple[DeclaredResourceFingerprint, ...]:
    root = worktree.resolve()
    resources: list[DeclaredResourceFingerprint] = []
    venv_or_cache_paths = set(_environment_resource_paths(venv_or_cache_values))
    paths = set(_environment_resource_paths(values)) | venv_or_cache_paths
    for value in sorted(paths):
        purpose = "venv_or_cache" if value in venv_or_cache_paths else "resource"
        candidate = worktree / value
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root) or candidate.is_symlink():
            raise ReplayError(f"environment resource escapes the worktree: {value!r}")
        if not candidate.exists():
            resources.append(
                DeclaredResourceFingerprint(value, "missing", None, "missing", purpose=purpose)
            )
            continue
        kind, sha256, worktree_path_reference = _resource_digest(candidate, value, worktree)
        resources.append(
            DeclaredResourceFingerprint(
                value,
                _git_path_state(worktree, value),
                sha256,
                kind,
                worktree_path_reference,
                purpose,
            )
        )
    return tuple(resources)


def _git_path_state(worktree: Path, value: str) -> str:
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", value],
            cwd=worktree,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReplayError(f"cannot classify environment resource {value!r}: {error}") from error
    if ignored.returncode == 0:
        return "ignored"
    if ignored.returncode != 1:
        raise ReplayError(f"git check-ignore failed for environment resource {value!r}")
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", value],
            cwd=worktree,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReplayError(f"cannot classify environment resource {value!r}: {error}") from error
    if tracked.returncode not in {0, 1}:
        raise ReplayError(f"git ls-files failed for environment resource {value!r}")
    return "tracked" if tracked.returncode == 0 else "untracked"


def _declared_resource_drift(
    left: tuple[DeclaredResourceFingerprint, ...],
    right: tuple[DeclaredResourceFingerprint, ...],
) -> tuple[EnvironmentDrift, ...]:
    if tuple((resource.path, resource.purpose) for resource in left) != tuple(
        (resource.path, resource.purpose) for resource in right
    ):
        return (EnvironmentDrift.UNKNOWN_ENVIRONMENT_DRIFT,)
    drift: list[EnvironmentDrift] = []
    for source, restored in zip(left, right, strict=True):
        if source.purpose == "venv_or_cache" and restored.state == "missing":
            category = EnvironmentDrift.MISSING_VENV_OR_CACHE
        elif source.state == "ignored" and restored.state == "missing":
            category = EnvironmentDrift.MISSING_IGNORED_FILE
        elif source.state == "untracked" and restored.state == "missing":
            category = EnvironmentDrift.MISSING_UNTRACKED_FILE
        elif source.worktree_path_reference != restored.worktree_path_reference:
            category = EnvironmentDrift.ABSOLUTE_PATH_MISMATCH
        elif (
            source.sha256 is not None
            and source.kind == restored.kind
            and source.sha256 == restored.sha256
        ):
            continue
        elif source.state == "tracked":
            category = EnvironmentDrift.TRACKED_STATE_MISMATCH
        else:
            category = EnvironmentDrift.UNKNOWN_ENVIRONMENT_DRIFT
        if category not in drift:
            drift.append(category)
    return tuple(drift)


def _declared_environment_variable_drift(
    left: tuple[DeclaredEnvironmentVariableFingerprint, ...],
    right: tuple[DeclaredEnvironmentVariableFingerprint, ...],
) -> tuple[EnvironmentDrift, ...]:
    if tuple(variable.name for variable in left) != tuple(variable.name for variable in right):
        return (EnvironmentDrift.ENVIRONMENT_VARIABLE_MISMATCH,)
    drift: list[EnvironmentDrift] = []
    if any(
        source.worktree_path_reference != current.worktree_path_reference
        for source, current in zip(left, right, strict=True)
    ):
        drift.append(EnvironmentDrift.ABSOLUTE_PATH_MISMATCH)
    if any(
        source.state != current.state or source.sha256 != current.sha256
        for source, current in zip(left, right, strict=True)
    ):
        drift.append(EnvironmentDrift.ENVIRONMENT_VARIABLE_MISMATCH)
    return tuple(drift)


def fingerprint_environment(
    worktree: Path,
    environment_resources: Sequence[str] = (),
    environment_variables: Sequence[str] = (),
    environment_venv_or_cache: Sequence[str] = (),
) -> EnvironmentFingerprint:
    """Fingerprint Git/tool state and only explicitly declared extra inputs."""

    resource_paths = _environment_resource_paths(environment_resources)
    variable_names = _environment_variable_names(environment_variables)
    venv_or_cache_paths = _environment_resource_paths(environment_venv_or_cache)
    snapshot_sha = _git_output(worktree, "rev-parse", "HEAD")
    tree_sha = _git_output(worktree, "rev-parse", "HEAD^{tree}")
    status = _git_output(worktree, "status", "--porcelain=v1", "--untracked-files=all")
    index_diff = _git_output(
        worktree, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"
    )
    tracked_diff = _git_output(
        worktree, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"
    )
    submodules = _git_output(worktree, "submodule", "status", "--recursive", allow_failure=True)
    git_version = _command_version(["git", "--version"])
    declared_resources = _fingerprint_resources(worktree, resource_paths, venv_or_cache_paths)
    declared_environment_variables = _fingerprint_environment_variables(variable_names, worktree)
    status_sha256 = _digest(status.encode())
    index_diff_sha256 = _digest(index_diff.encode())
    tracked_diff_sha256 = _digest(tracked_diff.encode())
    submodule_status_sha256 = _digest(submodules.encode())
    python_version = platform.python_version()
    platform_system = platform.system()
    platform_machine = platform.machine()
    ignored_state = "not_captured_by_git_snapshot"
    undeclared_environment_variables = "not_captured"
    material = {
        "snapshot_sha": snapshot_sha,
        "tree_sha": tree_sha,
        "status_sha256": status_sha256,
        "index_diff_sha256": index_diff_sha256,
        "tracked_diff_sha256": tracked_diff_sha256,
        "submodule_status_sha256": submodule_status_sha256,
        "git_version": git_version,
        "python_version": python_version,
        "platform_system": platform_system,
        "platform_machine": platform_machine,
        "ignored_state": ignored_state,
        "environment_variables": undeclared_environment_variables,
        "declared_resources": [asdict(resource) for resource in declared_resources],
        "declared_environment_variables": [
            asdict(variable) for variable in declared_environment_variables
        ],
    }
    return EnvironmentFingerprint(
        worktree=str(worktree.resolve()),
        snapshot_sha=snapshot_sha,
        tree_sha=tree_sha,
        status_sha256=status_sha256,
        index_diff_sha256=index_diff_sha256,
        tracked_diff_sha256=tracked_diff_sha256,
        submodule_status_sha256=submodule_status_sha256,
        git_version=git_version,
        python_version=python_version,
        platform_system=platform_system,
        platform_machine=platform_machine,
        ignored_state=ignored_state,
        environment_variables=undeclared_environment_variables,
        fingerprint_sha256=_digest(_canonical_json(material)),
        declared_resources=declared_resources,
        declared_environment_variables=declared_environment_variables,
    )


def compare_environments(
    left: EnvironmentFingerprint, right: EnvironmentFingerprint
) -> EnvironmentComparison:
    """Classify captured-state differences; worktree paths intentionally may differ."""

    drift: list[EnvironmentDrift] = []
    if (
        left.snapshot_sha,
        left.tree_sha,
        left.status_sha256,
        left.index_diff_sha256,
        left.tracked_diff_sha256,
    ) != (
        right.snapshot_sha,
        right.tree_sha,
        right.status_sha256,
        right.index_diff_sha256,
        right.tracked_diff_sha256,
    ):
        drift.append(EnvironmentDrift.TRACKED_STATE_MISMATCH)
    if left.submodule_status_sha256 != right.submodule_status_sha256:
        drift.append(EnvironmentDrift.SYMLINK_OR_SUBMODULE_MISMATCH)
    if (left.git_version, left.python_version) != (right.git_version, right.python_version):
        drift.append(EnvironmentDrift.TOOL_VERSION_DRIFT)
    if (
        left.platform_system,
        left.platform_machine,
        left.ignored_state,
        left.environment_variables,
    ) != (
        right.platform_system,
        right.platform_machine,
        right.ignored_state,
        right.environment_variables,
    ):
        drift.append(EnvironmentDrift.UNKNOWN_ENVIRONMENT_DRIFT)
    for category in _declared_resource_drift(left.declared_resources, right.declared_resources):
        if category not in drift:
            drift.append(category)
    for category in _declared_environment_variable_drift(
        left.declared_environment_variables,
        right.declared_environment_variables,
    ):
        if category not in drift:
            drift.append(category)
    return EnvironmentComparison(not drift, tuple(drift))


def fork_rollout(rollout: Path, call_id: str, new_id: str) -> Path:
    """Write a truncated, re-identified copy of a rollout next to the original.

    The copy ends strictly before the branch call so the resumed agent decides
    that step fresh. The original file is never touched.
    """
    lines = rollout.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ReplayError(f"rollout is empty: {rollout}")
    meta = _rollout_record(lines[0], 1)
    payload = meta.get("payload")
    if not isinstance(payload, dict):
        raise ReplayError("rollout has no session_meta payload on line 1")
    old_id = str(payload.get("session_id") or "")
    if not old_id:
        raise ReplayError("rollout has no session_meta session_id on line 1")
    cut = None
    for index, line in enumerate(lines[1:], 1):
        if call_id in _record_ids(_rollout_record(line, index + 1)):
            cut = index
            break
    if cut is None:
        raise ReplayError(f"call_id {call_id} not found in rollout {rollout.name}")
    if cut == 0:
        raise ReplayError("branch point is the first record; nothing to resume from")
    payload["session_id"] = new_id
    if payload.get("id") == old_id:
        payload["id"] = new_id
    forked = [json.dumps(meta), *lines[1:cut]]
    dest = rollout.with_name(rollout.name.replace(old_id, new_id))
    if dest.exists():
        raise ReplayError(f"forked rollout already exists: {dest}")
    dest.write_text("\n".join(forked) + "\n", encoding="utf-8")
    return dest


def fork(
    session_id: str,
    step: int,
    *,
    repo: Path | None = None,
    codex_home: Path | None = None,
    dest: Path | None = None,
    guidance: str | None = None,
    environment_resources: Sequence[str] = (),
    environment_variables: Sequence[str] = (),
    environment_venv_or_cache: Sequence[str] = (),
) -> ForkPlan:
    journal_file = journal_path({"session_id": session_id})
    records = StepJournal.load(journal_file)
    if not 0 <= step < len(records):
        raise ReplayError(f"step {step} out of range (journal has {len(records)} steps)")
    target = records[step]
    if target.event.kind != "tool_proposal":
        raise ReplayError(f"step {step} is {target.event.kind}; fork at a tool_proposal")
    tool_use_id = target.event.payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise ReplayError(f"step {step} has no tool_use_id (journaled before it was recorded)")

    snapshot = _nearest_snapshot(records, step)
    if snapshot is None:
        raise ReplayError(
            f"no snapshot at or before step {step} "
            "(snapshots are taken at apply_patch boundaries; none happened yet)"
        )
    repo_path = repo or _recorded_repo(records, step)
    if repo_path is None:
        raise ReplayError("journal has no cwd for this step; pass repo explicitly")
    resource_paths = _environment_resource_paths(environment_resources)
    variable_names = _environment_variable_names(environment_variables)
    venv_or_cache_paths = _environment_resource_paths(environment_venv_or_cache)
    source_resources = _fingerprint_resources(repo_path, resource_paths, venv_or_cache_paths)
    source_variables = _fingerprint_environment_variables(variable_names, repo_path)

    source_rollout = find_rollout(session_id, codex_home)
    rollout_records = _read_rollout(source_rollout)
    rollout_ids = {
        identifier
        for record in rollout_records
        for identifier in _record_ids(record)
        if isinstance(identifier, str)
    }
    code_mode_ids = _code_mode_call_ids(records, rollout_records)
    call_id = code_mode_ids.get(step) if code_mode_ids is not None else tool_use_id
    if call_id is None:
        raise ReplayError(f"tool_use_id {tool_use_id} has no exact rollout call correlation")
    if call_id not in rollout_ids:
        raise ReplayError(f"tool_use_id {tool_use_id} has no exact rollout call correlation")
    prefix = _build_prefix_manifest(
        session_id,
        step,
        target,
        records,
        repo_path,
        snapshot,
        source_rollout,
        call_id,
    )
    new_id = str(uuid.uuid4())
    worktree = dest or journal_file.parent.parent / "forks" / new_id
    manifest_file = fork_manifest_path(new_id)
    created_at = datetime.now(UTC).isoformat()
    manifest = ForkManifest(
        schema=FORK_MANIFEST_SCHEMA,
        schema_version=FORK_MANIFEST_SCHEMA_VERSION,
        fork_id=new_id,
        status=ForkStatus.CREATING,
        prefix=prefix,
        worktree=str(worktree),
        rollout=None,
        environment=None,
        created_at=created_at,
        updated_at=created_at,
        source_environment_preflight="MATCHED",
    )
    _write_fork_manifest(manifest_file, manifest)

    forked_rollout: Path | None = None
    restored = False
    try:
        forked_rollout = fork_rollout(source_rollout, call_id, new_id)
        restore_snapshot(repo_path, snapshot, worktree)
        restored = True
        environment = fingerprint_environment(
            worktree,
            resource_paths,
            variable_names,
            venv_or_cache_paths,
        )
        environment_drift = tuple(
            dict.fromkeys(
                (
                    *_declared_resource_drift(source_resources, environment.declared_resources),
                    *_declared_environment_variable_drift(
                        source_variables,
                        environment.declared_environment_variables,
                    ),
                )
            )
        )
        source_environment_preflight = (
            "MATCHED"
            if not environment_drift
            else "SOURCE_ENVIRONMENT_MISMATCH:"
            + ",".join(category.value for category in environment_drift)
        )
    except Exception as error:
        if forked_rollout is not None and not restored:
            forked_rollout.unlink(missing_ok=True)
        cleaned, _ = redact_text(str(error))
        failed = ForkManifest(
            schema=FORK_MANIFEST_SCHEMA,
            schema_version=FORK_MANIFEST_SCHEMA_VERSION,
            fork_id=new_id,
            status=ForkStatus.FAILED,
            prefix=prefix,
            worktree=str(worktree),
            rollout=str(forked_rollout) if forked_rollout and restored else None,
            environment=None,
            created_at=created_at,
            updated_at=datetime.now(UTC).isoformat(),
            failure=cleaned[:1000],
            source_environment_preflight="ENVIRONMENT_PREFLIGHT_ERROR",
        )
        with suppress(OSError):
            _write_fork_manifest(manifest_file, failed)
        raise

    ready = ForkManifest(
        schema=FORK_MANIFEST_SCHEMA,
        schema_version=FORK_MANIFEST_SCHEMA_VERSION,
        fork_id=new_id,
        status=ForkStatus.READY,
        prefix=prefix,
        worktree=str(worktree),
        rollout=str(forked_rollout),
        environment=environment,
        created_at=created_at,
        updated_at=datetime.now(UTC).isoformat(),
        source_environment_preflight=source_environment_preflight,
    )
    _write_fork_manifest(manifest_file, ready)

    argv = ["codex", "exec", "-C", str(worktree), "resume", "--json", new_id]
    if guidance:
        argv.append(guidance)
    command = shlex.join(argv)
    return ForkPlan(
        session_id=new_id,
        branch_step=step,
        worktree=str(worktree),
        rollout=str(forked_rollout),
        command=command,
        external_effects=list(prefix.external_effects),
        manifest=str(manifest_file),
        prefix_id=prefix.prefix_id,
        environment_fingerprint=environment.fingerprint_sha256,
        source_environment_preflight=source_environment_preflight,
    )


def plan_to_json(plan: ForkPlan) -> str:
    return json.dumps(asdict(plan), indent=2)


def fork_manifest_path(fork_id: str) -> Path:
    directory = spotter_home() / "fork-manifests"
    secure_dir(directory)
    return directory / f"{fork_id}.json"


def load_fork_manifest(path: Path) -> ForkManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError("manifest is not an object")
        schema = raw.get("schema")
        if "schema" in raw and schema != FORK_MANIFEST_SCHEMA:
            raise ReplayError(f"unsupported fork manifest schema {schema!r} in {path}")
        schema_version = raw.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ReplayError(f"fork manifest schema version is not an integer in {path}")
        if schema_version not in {1, 2, 3, 4, 5, FORK_MANIFEST_SCHEMA_VERSION}:
            raise ReplayError(f"unsupported fork manifest schema in {path}")
        prefix_raw = raw["prefix"]
        prefix = PrefixManifest(
            **{
                **prefix_raw,
                "context_limitations": tuple(prefix_raw["context_limitations"]),
                "external_effects": tuple(prefix_raw["external_effects"]),
            }
        )
        environment_raw = raw.get("environment")
        environment = None
        if isinstance(environment_raw, dict):
            resources = tuple(
                DeclaredResourceFingerprint(**resource)
                for resource in environment_raw.get("declared_resources", [])
            )
            variables = tuple(
                DeclaredEnvironmentVariableFingerprint(**variable)
                for variable in environment_raw.get("declared_environment_variables", [])
            )
            environment = EnvironmentFingerprint(
                **{
                    **environment_raw,
                    "declared_resources": resources,
                    "declared_environment_variables": variables,
                }
            )
        return ForkManifest(
            schema=schema,
            schema_version=schema_version,
            fork_id=raw["fork_id"],
            status=ForkStatus(raw["status"]),
            prefix=prefix,
            worktree=raw["worktree"],
            rollout=raw.get("rollout"),
            environment=environment,
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            failure=raw.get("failure"),
            source_environment_preflight=str(raw.get("source_environment_preflight") or "MATCHED"),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ReplayError(f"invalid fork manifest {path}: {error}") from error


def _build_prefix_manifest(
    session_id: str,
    step: int,
    target: StepRecord,
    records: list[StepRecord],
    repo: Path,
    snapshot: str,
    rollout: Path,
    call_id: str,
) -> PrefixManifest:
    snapshot_tree = _git_output(repo, "rev-parse", f"{snapshot}^{{tree}}")
    common_dir = Path(_git_output(repo, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = repo / common_dir
    repository_id = _digest(str(common_dir.resolve()).encode())
    rollout_digest = _rollout_prefix_digest(rollout, call_id)
    model, runtime_version, agent_config = _rollout_provenance(rollout, call_id)
    identity = target.event.identity
    turn_id = identity.turn_id.value if identity and identity.turn_id else None
    effects = tuple(external_effects(records, through_step=step))
    gaps = sum(record.event.kind == "observation_gap" for record in records[: step + 1])
    identity_material = {
        "source_session_id": session_id,
        "branch_step": step,
        "source_event_id": target.event.event_id,
        "tool_use_id": call_id,
        "repository_id": repository_id,
        "snapshot_sha": snapshot,
        "snapshot_tree_sha": snapshot_tree,
        "rollout_prefix_sha256": rollout_digest,
    }
    return PrefixManifest(
        prefix_id=_digest(_canonical_json(identity_material)),
        source_session_id=session_id,
        branch_step=step,
        source_event_id=target.event.event_id,
        source_turn_id=turn_id,
        connection_epoch=target.event.connection_epoch,
        journal_schema_version=target.version,
        tool_use_id=call_id,
        repository_path=str(repo.resolve()),
        repository_id=repository_id,
        snapshot_sha=snapshot,
        snapshot_tree_sha=snapshot_tree,
        rollout_prefix_sha256=rollout_digest,
        agent="codex",
        model=model,
        runtime_version=runtime_version,
        agent_config=agent_config,
        context_source="truncated_codex_rollout",
        context_limitations=(
            "spotter_private_reviewer_state_not_injected",
            "ignored_files_and_environment_variables_not_restored",
        ),
        external_effects=effects,
        observation_gaps=gaps,
        created_at=datetime.now(UTC).isoformat(),
    )


def _rollout_prefix_digest(rollout: Path, call_id: str) -> str:
    lines = rollout.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if call_id in _record_ids(_rollout_record(line, index + 1)):
            return _digest(("\n".join(lines[:index]) + "\n").encode())
    raise ReplayError(f"call_id {call_id} not found in rollout {rollout.name}")


def _rollout_provenance(rollout: Path, call_id: str) -> tuple[str | None, str | None, str]:
    model = None
    runtime_version = None
    agent_config = "not_captured"
    for index, line in enumerate(rollout.read_text(encoding="utf-8").splitlines()):
        record = _rollout_record(line, index + 1)
        if call_id in _record_ids(record):
            break
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("model"), str):
            model = payload["model"]
        if isinstance(payload.get("cli_version"), str):
            runtime_version = payload["cli_version"]
        if record.get("type") == "turn_context":
            config = {
                key: payload[key]
                for key in ("approval_policy", "effort", "personality")
                if isinstance(payload.get(key), str)
            }
            for key, nested_key in (
                ("collaboration_mode", "mode"),
                ("sandbox_policy", "type"),
            ):
                nested = payload.get(key)
                if isinstance(nested, dict) and isinstance(nested.get(nested_key), str):
                    config[key] = nested[nested_key]
            if config:
                agent_config = _canonical_json(config).decode()
    return model, runtime_version, agent_config


def _write_fork_manifest(path: Path, manifest: ForkManifest) -> None:
    if (
        manifest.schema != FORK_MANIFEST_SCHEMA
        or manifest.schema_version != FORK_MANIFEST_SCHEMA_VERSION
    ):
        raise ReplayError("only the current fork manifest schema may be written")
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a") as lock:
        flock(lock, LOCK_EX)
        if path.exists():
            load_fork_manifest(path)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as sink:
                json.dump(asdict(manifest), sink, indent=2)
                sink.write("\n")
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def _git_output(repo: Path, *args: str, allow_failure: bool = False) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReplayError(f"git {' '.join(args)} failed: {error}") from error
    if result.returncode != 0:
        if allow_failure:
            return f"unavailable:{result.returncode}"
        raise ReplayError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _snapshot_exists(repo: Path, snapshot: str, cache: dict[tuple[Path, str], bool]) -> bool:
    key = (repo.resolve(), snapshot)
    if key not in cache:
        try:
            result = subprocess.run(
                ["git", "cat-file", "-e", f"{snapshot}^{{commit}}"],
                cwd=repo,
                capture_output=True,
                timeout=30,
            )
            cache[key] = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            cache[key] = False
    return cache[key]


def _command_version(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise ReplayError(f"{' '.join(args)} failed: {error}") from error
    if result.returncode != 0:
        raise ReplayError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _rollout_record(line: str, number: int) -> dict[str, object]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        raise ReplayError(f"invalid rollout JSON on line {number}: {error.msg}") from error
    if not isinstance(record, dict):
        raise ReplayError(f"rollout line {number} is not a JSON object")
    return record


def _read_rollout(path: Path) -> list[dict[str, object]]:
    return [
        _rollout_record(line, number)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
    ]


def _code_mode_call_ids(
    records: list[StepRecord], rollout: list[dict[str, object]]
) -> dict[int, str] | None:
    """Map current Code Mode Hook execution ids to model call ids conservatively.

    Codex 0.147 gives Hooks an ``exec-...`` execution id while the persisted
    ``custom_tool_call`` uses a different ``call_...`` id. The two streams are
    ordered, so a complete one-to-one sequence is sufficient to recover the
    exact pre-call cut. Any missing/extra event rejects the mapping instead of
    overstating branch coverage.
    """

    proposals = [record for record in records if record.event.kind == "tool_proposal"]
    call_ids = []
    for record in rollout:
        payload = record.get("payload")
        if (
            isinstance(payload, dict)
            and payload.get("type") == "custom_tool_call"
            and isinstance(payload.get("call_id"), str)
        ):
            call_ids.append(payload["call_id"])
    if not call_ids:
        return None
    if len(proposals) != len(call_ids) or any(
        record.event.payload.get("proposal_number") != number
        for number, record in enumerate(proposals, 1)
    ):
        return {}
    return {record.step: call_id for record, call_id in zip(proposals, call_ids, strict=True)}


def _record_ids(record: dict[str, object]) -> set[object]:
    """Ids under which a tool call appears in a rollout record.

    Older Codex versions surfaced the Hook tool_use_id as event_msg
    payload.item.id. Current Code Mode uses distinct execution/model ids; its
    strict sequence correlation is handled by ``_code_mode_call_ids``.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return set()
    ids: set[object] = {payload.get("call_id")}
    item = payload.get("item")
    if isinstance(item, dict):
        ids.add(item.get("id"))
    ids.discard(None)
    return ids


def _nearest_snapshot(records: list[StepRecord], step: int) -> str | None:
    for record in reversed(records[: step + 1]):
        if record.snapshot:
            return record.snapshot
    return None


def _recorded_repo(records: list[StepRecord], step: int) -> Path | None:
    for record in reversed(records[: step + 1]):
        cwd = record.event.payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            return Path(cwd)
    return None
