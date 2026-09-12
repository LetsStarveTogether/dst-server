import logging
import math
import sys
from functools import partial
from io import StringIO
from unittest.mock import AsyncMock

import logbook
import orjson
import pytest
from rich.console import Console
from rich.text import Text

from dst_server.cli import common, main
from dst_server.cli.common import Options, emit, options
from dst_server.cli.output import diagnostic, format_log, single_line
from dst_server.host import Host


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


@pytest.fixture(params=[None, "standard"])
def terminal_console(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        common,
        "Console",
        partial(
            Console,
            force_terminal=True,
            color_system=request.param,
            no_color=False,
            width=120,
        ),
    )


@pytest.mark.usefixtures("terminal_console")
@pytest.mark.parametrize("json_output", [False, True])
def test_json_output_preserves_unicode_and_pydantic_nonfinite_values(
    json_output: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = options.set(Options(json=json_output))
    try:
        value = {
            "nested": ['玩家👩🏽‍💻\n\t"' * 100],
            "finite": 1.5,
            "nonfinite": [math.nan, math.inf, -math.inf],
        }
        emit(value)
        output = capsys.readouterr()
        text = output.out if json_output else Text.from_ansi(output.out).plain
        assert orjson.loads(text) == value | {"nonfinite": [None, None, None]}
        assert "玩家" in text
        if json_output:
            assert "\x1b" not in output.out
            assert output.out.count("\n") == 1
        assert output.err == ""
    finally:
        options.reset(token)


@pytest.mark.usefixtures("terminal_console")
@pytest.mark.parametrize("table", [False, True])
def test_human_output_preserves_literal_markup(
    table: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = "player[/] [red]Alice[/red]"
    token = options.set(Options())
    try:
        emit([{message: message}] if table else message)
    finally:
        options.reset(token)
    output = capsys.readouterr()
    assert message in Text.from_ansi(output.out).plain
    assert output.err == ""


@pytest.mark.usefixtures("terminal_console")
@pytest.mark.parametrize("recycle", [False, True])
@pytest.mark.parametrize(
    ("terminal", "as_json"), [(False, False), (True, False), (True, True)]
)
def test_automation_results_follow_cli_output_policy(
    cli_host: Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    recycle: bool,
    terminal: bool,
    as_json: bool,
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: terminal)
    results = {
        str(number): {"status": "skipped" if recycle else "unchanged"}
        for number in range(140)
    }
    operation = AsyncMock(return_value=results)
    monkeypatch.setattr(
        "dst_server.host.recycling.run_recycle"
        if recycle
        else "dst_server.host.schedule.run_schedule",
        operation,
    )
    assert (
        main([
            *(["--json"] if as_json else []),
            *(["maintenance", "recycle"] if recycle else ["schedule", "run"]),
        ])
        == 0
    )
    operation.assert_awaited_once()
    assert operation.await_args is not None
    assert operation.await_args.args[0].cluster_root == cli_host.cluster_root
    output = capsys.readouterr()
    if as_json or not terminal:
        assert orjson.loads(output.out) == results
        assert "\x1b" not in output.out
        assert output.out.count("\n") == 1
    else:
        assert orjson.loads(Text.from_ansi(output.out).plain) == results
    assert output.err == ""
