"""Shared classification for runtime outcome payloads."""

import re
from collections.abc import Mapping

_EXIT_CODE_TEXT = re.compile(r"(?im)^Exit code: (-?\d+)\s*$")
_FAILED_STATUSES = {"cancelled", "declined", "error", "failed", "interrupted"}
_SUCCEEDED_STATUSES = {"completed", "passed", "succeeded", "success"}


def outcome_failure(payload: Mapping[str, object]) -> bool | None:
    """Return failure, success, or unknown for any normalized runtime outcome."""

    response = payload.get("tool_response")
    signals: list[bool] = []

    if isinstance(response, Mapping):
        _append_exit_code(signals, response.get("exit_code"))
        _append_success(signals, response.get("ok"))
        _append_status(signals, response.get("status"))
    elif isinstance(response, str) and (match := _EXIT_CODE_TEXT.search(response)):
        signals.append(int(match.group(1)) != 0)

    _append_exit_code(signals, payload.get("exitCode", payload.get("exit_code")))
    _append_success(signals, payload.get("success"))
    _append_status(signals, payload.get("status"))

    if any(signals):
        return True
    return False if signals else None


def _append_exit_code(signals: list[bool], value: object) -> None:
    if isinstance(value, int) and not isinstance(value, bool):
        signals.append(value != 0)


def _append_success(signals: list[bool], value: object) -> None:
    if isinstance(value, bool):
        signals.append(not value)


def _append_status(signals: list[bool], value: object) -> None:
    if not isinstance(value, str):
        return
    if value in _FAILED_STATUSES:
        signals.append(True)
    elif value in _SUCCEEDED_STATUSES:
        signals.append(False)
