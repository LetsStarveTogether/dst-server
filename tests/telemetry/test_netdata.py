import asyncio
import math
import os
import signal
import socket
import sys
import tracemalloc
from contextlib import suppress
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import orjson
import pytest
from pydantic import ValidationError

from dst_server.logs._process import LogProcessError
from dst_server.logs.netdata import NetdataLogQuery, NetdataLogRecord, NetdataLogs
from tests.helpers import process_stopped

FAKE_PLUGIN = (
    f"#!{sys.executable}\n"
    r"""import orjson
import os
from pathlib import Path
import socket
import sys
import time

assert sys.argv[1] == "logs"
assert all(argument.startswith("--") and "=" in argument for argument in sys.argv[2:])
args = dict(argument[2:].split("=", 1) for argument in sys.argv[2:])
Path(sys.argv[0]).with_suffix(".argv.json").write_bytes(orjson.dumps(args))
query = args.get("query", "")
since, until = int(args["since"]), int(args["until"])
limit = int(args["limit"])

if query.startswith("hang:"):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
        ready.sendto(str(os.getpid()).encode(), query.removeprefix("hang:"))
    while True:
        sys.stderr.write("waiting " * 8192)
        sys.stderr.flush()
        time.sleep(0.01)

if query == "empty":
    stream = ""
    if "name" in args:
        stream = f", stream={args.get('namespace', '')}/{args['name']}"
    print(
        "no WAL/SFST files matched "
        f"(tenant=default, window={since}..{until}{stream})",
        file=sys.stderr,
    )
    raise SystemExit(0)
if query == "invalid":
    print('{"timestamp_ns":')
    raise SystemExit(0)

count = 0 if query == "zero" else limit if query == "large" else 2
if query == "excess":
    count = limit + 1
for index in range(count):
    fields = [["event_name", "dst.player.action"], ["tag", "a"], ["tag", "b"]]
    if query == "large":
        fields = [["body", "x" * 32768]]
    print(orjson.dumps({
        "timestamp_ns": since * 1_000_000_000 + count - index,
        "fields": fields,
    }).decode())

if query == "fail":
    print("query failed", file=sys.stderr)
    raise SystemExit(7)
if query == "missing":
    raise SystemExit(0)
if query == "warnings":
    print("WARN skipped unreadable file", file=sys.stderr)
if query == "count-failure":
    print("WARN matched count failed for a file", file=sys.stderr)
if query == "diagnostics":
    print("diagnostic " * 20000, file=sys.stderr)

matched = 5 if query == "limited" else 0 if query == "count-failure" else count
returned = count + 1 if query == "wrong-count" else count
end = until + 1 if query == "wrong-window" else until
summary = f"matched={matched} returned={returned} window={since}..{end}"
print(summary, file=sys.stderr)
if query == "duplicate-summary":
    print(summary, file=sys.stderr)
if query == "lost-summary":
    print("later diagnostic " * 20000, file=sys.stderr)
"""
)


def make_logs(tmp_path: Path, **options: int) -> NetdataLogs:
    executable = tmp_path / "otel-plugin"
    executable.write_text(FAKE_PLUGIN, encoding="utf-8")
    executable.chmod(0o755)
    return NetdataLogs(
        executable,
        stock_config=tmp_path / "stock.yaml",
        config=tmp_path / "otel.yaml",
        **options,
    )


def request(**changes: object) -> NetdataLogQuery:
    return NetdataLogQuery.model_validate({
        "since": datetime(2026, 9, 1, tzinfo=UTC),
        "until": datetime(2026, 9, 1, 0, 5, tzinfo=UTC),
        **changes,
    })


@pytest.mark.parametrize("query", ["--help", "玩家 | --limit 999"])
async def test_query_preserves_fields_and_encodes_arguments(
    tmp_path: Path, query: str
) -> None:
    logs = make_logs(tmp_path)
    value = request(
        service_name="--service",
        service_namespace="",
        filters=(("cluster", "dst-000"), ("event", "joined"), ("event", "left")),
        query=query,
        fields=("event_name", "tag"),
        limit=20,
    )
    result = await logs.query(value)

    assert result.records[0].fields == (
        ("event_name", "dst.player.action"),
        ("tag", "a"),
        ("tag", "b"),
    )
    assert result.records[0].values("tag") == ("a", "b")
    assert result.records[0].values("missing") == ()
    assert result.records[0].timestamp_ns > result.records[1].timestamp_ns
    assert result.matched == 2
    assert result.truncated is False
    assert result.since == value.since
    assert result.until == value.until
    arguments = orjson.loads((tmp_path / "otel-plugin.argv.json").read_text())
    assert arguments == {
        "stock-config": str(tmp_path / "stock.yaml"),
        "config": str(tmp_path / "otel.yaml"),
        "since": str(int(value.since.timestamp())),
        "until": str(int(result.until.timestamp())),
        "name": "--service",
        "namespace": "",
        "filter": "cluster=dst-000,event=joined,event=left",
        "query": query,
        "fields": "event_name,tag",
        "limit": "20",
        "output": "ndjson",
    }


async def test_default_query_does_not_assume_service_and_includes_current_second(
    tmp_path: Path,
) -> None:
    before = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    result = await make_logs(tmp_path).query(request(until=None))
    after = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)

    assert before <= result.until <= after
    arguments = orjson.loads((tmp_path / "otel-plugin.argv.json").read_text())
    assert "name" not in arguments
    assert "namespace" not in arguments
    assert arguments["until"] == str(int(result.until.timestamp()))


@pytest.mark.parametrize("query", ["empty", "zero"])
@pytest.mark.parametrize("service_name", [None, "dst-server"])
async def test_empty_storage_and_zero_matches_are_valid(
    tmp_path: Path, query: str, service_name: str | None
) -> None:
    result = await make_logs(tmp_path).query(
        request(query=query, service_name=service_name)
    )
    assert result.records == ()
    assert result.matched == 0
    assert result.truncated is False


@pytest.mark.parametrize(
    ("query", "matched", "truncated"),
    [("limited", 5, True), ("warnings", 2, False), ("count-failure", 0, False)],
)
async def test_backend_counts_do_not_claim_completeness(
    tmp_path: Path, query: str, matched: int, truncated: bool
) -> None:
    result = await make_logs(tmp_path).query(request(query=query, limit=2))
    assert result.matched == matched
    assert result.truncated is truncated
    assert "complete" not in result.model_dump()
    if query == "warnings":
        assert "WARN skipped unreadable file" in result.diagnostics


@pytest.mark.parametrize("query", ["diagnostics", "lost-summary"])
async def test_diagnostics_are_bounded_and_truncation_is_explicit(
    tmp_path: Path, query: str
) -> None:
    result = await make_logs(tmp_path).query(request(query=query))
    assert result.diagnostics_truncated
    assert len(result.diagnostics.encode()) <= 65536
    assert result.matched == (2 if query == "diagnostics" else None)
    assert result.truncated == (False if query == "diagnostics" else None)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("invalid", "invalid Netdata NDJSON record on line 1"),
        ("missing", "valid query summary"),
        ("wrong-count", "returned count"),
        ("wrong-window", "summary window"),
        ("duplicate-summary", "multiple query summaries"),
        ("excess", "more records"),
    ],
)
async def test_invalid_protocol_is_not_an_empty_result(
    tmp_path: Path, query: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await make_logs(tmp_path).query(request(query=query))


async def test_process_failure_preserves_status_and_diagnostics(tmp_path: Path) -> None:
    with pytest.raises(LogProcessError) as failure:
        await make_logs(tmp_path).query(request(query="fail"))
    assert failure.value.returncode == 7
    assert failure.value.diagnostics == "query failed"
    assert failure.value.diagnostics_truncated is False


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_record_bytes": 1024}, "record exceeds"),
        ({"max_output_bytes": 40000}, "query exceeds"),
    ],
)
async def test_byte_limits_bound_large_output(
    tmp_path: Path, options: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await make_logs(tmp_path, **options).query(request(query="large", limit=2))


async def test_parsing_does_not_retain_a_second_complete_wire_copy(
    tmp_path: Path,
) -> None:
    count = 128
    logs = make_logs(tmp_path)
    tracemalloc.start()
    try:
        result = await logs.query(request(query="large", limit=count))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(result.records) == count
    assert result.records[-1].values("body") == ("x" * 32768,)
    assert peak < count * 32768 * 1.8


@pytest.mark.parametrize("finish", ["timeout", "cancel"])
async def test_interruption_drains_and_reaps_a_noisy_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    loop = asyncio.get_running_loop()
    real_timeout = asyncio.timeout
    deadline = real_timeout(None)
    monkeypatch.setattr(
        asyncio,
        "timeout",
        lambda delay: deadline if delay == 42 else real_timeout(delay),
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as ready:
        address = str(tmp_path / "ready")
        ready.bind(address)
        ready.setblocking(False)
        pending = asyncio.create_task(
            make_logs(tmp_path).query(
                request(query=f"hang:{address}"), completion_timeout=42
            )
        )
        completed = asyncio.gather(pending, return_exceptions=True)
        process_fd = None
        try:
            async with real_timeout(5):
                process_id = int(await loop.sock_recv(ready, 32))
            process_fd = os.pidfd_open(process_id)
            if finish == "timeout":
                deadline.reschedule(loop.time())
            else:
                pending.cancel()
            (outcome,) = await asyncio.wait_for(asyncio.shield(completed), 5)
            assert isinstance(
                outcome, TimeoutError if finish == "timeout" else asyncio.CancelledError
            )
            assert await asyncio.to_thread(process_stopped, process_id)
        finally:
            pending.cancel()
            if process_fd is not None:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(process_fd, signal.SIGKILL)
                os.close(process_fd)
            await asyncio.wait_for(asyncio.shield(completed), 5)


async def test_timeout_includes_waiting_for_a_query_slot(tmp_path: Path) -> None:
    logs = make_logs(tmp_path)
    async with logs._semaphore:
        with pytest.raises(TimeoutError):
            await logs.query(request(), completion_timeout=0.01)


def test_time_normalization_preserves_instant_and_exposes_second_precision() -> None:
    value = request(
        since=datetime(2026, 9, 1, 8, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8)))
    )
    assert value.since == datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "changes",
    [
        {"until": datetime(2026, 9, 1, tzinfo=UTC)},
        {"service_namespace": "games"},
        {"service_namespace": ""},
        {"since": datetime(2026, 9, 1)},  # ruff: ignore[call-datetime-without-tzinfo]
        {"since": datetime(1969, 12, 31, tzinfo=UTC)},
        {"until": datetime.fromtimestamp(1 << 32, UTC)},
        {"limit": True},
        {"limit": 0},
        {"filters": (("bad,field", "value"),)},
        {"filters": (("field", "bad,value"),)},
        {"filters": (("field", " value "),)},
        {"fields": ("bad,field",)},
        {"fields": (" field ",)},
        {"query": "\0"},
        {"service_name": "\0"},
    ],
)
def test_invalid_queries_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**changes)


@pytest.mark.parametrize("value", [True, 0, -1, math.inf, math.nan, "1"])
async def test_invalid_deadline_is_rejected(tmp_path: Path, value: object) -> None:
    with pytest.raises(ValidationError):
        await make_logs(tmp_path).query(
            request(), completion_timeout=cast("float", value)
        )


@pytest.mark.parametrize(
    "option", ["max_concurrency", "max_record_bytes", "max_output_bytes"]
)
@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1"])
def test_invalid_reader_limits_are_rejected(option: str, value: object) -> None:
    with pytest.raises(ValueError, match=r"positive|validation"):
        NetdataLogs(**{option: cast("int", value)})  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("timestamp", [-1, True, 1 << 64])
def test_records_validate_timestamps(timestamp: int) -> None:
    with pytest.raises(ValidationError):
        NetdataLogRecord(timestamp_ns=timestamp, fields=())
