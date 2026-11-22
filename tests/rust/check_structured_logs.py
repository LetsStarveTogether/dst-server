"""Check Netdata projections and live batches with isolated readers."""

import asyncio
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from dst_server import EventFilter, Host, NetdataLogs, RoomStream, subscribe_history

START = datetime(2026, 11, 22, tzinfo=UTC)
UID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
PLUGIN = """
import json, sys
args = dict(a.split('=', 1) for a in sys.argv[2:])
is_coverage = 'event_name=dst.telemetry.coverage' in args['--filter']
assert args['--tenant'] == 'fixture'
assert 'Caves,East' not in args['--filter'] and 'room,1' not in args['--filter']
assert (('--query' in args) != is_coverage)
since, until = int(args['--since']), int(args['--until'])
name = 'dst.telemetry.coverage' if is_coverage else 'dst.player.ate'
if is_coverage and args['--name'] == 'broken':
    sys.exit(8)
fields = [
    ('event_name', name),
    ('attributes.log.record.uid', '01ARZ3NDEKTSV4RRFFQ69G5FAV'),
    ('attributes.dst.shard.name', 'Caves,East'),
    ('resource.attributes.dst.cluster.name', 'room,1'),
    ('resource.attributes.service.instance.id', 'agent-1'),
    ('body.feeder.userid', 'KU_a.b'), ('body.food','pie'),
]
conflict = fields + [('attributes.dst.shard.name','other')]
rows = [fields] if is_coverage else [fields, fields, conflict]
for row in rows:
    print(json.dumps({'timestamp_ns': since*10**9+2000, 'fields': row}))
matched = len(rows) + int(not is_coverage)
print(f'matched={matched} returned={len(rows)} window={since}..{until}',
      file=sys.stderr)
"""


async def check_search(directory: Path) -> None:
    plugin = directory / "plugin"
    plugin.write_text(f"#!{sys.executable}\n{PLUGIN}")
    plugin.chmod(0o755)
    reader = NetdataLogs(plugin)
    where = EventFilter(
        events=("dst.player.ate",), shards=("Caves,East",), userid="KU_a.b"
    )
    for service in ("dst-server", "broken"):
        source = RoomStream(1, service, "", "room,1", "agent-1", tenant="fixture")
        result = await reader.search(
            source,
            since=START + timedelta(microseconds=1),
            until=START + timedelta(microseconds=3),
            where=where,
            fields=("body.food",),
            limit=3,
        )
        page = result.gameplay
        assert [row.uid for row in page.hits] == [UID, UID]
        assert [row.fields for row in page.hits] == [(("body.food", "pie"),)] * 2
        assert page.distinct_uids == page.unverified == 1
        assert page.matched_candidates == 4
        assert page.truncated
        assert result.native_since == START
        assert result.native_until == START + timedelta(seconds=1)
        if service == "broken":
            assert result.coverage.error
            assert result.coverage.matched_candidates is None
        else:
            assert result.coverage.hits[0].event_name == "dst.telemetry.coverage"
            assert result.coverage.error is None


def event(player: str) -> dict:
    return {
        "event_name": "dst.client.authenticated",
        "body": {"userid": player},
        "observed_timestamp_ns": 100,
        "resource": {},
        "attributes": {
            "log.record.uid": UID,
            "dst.cluster.name": "one",
            "dst.shard.name": "Forest",
            "dst.game.attempt.id": UID,
            "dst.runtime.generation": 1,
            "dst.event.sequence": 1,
            "dst.tick": 1,
            "dst.monotonic_ms": 1,
        },
    }


def live_host(stream: AsyncMock, lifecycle: AsyncMock) -> AsyncMock:
    from collections.abc import AsyncGenerator

    host = AsyncMock(spec=Host)

    @asynccontextmanager
    async def connect(room: int) -> AsyncGenerator[AsyncMock]:
        assert room == 1
        client = AsyncMock()
        client.subscribe.side_effect = [stream, lifecycle]
        stream.__aenter__.return_value = stream
        lifecycle.__aenter__.return_value = lifecycle
        yield client

    host.connect = connect
    return host


async def check_live() -> None:
    stream, lifecycle = AsyncMock(), AsyncMock()
    host = live_host(stream, lifecycle)
    stream.next.side_effect = [
        {"records": [event("KU_b")], "dropped": 7, "closed": False},
        {"records": [event("KU_a")], "dropped": 0, "closed": True},
    ]
    lifecycle.next.return_value = {
        "records": [{"event": "room_status", "status": {"active": True}}],
        "dropped": 2,
        "closed": True,
    }
    async with subscribe_history(
        host, 1, where=EventFilter(userid="KU_a"), buffer_batches=1
    ) as live:
        batches = [batch async for batch in live]
    game = [b for b in batches if b.stream == "events"]
    assert game[0].dropped == 7
    assert not game[0].observations
    assert game[1].closed
    assert game[1].observations[0].uid == UID
    [status] = [b for b in batches if b.stream == "lifecycle"]
    assert status.auxiliary[0]["event"] == "room_status"
    assert status.dropped == 2
    assert status.closed
    stream.__aexit__.assert_awaited_once()
    lifecycle.__aexit__.assert_awaited_once()
    await check_live_arguments()
    await check_live_order()
    await check_live_reconciliation()
    await check_live_disconnect()
    await check_live_cancellation()


async def check_live_arguments() -> None:
    host = AsyncMock(spec=Host)
    for key, value in [
        ("where", {}),
        ("where", {"userid": "fixture"}),
        ("buffer_batches", float("nan")),
        ("buffer_batches", True),
        ("buffer_batches", 0),
        ("max_items", True),
        ("max_items", 1.5),
        ("max_items", 513),
    ]:
        try:
            async with subscribe_history(host, 1, **{key: value}):
                raise AssertionError(key)
        except (TypeError, ValueError) as exc:
            reason = str(exc)
        assert key in reason
    host.connect.assert_not_called()


async def check_live_order() -> None:
    import json
    import runpy

    fixtures = runpy.run_path(str(Path(__file__).with_name("check_history.py")))
    journal, _ = fixtures["coverage_rows"](fixtures["capture_body"]())
    coverage = json.loads(journal.message.removeprefix("DST_RECORD|"))
    status = {"event": "room_status", "status": {}}
    events, lifecycle = AsyncMock(), AsyncMock()
    events.next.return_value = {"records": [], "dropped": 0, "closed": True}
    lifecycle.next.side_effect = [
        {"records": [coverage, status], "dropped": 0, "closed": False},
        {"records": [status, coverage], "dropped": 0, "closed": True},
    ]
    async with subscribe_history(live_host(events, lifecycle), 1) as live:
        first, last = [batch async for batch in live if batch.stream == "lifecycle"]
    assert (first.observation_indices, first.auxiliary_indices) == ((0,), (1,))
    assert (last.observation_indices, last.auxiliary_indices) == ((1,), (0,))


async def check_live_reconciliation() -> None:
    from types import SimpleNamespace

    from dst_server import HistorySelection

    events, lifecycle = AsyncMock(), AsyncMock()
    host = live_host(events, lifecycle)
    events.next.return_value = {"records": [event("a")], "dropped": 0, "closed": True}
    lifecycle.next.return_value = {"records": [], "dropped": 0, "closed": True}
    async with subscribe_history(host, 1) as live:
        registration = live.registered_at
        receipt = registration + timedelta(seconds=1)
        delayed_uid = "01ARZ3NDEKTSV4RRFFQ69G5FAW"

        def read_receipts(selection: HistorySelection, **_: object) -> SimpleNamespace:
            # Both writes arrive after registration. Only one also reached RPC.
            uids = (
                (UID, delayed_uid)
                if selection.since <= receipt < selection.until
                else ()
            )
            records = tuple(SimpleNamespace(uid=uid) for uid in uids)
            return SimpleNamespace(records=records, has_more=False)

        host.history_page.side_effect = read_receipts
        seen = {row.uid async for batch in live for row in batch.observations}
        assert seen == {UID}
        async for page in live.backfill(
            registration - timedelta(days=1), until=registration
        ):
            assert not page.records
        async for page in live.backfill(
            registration, until=receipt + timedelta(seconds=1)
        ):
            seen.update(row.uid for row in page.records)
        assert seen == {UID, delayed_uid}


async def check_live_disconnect() -> None:
    events, lifecycle = AsyncMock(), AsyncMock()
    error = ConnectionError("fixture disconnected")
    events.next.side_effect = error
    lifecycle.next.return_value = {"records": [], "dropped": 3, "closed": True}
    async with (
        asyncio.timeout(2),
        subscribe_history(live_host(events, lifecycle), 1) as live,
    ):
        batches = {batch.stream: batch async for batch in live}
        assert batches["events"].error is error
        assert batches["events"].dropped is None
        assert not batches["events"].closed
        assert batches["lifecycle"].closed
        assert batches["lifecycle"].dropped == 3
        assert batches["lifecycle"].error is None
        assert await anext(live, None) is None


async def check_live_cancellation() -> None:
    events, lifecycle = AsyncMock(), AsyncMock()
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def pending(_: int) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    events.next.side_effect = lifecycle.next.side_effect = pending
    reader = None

    async def subscribe() -> None:
        nonlocal reader
        async with subscribe_history(live_host(events, lifecycle), 1) as live:
            reader = asyncio.create_task(anext(live, None))
            await asyncio.Event().wait()

    async with asyncio.timeout(2):
        owner = asyncio.create_task(subscribe())
        await started.wait()
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
        assert owner.cancelled()
        assert cancelled.is_set()
        assert reader is not None
        assert await reader is None
        events.__aexit__.assert_awaited_once()
        lifecycle.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as temp:
        asyncio.run(check_search(Path(temp)))
        asyncio.run(check_live())
