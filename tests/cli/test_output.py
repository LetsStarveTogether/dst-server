import logging
import sys
from io import StringIO
from unittest.mock import AsyncMock

import logbook
import pytest

from dst_server.cli import main
from dst_server.cli.output import diagnostic, format_log, single_line


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("房间 000 skipped", "房间 000 skipped"),
        ("subprocess output\r\n", "subprocess output"),
        ("first\rsecond\nthird\0last\n", r"first\rsecond\nthird\0last"),
        ("", ""),
    ],
)
def test_single_line_preserves_text_and_escapes_record_separators(
    message: str, expected: str
) -> None:
    assert single_line(message) == expected


def test_log_formatter_keeps_long_records_on_one_line() -> None:
    stream = StringIO()
    handler = logbook.StreamHandler(stream)
    handler.formatter = format_log
    message = "玩家房间 [red]000[/red] " * 100
    with handler.applicationbound():
        logbook.Logger("test").info("{}\r\n", message)
    assert stream.getvalue() == message + "\n"


@pytest.mark.parametrize("stdlib", [False, True])
def test_cli_preserves_tracebacks_in_one_record_and_restores_logging(
    stdlib: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dst_server.cluster import service

    original_handlers = logging.root.handlers[:]
    original_level = logging.root.level
    logger = logging.getLogger(__name__) if stdlib else logbook.Logger(__name__)

    def fail() -> None:
        message = "房间 000\nfailed\0reason"
        raise RuntimeError(message)

    def prepare(**_: object) -> None:
        try:
            fail()
        except RuntimeError:
            logger.exception("prepare\nfailed")

    monkeypatch.setattr(service, "prepare_shared", AsyncMock(side_effect=prepare))
    with logbook.TestHandler().applicationbound() as outer:
        assert main(["agent", "prepare"]) == 0
        output = capsys.readouterr()
        assert output.out == ""
        assert output.err.count("\n") == 1
        assert output.err.startswith(r"prepare\nfailed\nTraceback")
        assert "test_output.py" in output.err
        assert r"RuntimeError: 房间 000\nfailed\0reason" in output.err
        assert "\0" not in output.err
        assert not outer.records
        logbook.Logger(__name__).warning("outside CLI")
        assert [record.message for record in outer.records] == ["outside CLI"]
    assert logging.root.handlers == original_handlers
    assert logging.root.level == original_level


@pytest.mark.parametrize("tty", [False, True])
def test_diagnostics_preserve_text_without_automatic_wrapping(
    tty: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys.stderr, "isatty", lambda: tty)
    monkeypatch.setenv("COLUMNS", "20")
    message = "玩家 [red]000[/red] " * 100 + "\nnext"
    diagnostic(message)
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (message if tty else single_line(message)) + "\n"
