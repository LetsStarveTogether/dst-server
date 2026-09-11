import asyncio
import json
import sys
from collections.abc import Callable
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dst_server.host import logs as journal


@pytest.fixture
def journal_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[
    Callable[[str], None], list[tuple[str, ...]], list[asyncio.subprocess.Process]
]:
    executable = tmp_path / "journalctl.py"
    commands: list[tuple[str, ...]] = []
    processes: list[asyncio.subprocess.Process] = []

    async def launch(*command: str, **kwargs: Any) -> asyncio.subprocess.Process:
        commands.append(command)
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(executable), **kwargs
        )
        processes.append(process)
        return process

    def write(code: str) -> None:
        executable.write_text(code, encoding="utf-8")

    monkeypatch.setattr(journal, "create_subprocess_exec", launch)
    return write, commands, processes


async def test_history_filters_preserve_large_and_binary_messages(
    journal_process: tuple,
) -> None:
    write, commands, processes = journal_process
    entries = [
        {
            "__CURSOR": "first",
            "__REALTIME_TIMESTAMP": "1000001",
            "_SYSTEMD_UNIT": "dst-000-forest.service",
            "MESSAGE": "x" * 70000,
        },
        {
            "__CURSOR": "second",
            "__REALTIME_TIMESTAMP": "2000002",
            "UNIT": "dst-000-cave.service",
            "_SYSTEMD_UNIT": "init.scope",
            "MESSAGE": [104, 105, 0, 255],
        },
        {
            "__CURSOR": "third",
            "__REALTIME_TIMESTAMP": "3000003",
            "MESSAGE": ["first", [115, 101, 99, 111, 110, 100]],
        },
    ]
    write(f"import json\nfor entry in {entries!r}: print(json.dumps(entry))\n")

    records = [
        record
        async for record in journal.logs(
            ["dst-000-forest.service", "dst-000-cave.service"],
            since="yesterday",
            until="now",
            after_cursor="previous",
        )
    ]

    assert len(commands) == 1
    command = commands[0]
    assert "--lines=100" in command
    assert "--all" in command
    assert "--no-pager" in command
    assert "--since=yesterday" in command
    assert "--until=now" in command
    assert "--after-cursor=previous" in command
    assert "--unit=dst-000-forest.service" in command
    assert "--unit=dst-000-cave.service" in command
    assert not any(argument.startswith("--boot") for argument in command)
    assert "--follow" not in command
    assert records[0].message == "x" * 70000
    assert records[0].timestamp == datetime(1970, 1, 1, 0, 0, 1, 1, tzinfo=UTC)
    assert records[1].message == "hi\0\ufffd"
    assert records[1].unit == "dst-000-cave.service"
    assert records[2].message == "first\nsecond"
    assert records[2].unit == ""
    assert processes[0].returncode == 0


@pytest.mark.parametrize(
    ("cancel", "ignore_term"), [(False, False), (True, False), (False, True)]
)
async def test_follow_close_and_cancellation_reap_the_reader(
    journal_process: tuple, cancel: bool, ignore_term: bool
) -> None:
    write, commands, processes = journal_process
    entry = json.dumps({
        "__CURSOR": "first",
        "__REALTIME_TIMESTAMP": "1",
        "MESSAGE": "ready",
    })
    write(
        "import signal, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + f"print({entry!r}, flush=True)\ntime.sleep(60)\n"
    )

    async with aclosing(
        journal.logs(["dst-000-forest.service"], follow=True)
    ) as stream:
        record = await anext(stream)
        assert record.message == "ready"
        if cancel:
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

    assert len(processes) == 1
    assert "--follow" in commands[0]
    assert processes[0].returncode is not None


async def test_reader_error_drains_stderr_and_reports_its_bounded_tail(
    journal_process: tuple,
) -> None:
    write, _, processes = journal_process
    write(
        "import sys\nsys.stderr.write('x' * 100000 + ' permission denied')\n"
        "sys.exit(3)\n"
    )

    with pytest.raises(RuntimeError, match=r"status 3:.*permission denied") as failure:
        _ = [record async for record in journal.logs(["dst-000-forest.service"])]

    assert len(str(failure.value)) < 8300
    assert processes[0].returncode == 3


async def test_malformed_record_closes_the_reader(journal_process: tuple) -> None:
    write, _, processes = journal_process
    write("import time\nprint('{}', flush=True)\ntime.sleep(60)\n")

    with pytest.raises(ValueError, match="invalid journalctl JSON record"):
        _ = [record async for record in journal.logs(["dst-000-forest.service"])]

    assert processes[0].returncode is not None


async def test_all_history_is_explicit_and_has_no_record_count_limit(
    journal_process: tuple,
) -> None:
    write, commands, processes = journal_process
    write("pass\n")
    assert [
        record async for record in journal.logs(["dst-task.service"], lines=None)
    ] == []
    assert "--lines=all" in commands[0]
    assert processes[0].returncode == 0


@pytest.mark.parametrize(
    ("units", "options"),
    [
        ([], {}),
        ("dst-000-forest.service", {}),
        (["*.service"], {}),
        (["dst-000-forest.service"], {"lines": -1}),
        (["dst-000-forest.service"], {"lines": True}),
        (["dst-000-forest.service"], {"since": ""}),
        (["dst-000-forest.service"], {"after_cursor": "bad\0cursor"}),
    ],
)
async def test_invalid_queries_do_not_start_a_reader(
    journal_process: tuple, units: Any, options: dict
) -> None:
    _, commands, _ = journal_process

    with pytest.raises(ValueError, match=r"journal|unsafe Quadlet"):
        _ = [record async for record in journal.logs(units, **options)]

    assert commands == []
