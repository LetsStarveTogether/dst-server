"""Run with the isolated wheel interpreter to verify native log ownership."""

# These stdlib checks run in the dependency-free wheel environment.
# ruff: file-ignore[pytest-assert-in-except]

import asyncio
import gc
import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from dst_server import _native
from dst_server.logs import (
    JournalLogs,
    JournalQuery,
    LogProcessError,
    NetdataLogQuery,
    NetdataLogs,
)


def executable(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        "path = pathlib.Path(sys.argv[0])\n"
        "path.with_suffix('.pid').write_text(str(os.getpid()))\n"
        "path.with_suffix('.args').write_text(json.dumps(sys.argv[1:]))\n" + body,
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


async def reaped(pid: int) -> None:
    async with asyncio.timeout(5):
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
    try:
        os.waitpid(pid, os.WNOHANG)  # ruff: ignore[wait-for-process-in-async-function]
    except ChildProcessError:
        return
    msg = "native reader left a child for Python to reap"
    raise AssertionError(msg)


JOURNAL = r"""
args = dict(arg.split('=', 1) for arg in sys.argv[1:] if '=' in arg)
if args.get('--grep') == 'failure':
    print('permission denied', file=sys.stderr)
    sys.exit(7)
records = [{
    '__CURSOR': str(i), '__REALTIME_TIMESTAMP': '1000001',
    'MESSAGE': [104,105,255], 'UNIT': 'dst-007-surface.service',
    'EXTRA': ['one', 'two'],
} for i in range(5)]
if '--follow' in sys.argv:
    print('reader warning', file=sys.stderr, flush=True)
    print(json.dumps(records[0]), flush=True)
    time.sleep(60)
if '--reverse' in sys.argv: records.reverse()
if '--cursor' in args:
    index = next(i for i, r in enumerate(records) if r['__CURSOR'] == args['--cursor'])
    records = records[index:]
for record in records[:int(args['--lines'])]: print(json.dumps(record))
"""

PLUGIN = r"""
assert sys.argv[1] == 'logs'
args = dict(arg[2:].split('=', 1) for arg in sys.argv[2:])
since, until = int(args['since']), int(args['until'])
print(json.dumps({
    'timestamp_ns': 18446744073709551615,
    'fields': [['tag','a'], ['tag','b'], ['body','玩家']],
}))
print(f'matched=5 returned=1 window={since}..{until}', file=sys.stderr)
"""


async def check_journal(directory: Path) -> None:
    journal = executable(directory, "journal", JOURNAL)
    logs = JournalLogs(journal)
    request = JournalQuery(
        limit=2,
        direction="forward",
        since=datetime(2026, 9, 1, 8, tzinfo=timezone(timedelta(hours=8))),
        until="now",
        namespace="games",
        grep="--help",
    )
    page = await logs.query(("dst-007-*.service",), request)
    assert [record.cursor for record in page.records] == ["0", "1"]
    assert page.has_more
    assert page.next_cursor == "1"
    record = page.records[0]
    assert record.fields["MESSAGE"] == [104, 105, 255]
    assert record.fields["EXTRA"] == ["one", "two"]
    assert record.message == "hi�"
    assert record.timestamp == datetime(1970, 1, 1, 0, 0, 1, 1, UTC)
    arguments = json.loads(journal.with_suffix(".args").read_text())
    assert "--since=2026-09-01 00:00:00.000000 UTC" in arguments
    assert "--grep=--help" in arguments
    resumed = await logs.query(
        None, JournalQuery(limit=2, direction="forward", cursor=page.next_cursor)
    )
    assert [record.cursor for record in resumed.records] == ["2", "3"]
    await reaped(int(journal.with_suffix(".pid").read_text()))
    try:
        await logs.query(None, JournalQuery(grep="failure"))
    except LogProcessError as error:
        assert error.returncode == 7
        assert error.diagnostics == "permission denied"
        assert not error.diagnostics_truncated
        assert error.command[0] == str(journal)
    else:
        msg = "reader error lost its status"
        raise AssertionError(msg)


async def check_streams(directory: Path) -> None:
    journal = executable(directory, "follow", JOURNAL)
    logs = JournalLogs(journal)
    async with logs.follow(None) as stream:
        assert (await anext(stream)).cursor == "0"
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        assert stream.diagnostics == "reader warning"
        await stream.close()
        assert isinstance(
            (await asyncio.gather(pending, return_exceptions=True))[0],
            StopAsyncIteration,
        )
        await reaped(stream.pid)

    async with logs.follow(None) as stream:
        await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.02)
        pending.cancel()
        assert isinstance(
            (await asyncio.gather(pending, return_exceptions=True))[0],
            asyncio.CancelledError,
        )
        await reaped(stream.pid)

    # Dropping the native handle also closes its Rust-owned process.
    stream = await _native.JournalLogs(journal, 4096, 65536).follow(
        None, {"direction": "forward", "limit": 0}
    )
    await stream.next()
    pid = stream.pid
    del stream
    gc.collect()
    await reaped(pid)

    hanging = executable(directory, "hanging", "time.sleep(60)\n")
    try:
        await JournalLogs(hanging).query(None, completion_timeout=0.2)
    except TimeoutError:
        await reaped(int(hanging.with_suffix(".pid").read_text()))
    else:
        msg = "native query timeout was not exposed"
        raise AssertionError(msg)


async def check_netdata(directory: Path) -> None:
    plugin = executable(directory, "otel-plugin", PLUGIN)
    reader = NetdataLogs(
        plugin, stock_config=directory / "stock.yaml", config=directory / "otel.yaml"
    )
    since = datetime(2026, 9, 1, 8, 0, 0, 123456, timezone(timedelta(hours=8)))
    result = await reader.query(
        NetdataLogQuery(
            since=since,
            until=since + timedelta(minutes=1),
            service_name="--service",
            service_namespace="",
            filters=(("event", "joined"), ("event", "left")),
            fields=("tag", "body"),
            query="玩家 | --limit 999",
            limit=2,
        )
    )
    assert result.records[0].timestamp_ns == (1 << 64) - 1
    assert result.records[0].values("tag") == ("a", "b")
    assert result.records[0].values("body") == ("玩家",)
    assert result.since == datetime(2026, 9, 1, tzinfo=UTC)
    assert result.until == result.since + timedelta(minutes=1)
    assert result.matched == 5
    assert result.truncated
    arguments = json.loads(plugin.with_suffix(".args").read_text())
    assert "--query=玩家 | --limit 999" in arguments
    assert "--namespace=" in arguments
    assert "--filter=event=joined,event=left" in arguments
    await reaped(int(plugin.with_suffix(".pid").read_text()))


async def check_validation() -> None:
    reader = NetdataLogs()
    for invalid in (True, 0, -1, 1.5, "1"):
        try:
            JournalLogs(max_record_bytes=invalid)
        except ValueError:
            pass
        else:
            msg = "invalid byte limit accepted"
            raise AssertionError(msg)
    for invalid in (True, 0, -1, float("inf"), float("nan"), "1"):
        try:
            await reader.query(
                NetdataLogQuery(since=1, until=2), completion_timeout=invalid
            )
        except ValueError:
            pass
        else:
            msg = "invalid query deadline accepted"
            raise AssertionError(msg)
    try:
        await reader.query(
            NetdataLogQuery(since=datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None))
        )
    except ValueError:
        pass
    else:
        msg = "naive datetime accepted"
        raise AssertionError(msg)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="dst-python-logs-") as directory:
        asyncio.run(check_journal(Path(directory)))
        asyncio.run(check_streams(Path(directory)))
        asyncio.run(check_netdata(Path(directory)))
        asyncio.run(check_validation())
    print("Python native log records, cancellation and reaping passed")  # ruff: ignore[print]


if __name__ == "__main__":
    main()
