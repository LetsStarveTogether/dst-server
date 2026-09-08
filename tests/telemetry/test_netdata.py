import asyncio
import math
import subprocess  # ruff: ignore[suspicious-subprocess-import]
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from dst_server.netdata import NetdataLogQuery, NetdataLogRecord, NetdataLogs
from tests.helpers import process_stopped

FAKE_PLUGIN = r"""#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

assert sys.argv[1] == "logs"
arguments = sys.argv[2:]
query = arguments[arguments.index("--query") + 1] if "--query" in arguments else ""
print("ARGV|" + "|".join(arguments), file=sys.stderr, flush=True)

if query == "fail":
    print("query failed", file=sys.stderr, flush=True)
    raise SystemExit(7)
if query == "invalid":
    print('{"timestamp_ns":', flush=True)
    raise SystemExit(0)
if query.startswith("hang:"):
    Path(query.removeprefix("hang:")).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(60)

print(json.dumps({
    "timestamp_ns": 42,
    "fields": [["event_name", "dst.player.action"], ["tag", "a"], ["tag", "b"]],
}))
print(json.dumps({"timestamp_ns": 43, "fields": []}))
print("matched=2 returned=2 window=0..1", file=sys.stderr, flush=True)
"""


def make_logs(tmp_path: Path) -> NetdataLogs:
    executable = tmp_path / "otel-plugin"
    executable.write_text(FAKE_PLUGIN, encoding="utf-8")
    executable.chmod(0o755)
    return NetdataLogs(
        executable,
        stock_config=tmp_path / "stock.yaml",
        config=tmp_path / "otel.yaml",
    )


def request(**changes: object) -> NetdataLogQuery:
    return NetdataLogQuery.model_validate({
        "since": datetime(2026, 9, 1, tzinfo=UTC),
        **changes,
    })


async def test_query_maps_arguments_and_preserves_records(tmp_path: Path) -> None:
    logs = make_logs(tmp_path)
    value = request(
        until=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=5),
        service_namespace="games",
        filters=(("cluster", "dst-000"), ("event", "joined")),
        query="KU_123",
        fields=("event_name", "tag"),
        limit=20,
    )

    result = await logs.query(value)

    assert result.records[0].timestamp_ns == 42
    assert result.records[0].fields == (
        ("event_name", "dst.player.action"),
        ("tag", "a"),
        ("tag", "b"),
    )
    assert result.records[1].fields == ()
    assert f"--since|{int(value.since.timestamp())}" in result.diagnostics
    assert value.until is not None
    assert f"--until|{int(value.until.timestamp())}" in result.diagnostics
    assert "--filter|cluster=dst-000,event=joined" in result.diagnostics
    assert "--fields|event_name,tag" in result.diagnostics
    assert "--limit|20|--output|ndjson" in result.diagnostics
    assert "matched=2 returned=2" in result.diagnostics


@pytest.mark.parametrize(
    ("query", "error", "match"),
    [
        ("fail", subprocess.CalledProcessError, "returned non-zero exit status 7"),
        ("invalid", ValueError, "invalid Netdata NDJSON record on line 1"),
    ],
)
async def test_query_rejects_process_and_protocol_errors(
    tmp_path: Path,
    query: str,
    error: type[Exception],
    match: str,
) -> None:
    with pytest.raises(error, match=match):
        await make_logs(tmp_path).query(request(query=query))


async def test_query_timeout_kills_and_reaps_process(tmp_path: Path) -> None:
    process_id = tmp_path / "pid"

    with pytest.raises(TimeoutError):
        await make_logs(tmp_path).query(
            request(query=f"hang:{process_id}"),
            completion_timeout=0.5,
        )

    assert process_stopped(int(process_id.read_text(encoding="utf-8")))


async def test_query_timeout_includes_waiting_for_a_query_slot(tmp_path: Path) -> None:
    logs = make_logs(tmp_path)
    async with logs._semaphore:
        with pytest.raises(TimeoutError):
            await logs.query(request(), completion_timeout=0.01)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"until": datetime(2026, 9, 1, tzinfo=UTC)}, "until must be later"),
        ({"service_name": None, "service_namespace": "games"}, "namespace requires"),
        (
            {"since": datetime(2026, 9, 1)},  # ruff: ignore[call-datetime-without-tzinfo]
            "timezone_aware",
        ),
        (
            {
                "since": datetime(2026, 9, 1, microsecond=100, tzinfo=UTC),
                "until": datetime(2026, 9, 1, microsecond=200, tzinfo=UTC),
            },
            "until must be later",
        ),
        ({"since": datetime(1969, 12, 31, tzinfo=UTC)}, "unsigned 32-bit"),
        ({"until": datetime.fromtimestamp(1 << 32, UTC)}, "unsigned 32-bit"),
        ({"limit": True}, "int_type"),
        ({"limit": 0}, "greater_than"),
        ({"limit": -1}, "greater_than"),
    ],
)
def test_query_validates_stable_semantics(
    changes: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        request(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"filters": (("bad,field", "value"),)},
        {"filters": (("field", "bad,value"),)},
        {"filters": (("field", " value "),)},
        {"fields": ("bad,field",)},
        {"fields": (" field ",)},
    ],
)
def test_query_rejects_values_the_cli_cannot_encode(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match=r"comma|whitespace"):
        request(**changes)


@pytest.mark.parametrize("value", [True, 0, -1, math.inf, math.nan, "1"])
async def test_query_rejects_invalid_deadlines_before_creating_process(
    tmp_path: Path, value: object
) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        await make_logs(tmp_path).query(
            request(), completion_timeout=cast("float", value)
        )


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1"])
def test_query_rejects_invalid_concurrency(value: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        NetdataLogs(max_concurrency=cast("int", value))


async def test_cancellation_kills_and_reaps_child(tmp_path: Path) -> None:
    process_id = tmp_path / "pid"
    pending = asyncio.create_task(
        make_logs(tmp_path).query(request(query=f"hang:{process_id}"))
    )
    try:
        async with asyncio.timeout(3):
            while not process_id.exists() or not process_id.read_text():  # ruff: ignore[async-busy-wait]
                await asyncio.sleep(0.005)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert process_stopped(int(process_id.read_text(encoding="utf-8")))
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("timestamp", [-1, True, 1 << 64])
def test_records_reject_out_of_range_timestamps(timestamp: int) -> None:
    with pytest.raises(
        ValidationError, match=r"greater_than_equal|less_than_equal|int_type"
    ):
        NetdataLogRecord(timestamp_ns=timestamp, fields=())
