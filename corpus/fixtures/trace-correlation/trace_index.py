from dataclasses import dataclass


class TraceConflict(ValueError):
    pass


@dataclass(frozen=True)
class Event:
    event_id: str
    epoch: int
    item_id: str
    kind: str
    timestamp: int
    outcome: str | None = None


@dataclass(frozen=True)
class Action:
    epoch: int
    item_id: str
    observed_start: bool
    outcome: str | None
    last_timestamp: int
    out_of_order: bool


class TraceIndex:
    def __init__(self) -> None:
        self.actions: dict[str, Action] = {}

    def ingest(self, event: Event) -> Action:
        action = Action(
            event.epoch,
            event.item_id,
            event.kind == "start",
            event.outcome,
            event.timestamp,
            False,
        )
        self.actions[event.item_id] = action
        return action
