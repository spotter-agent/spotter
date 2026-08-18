from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    name: str
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class Result:
    name: str
    status: str


def run(tasks: Sequence[Task], execute: Callable[[str], bool]) -> list[Result]:
    return [Result(task.name, "passed" if execute(task.name) else "failed") for task in tasks]
