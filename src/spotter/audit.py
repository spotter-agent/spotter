"""Independent, compact evidence state maintained by Spotter."""

from __future__ import annotations

from dataclasses import dataclass, field

from spotter.trace import TraceEvent


@dataclass(frozen=True)
class AuditSnapshot:
    """Read-only reviewer view, preventing a reviewer from rewriting audit evidence."""

    user_goal: str | None
    constraints: frozenset[str]
    hypotheses: tuple[str, ...]
    touched_files: frozenset[str]
    validation_status: str
    recent_failures: tuple[str, ...]
    consecutive_failures: int


@dataclass
class AuditState:
    """Facts observed from events; agent claims are retained only as hypotheses."""

    user_goal: str | None = None
    constraints: set[str] = field(default_factory=set)
    hypotheses: list[str] = field(default_factory=list)
    touched_files: set[str] = field(default_factory=set)
    validation_status: str = "unknown"
    recent_failures: list[str] = field(default_factory=list)
    consecutive_failures: int = 0

    def update(self, event: TraceEvent) -> None:
        if event.kind == "user_goal":
            self.user_goal = event.intent or _string_payload(event, "goal")
        self.constraints.update(event.constraints)
        self.touched_files.update(event.files)

        hypothesis = _string_payload(event, "hypothesis")
        if hypothesis and hypothesis not in self.hypotheses:
            self.hypotheses = [*self.hypotheses, hypothesis][-5:]
        if event.validation:
            self.validation_status = event.validation

        if event.is_failed_result:
            self.consecutive_failures += 1
            self.recent_failures = [*self.recent_failures, event.result or "failure"][-5:]
        elif event.is_result:
            self.consecutive_failures = 0

    def snapshot(self) -> AuditSnapshot:
        return AuditSnapshot(
            user_goal=self.user_goal,
            constraints=frozenset(self.constraints),
            hypotheses=tuple(self.hypotheses),
            touched_files=frozenset(self.touched_files),
            validation_status=self.validation_status,
            recent_failures=tuple(self.recent_failures),
            consecutive_failures=self.consecutive_failures,
        )


def _string_payload(event: TraceEvent, key: str) -> str | None:
    value = event.payload.get(key)
    return value if isinstance(value, str) and value else None
