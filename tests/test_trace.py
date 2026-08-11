from spotter.trace import Segment, TraceEvent, segment_events


def test_segments_aggregate_consecutive_roles() -> None:
    events = [
        TraceEvent("reasoning_summary"),
        TraceEvent("file_read"),
        TraceEvent("search"),
        TraceEvent("patch"),
        TraceEvent("test_result"),
    ]
    assert segment_events(events) == [
        Segment("hypothesis", 0, 0),
        Segment("evidence", 1, 2),
        Segment("commit", 3, 3),
        Segment("validation", 4, 4),
    ]


def test_unknown_kinds_fold_into_surrounding_segment() -> None:
    events = [
        TraceEvent("file_read"),
        TraceEvent("some_new_codex_event"),  # runtimes will grow kinds we don't know
        TraceEvent("tool_result"),
    ]
    assert segment_events(events) == [Segment("evidence", 0, 2)]


def test_only_unknown_kinds_yield_no_segments() -> None:
    assert segment_events([TraceEvent("mystery")]) == []
