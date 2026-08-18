from collections.abc import Callable

from workflow import Result, Task, run


def rows(results: list[Result]) -> list[tuple[str, str]]:
    return [(result.name, result.status) for result in results]


def rejects(tasks: tuple[Task, ...], message: str) -> None:
    calls: list[str] = []
    try:
        run(tasks, lambda name: calls.append(name) is None)
    except ValueError as error:
        assert message in str(error)
    else:
        raise AssertionError("invalid workflow should be rejected")
    assert calls == [], "the graph must be validated before execution"


tasks = (
    Task("deploy", ("build",)),
    Task("lint"),
    Task("build"),
    Task("test", ("build",)),
)
original = tasks
calls: list[str] = []


def succeeds(name: str) -> bool:
    calls.append(name)
    return True


assert rows(run(tasks, succeeds)) == [
    ("lint", "passed"),
    ("build", "passed"),
    ("deploy", "passed"),
    ("test", "passed"),
]
assert calls == ["lint", "build", "deploy", "test"]
assert tasks == original

failure_tasks = (
    Task("release", ("package",)),
    Task("docs"),
    Task("package"),
    Task("smoke", ("release",)),
)
failure_calls: list[str] = []


def package_fails(name: str) -> bool:
    failure_calls.append(name)
    return name != "package"


assert rows(run(failure_tasks, package_fails)) == [
    ("docs", "passed"),
    ("package", "failed"),
    ("release", "blocked"),
    ("smoke", "blocked"),
]
assert failure_calls == ["docs", "package"]

rejects((Task("a", ("missing",)),), "missing dependency")
rejects((Task("a", ("b",)), Task("b", ("a",)), Task("free")), "cycle")
rejects((Task("same"), Task("same")), "duplicate task")
