import json
from dataclasses import dataclass
from pathlib import Path


class LedgerError(ValueError):
    pass


@dataclass(frozen=True)
class ReadResult:
    records: tuple[dict[str, object], ...]
    repair_offset: int


def read_ledger(path: Path) -> ReadResult:
    records = tuple(json.loads(line) for line in path.read_text().splitlines() if line)
    return ReadResult(records, path.stat().st_size)
