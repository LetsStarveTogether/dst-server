import asyncio
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import orjson
import pytest

from dst_server.logs import _process
from dst_server.logs.journal import JournalCursorError, JournalLogs, JournalQuery


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
            sys.executable, str(executable), *command[1:], **kwargs
        )
        processes.append(process)
        return process

    def write(code: str) -> None:
        executable.write_text(code, encoding="utf-8")

    monkeypatch.setattr(_process, "create_subprocess_exec", launch)
    return write, commands, processes


def entry(cursor: str, **fields: Any) -> dict[str, Any]:
    return {
        "__CURSOR": cursor,
        "__REALTIME_TIMESTAMP": "1000001",
        "MESSAGE": cursor,
        **fields,
    }


async def test_query_preserves_fields_and_success_diagnostics(
    journal_process: tuple,
) -> None:
    write, commands, processes = journal_process
    entries = [
        entry("first", MESSAGE="x" * 70000, PRIORITY="4", _BOOT_ID="boot"),
        entry(
            "second",
            UNIT="dst-007-cave.service",
            _SYSTEMD_UNIT="init.scope",
            MESSAGE=[104, 105, 0, 255],
            CONTAINER_ID="container",
            EXTRA=["first", [115, 101, 99, 111, 110, 100]],
        ),
    ]
    write(
        "import orjson, sys\n"
        f"for record in {entries!r}: print(orjson.dumps(record).decode())\n"
        "sys.stderr.write('x' * 100000 + ' partial journal access')\n"
    )
    result = await JournalLogs().query(
        ("dst-007-pod.service", "dst-007-*.service"),
        JournalQuery(
            since=datetime(2026, 9, 12, 8, tzinfo=timezone(timedelta(hours=8))),
            until="now",
            namespace="dst",
        ),
    )
    assert len(commands) == 1
    command = commands[0]
    assert "--lines=101" in command
    assert "--reverse" in command
    assert "--all" in command
    assert "--no-pager" in command
    assert "--since=2026-09-12 00:00:00.000000 UTC" in command
    assert "--until=now" in command
    assert "--namespace=dst" in command
    assert "--unit=dst-007-*.service" in command
    assert not any(argument.startswith("--output-fields") for argument in command)
    assert not any(argument.startswith("--boot") for argument in command)
    assert result.records[0].fields == entries[0]
    assert result.records[1].fields == entries[1]
    assert result.records[0].message == "x" * 70000
    assert result.records[0].timestamp == datetime(1970, 1, 1, 0, 0, 1, 1, tzinfo=UTC)
    assert result.records[1].message == "hi\0\ufffd"
    assert result.records[1].unit == "dst-007-cave.service"
    serialized = result.records[1].model_dump(mode="json")
    assert serialized["fields"] == entries[1]
    assert serialized["cursor"] == "second"
    assert serialized["message"] == "hi\0\ufffd"
    assert result.next_cursor == "second"
    assert not result.has_more
    assert result.diagnostics.endswith("partial journal access")
    assert result.diagnostics_truncated
    assert len(result.diagnostics) == 64 * 1024
    assert processes[0].returncode == 0


@pytest.mark.parametrize("direction", ["forward", "backward"])
async def test_cursor_pages_preserve_equal_timestamps_and_lookahead(
    journal_process: tuple, direction: Literal["forward", "backward"]
) -> None:
    write, commands, processes = journal_process
    entries = [entry(str(index)) for index in range(7)]
    write(
        "import orjson, sys\n"
        f"records = {entries!r}\n"
        "options = dict(arg.split('=', 1) for arg in sys.argv[1:] if '=' in arg)\n"
        "if '--reverse' in sys.argv: records.reverse()\n"
        "if '--cursor' in options:\n"
        "    index = next(i for i, item in enumerate(records) "
        "if item['__CURSOR'] == options['--cursor'])\n"
        "    records = records[index:]\n"
        "for record in records[:int(options['--lines'])]: "
        "print(orjson.dumps(record).decode())\n"
    )
    reader = JournalLogs()
    cursor = None
    delivered: list[str] = []
    while True:
        result = await reader.query(
            ("dst-007-forest.service",),
            JournalQuery(limit=2, direction=direction, cursor=cursor),
        )
        delivered.extend(record.cursor for record in result.records)
        assert len(result.records) <= 2
        assert result.next_cursor == result.records[-1].cursor
        if not result.has_more:
            break
        cursor = result.next_cursor
    expected = [str(index) for index in range(7)]
    assert delivered == (expected if direction == "forward" else expected[::-1])
    assert len(commands) == 4
    assert all(process.returncode == 0 for process in processes)
    if direction == "forward":
        assert "--lines=+3" in commands[0]
    else:
        assert "--reverse" in commands[0]


@pytest.mark.parametrize("records", [[], [entry("retained")]])
async def test_missing_cursor_fails_without_silently_resuming(
    journal_process: tuple, records: list[dict]
) -> None:
    write, _, processes = journal_process
    write(
        "import orjson\n"
        f"for record in {records!r}: print(orjson.dumps(record).decode())\n"
    )
    with pytest.raises(JournalCursorError) as failure:
        await JournalLogs().query(
            ("dst-007-forest.service",), JournalQuery(cursor="missing")
        )
    assert failure.value.cursor == "missing"
    assert processes[0].returncode is not None


@pytest.mark.parametrize("records", [[], [entry("present")]])
async def test_zero_limit_reports_whether_records_exist(
    journal_process: tuple, records: list[dict]
) -> None:
    write, commands, _ = journal_process
    write(
        "import orjson\n"
        f"for record in {records!r}: print(orjson.dumps(record).decode())\n"
    )
    result = await JournalLogs().query(
        ("dst-007-forest.service",), JournalQuery(limit=0)
    )
    assert result.records == ()
    assert result.next_cursor is None
    assert result.has_more == bool(records)
    assert "--lines=1" in commands[0]


@pytest.mark.parametrize(
    ("cancel", "ignore_term"), [(False, False), (True, False), (False, True)]
)
async def test_follow_close_and_cancellation_reap_reader(
    journal_process: tuple, cancel: bool, ignore_term: bool
) -> None:
    write, commands, processes = journal_process
    write(
        "import signal, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "sys.stderr.write('reader warning\\n'); sys.stderr.flush()\n"
        + f"print({orjson.dumps(entry('ready')).decode()!r}, flush=True)\n"
        "time.sleep(60)\n"
    )
    async with JournalLogs().follow(("dst-007-forest.service",)) as stream:
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
    assert "--lines=0" in commands[0]
    assert processes[0].returncode is not None
    assert stream.diagnostics == "reader warning"
    assert not stream.diagnostics_truncated


async def test_follow_cursor_drops_anchor_and_keeps_resumed_history(
    journal_process: tuple,
) -> None:
    write, commands, _ = journal_process
    records = [entry(str(index)) for index in range(5)]
    write(
        "import orjson\n"
        f"for record in {records!r}: print(orjson.dumps(record).decode())\n"
    )
    async with JournalLogs().follow(
        ("dst-007-forest.service",),
        JournalQuery(direction="forward", cursor="0", limit=1),
    ) as stream:
        delivered = [record.cursor async for record in stream]
    assert delivered == ["1", "2", "3", "4"]
    assert "--cursor=0" in commands[0]
    assert "--follow" in commands[0]


async def test_cancelled_cursor_wait_keeps_anchor_validation(
    journal_process: tuple,
) -> None:
    write, _, _ = journal_process
    records = [entry("anchor"), entry("next")]
    write(
        "import orjson, time\ntime.sleep(0.1)\n"
        f"for record in {records!r}: print(orjson.dumps(record).decode())\n"
    )
    async with JournalLogs().follow(
        ("dst-007-forest.service",),
        JournalQuery(direction="forward", cursor="anchor"),
    ) as stream:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await anext(stream)
        assert [record.cursor async for record in stream] == ["next"]


async def test_reader_error_preserves_bounded_diagnostics(
    journal_process: tuple,
) -> None:
    write, _, processes = journal_process
    write(
        "import sys\nsys.stderr.write('x' * 100000 + ' permission denied')\n"
        "sys.exit(3)\n"
    )
    with pytest.raises(_process.LogProcessError, match="status 3") as failure:
        await JournalLogs().query(("dst-007-forest.service",))
    assert failure.value.returncode == 3
    assert failure.value.diagnostics.endswith("permission denied")
    assert failure.value.diagnostics_truncated
    assert len(failure.value.diagnostics) == 64 * 1024
    assert processes[0].returncode == 3


@pytest.mark.parametrize(
    "record",
    [
        "invalid JSON",
        "[]",
        "{}",
        orjson.dumps(entry("bad", MESSAGE=[256])).decode(),
        orjson.dumps(entry("bad", MESSAGE={"unexpected": "object"})).decode(),
        orjson.dumps(entry("bad", __REALTIME_TIMESTAMP="-1")).decode(),
        orjson.dumps(entry("bad", __REALTIME_TIMESTAMP="9" * 30)).decode(),
        orjson.dumps(entry("bad", __CURSOR=None)).decode(),
    ],
)
async def test_malformed_records_close_reader(
    journal_process: tuple, record: str
) -> None:
    write, _, processes = journal_process
    write(f"import time\nprint({record!r}, flush=True)\ntime.sleep(60)\n")
    with pytest.raises(ValueError, match="invalid journalctl JSON record"):
        await JournalLogs().query(("dst-007-forest.service",))
    assert processes[0].returncode is not None


@pytest.mark.parametrize(
    ("options", "records", "error"),
    [
        ({"max_record_bytes": 100}, [entry("large", MESSAGE="x" * 200)], "record"),
        ({"max_output_bytes": 100}, [entry("first"), entry("second")], "query"),
    ],
)
async def test_query_byte_limits_reap_process(
    journal_process: tuple, options: dict, records: list[dict], error: str
) -> None:
    write, _, processes = journal_process
    write(
        "import orjson, time\n"
        f"for record in {records!r}: print(orjson.dumps(record).decode(), flush=True)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(ValueError, match=f"log {error} exceeds"):
        await JournalLogs(**options).query(("dst-007-forest.service",))
    assert processes[0].returncode is not None


async def test_query_timeout_reaps_process(journal_process: tuple) -> None:
    write, _, processes = journal_process
    write("import time\ntime.sleep(60)\n")
    with pytest.raises(TimeoutError):
        await JournalLogs().query(("dst-007-forest.service",), completion_timeout=0.1)
    assert processes[0].returncode is not None


@pytest.mark.parametrize(
    ("units", "options"),
    [
        ([], {}),
        ("dst-007-forest.service", {}),
        (("--bad-option",), {}),
        (("bad\0unit",), {}),
        (("bad\nunit",), {}),
        (("../bad.service",), {}),
        (("",), {}),
        (("dst-007-forest.service",), {"limit": -1}),
        (("dst-007-forest.service",), {"limit": True}),
        (("dst-007-forest.service",), {"since": ""}),
        (
            ("dst-007-forest.service",),
            {"since": datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)},
        ),
        (("dst-007-forest.service",), {"cursor": "bad\0cursor"}),
        (
            ("dst-007-forest.service",),
            {"cursor": "previous", "since": "yesterday"},
        ),
    ],
)
async def test_invalid_queries_never_start_reader(
    journal_process: tuple, units: Any, options: dict
) -> None:
    _, commands, _ = journal_process
    with pytest.raises(ValueError, match=r"journal|validation"):
        await JournalLogs().query(units, JournalQuery(**options))
    assert commands == []


async def test_follow_rejects_backward_query(journal_process: tuple) -> None:
    _, commands, _ = journal_process
    with pytest.raises(ValueError, match="forward direction"):
        async with JournalLogs().follow(("dst-007-forest.service",), JournalQuery()):
            pytest.fail("invalid follow started")
    assert commands == []
