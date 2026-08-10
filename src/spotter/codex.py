"""Codex event normalization kept outside the supervision core."""

from dataclasses import dataclass, field

from spotter.trace import TraceEvent


@dataclass
class CodexAdapter:
    """In-memory sink and translator for Codex App Server-like event objects."""

    events: list[TraceEvent] = field(default_factory=list)

    def record(self, event: TraceEvent) -> None:
        self.events.append(event)

    def normalize(self, raw: dict[str, object], step: int) -> TraceEvent:
        """Normalize the useful public fields without requiring chain-of-thought."""

        kind = str(raw.get("type", raw.get("kind", "unknown")))
        files_value = raw.get("files", ())
        files = tuple(str(path) for path in files_value) if isinstance(files_value, list) else ()
        constraints_value = raw.get("constraints", ())
        constraints = (
            tuple(str(item) for item in constraints_value)
            if isinstance(constraints_value, list)
            else ()
        )
        payload = raw.get("payload", {})
        return TraceEvent(
            step=step,
            kind=kind,
            intent=_optional_text(raw.get("intent")),
            operation=_optional_text(raw.get("operation") or raw.get("command")),
            files=files,
            result=_optional_text(raw.get("result") or raw.get("output_summary")),
            validation=_optional_text(raw.get("validation")),
            constraints=constraints,
            payload=dict(payload) if isinstance(payload, dict) else {},
        )


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None
