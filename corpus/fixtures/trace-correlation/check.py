from trace_index import Action, Event, TraceConflict, TraceIndex


def rejects(index: TraceIndex, event: Event, message: str) -> None:
    before = dict(index.actions)
    try:
        index.ingest(event)
    except TraceConflict as error:
        assert message in str(error)
    else:
        raise AssertionError("conflicting event should be rejected")
    assert index.actions == before


index = TraceIndex()
started = index.ingest(Event("start-1", 1, "item", "start", 10))
assert started == Action(1, "item", True, None, 10, False)
completed = index.ingest(Event("done-1", 1, "item", "terminal", 20, "succeeded"))
assert completed == Action(1, "item", True, "succeeded", 20, False)
assert index.ingest(Event("done-1", 1, "item", "terminal", 20, "succeeded")) == completed

late = TraceIndex()
terminal_first = late.ingest(Event("done-2", 3, "late", "terminal", 20, "failed"))
assert terminal_first == Action(3, "late", False, "failed", 20, False)
start_late = late.ingest(Event("start-2", 3, "late", "start", 10))
assert start_late == Action(3, "late", True, "failed", 20, True)

other_epoch = late.ingest(Event("start-3", 4, "late", "start", 1))
assert other_epoch == Action(4, "late", True, None, 1, False)
assert late.actions[(3, "late")] == start_late
assert late.actions[(4, "late")] == other_epoch

rejects(index, Event("done-3", 1, "item", "terminal", 30, "failed"), "terminal outcome")
rejects(index, Event("done-1", 1, "other", "terminal", 20, "succeeded"), "event_id")
rejects(index, Event("unknown", 1, "item", "mystery", 40), "kind")
rejects(index, Event("empty", 1, "item", "terminal", 40), "outcome")
