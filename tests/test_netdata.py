import subprocess  # ruff: ignore[suspicious-subprocess-import]
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from dst_server.netdata import NetdataLogQuery, NetdataLogs

from .helpers import process_stopped

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
            completion_timeout=0.1,
        )

    assert process_stopped(int(process_id.read_text(encoding="utf-8")))


def test_query_validates_stable_semantics() -> None:
    since = datetime(2026, 9, 1, tzinfo=UTC)

    with pytest.raises(ValidationError, match="until must be later than since"):
        NetdataLogQuery(since=since, until=since)
    with pytest.raises(ValidationError, match="namespace requires a service name"):
        NetdataLogQuery(
            since=since,
            service_name=None,
            service_namespace="games",
        )
    with pytest.raises(ValidationError, match="timezone_aware"):
        NetdataLogQuery(
            since=datetime(2026, 9, 1),  # ruff: ignore[call-datetime-without-tzinfo]
        )
    with pytest.raises(ValidationError, match="until must be later than since"):
        NetdataLogQuery(
            since=since + timedelta(microseconds=100),
            until=since + timedelta(microseconds=200),
        )
    with pytest.raises(ValidationError, match="unsigned 32-bit"):
        NetdataLogQuery(since=datetime(1969, 12, 31, tzinfo=UTC))


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
async def test_query_rejects_values_the_cli_cannot_encode(
    tmp_path: Path,
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=r"comma|whitespace"):
        await make_logs(tmp_path).query(request(**changes))
