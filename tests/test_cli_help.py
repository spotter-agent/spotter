import pytest

from spotter.cli import main


@pytest.mark.parametrize(
    ("arguments", "usage", "expected"),
    [
        (["--help"], "usage: spotter COMMAND [OPTIONS]", "Get started:"),
        (["setup", "codex", "--help"], "usage: spotter setup codex", "--local"),
        (["mode", "--help"], "usage: spotter mode", "advisory"),
        (["status", "--help"], "usage: spotter status", "--session THREAD_ID"),
        (["doctor", "-h"], "usage: spotter doctor", "does not execute commands"),
        (["codex", "--help"], "usage: spotter codex", "live observation"),
        (["daemon", "--help"], "usage: spotter daemon", "start|stop|restart"),
    ],
)
def test_common_command_help_is_scoped(
    arguments: list[str],
    usage: str,
    expected: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(arguments) == 0
    output = capsys.readouterr().out
    assert usage in output
    assert expected in output
    assert "--wrong-nudge-id" not in output
