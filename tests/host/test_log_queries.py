from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from dst_server.host import Host
from dst_server.logs import JournalLogs, JournalQuery, NetdataLogQuery, NetdataLogs


def test_history_identities_do_not_require_room_files_or_current_shards(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path / "missing", tmp_path / "missing-quadlets")
    assert host.log_units([7, 12, 7]) == (
        "dst-007-pod.service",
        "dst-007-*.service",
        "dst-012-pod.service",
        "dst-012-*.service",
    )
    assert host.log_units(7, shard="deleted") == ("dst-007-deleted.service",)
    assert host.log_units(7, shard="洞穴") == (
        "dst-007-\\xe6\\xb4\\x9e\\xe7\\xa9\\xb4.service",
    )
    assert host.log_units(7, shard="literal*") == ("dst-007-literal\\x2a.service",)
    assert not host.cluster_root.exists()
    assert not host.quadlet_dir.exists()
    assert host._systemd is None


@pytest.mark.parametrize("rooms", [True, [], "7", [7, True], [-1], [300], [1.5]])
def test_log_room_scope_is_validated_before_querying(
    tmp_path: Path, rooms: object
) -> None:
    host = Host(tmp_path, tmp_path)
    with pytest.raises(ValueError, match=r"room|RoomPortAllocation"):
        host.log_units(rooms)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("shard", ["", "../forest", "forest/cave", "bad\0name"])
def test_log_shard_scope_reuses_native_directory_validation(
    tmp_path: Path, shard: str
) -> None:
    with pytest.raises(ValueError, match="unsafe DST shard"):
        Host(tmp_path, tmp_path).log_units(7, shard=shard)


async def test_host_queries_and_follow_use_stable_identity_without_systemd_or_rpc(
    tmp_path: Path,
) -> None:
    journal = Mock(spec=JournalLogs)
    netdata = Mock(spec=NetdataLogs)
    host = Host(
        tmp_path / "missing",
        tmp_path / "quadlets",
        journal_logs=journal,
        netdata_logs=netdata,
    )
    criteria = JournalQuery(limit=2)
    assert await host.journal(7, criteria) is journal.query.return_value
    journal.query.assert_awaited_once_with(
        ("dst-007-pod.service", "dst-007-*.service"),
        criteria,
        completion_timeout=120.0,
    )

    closed = False
    stream = object()

    @asynccontextmanager
    async def follow(
        units: tuple[str, ...], request: JournalQuery
    ) -> AsyncIterator[object]:
        nonlocal closed
        assert units == ("dst-007-deleted.service",)
        assert request.direction == "forward"
        assert request.limit == 0
        try:
            yield stream
        finally:
            closed = True

    journal.follow.side_effect = follow
    async with host.follow_journal(7, shard="deleted") as selected:
        assert selected is stream
    assert closed

    now = datetime.now(UTC)
    request = NetdataLogQuery(
        since=now - timedelta(minutes=1),
        filters=(("event_name", "dst.player.shard_entered"),),
    )
    assert (
        await host.telemetry([7, 12], request, shard="洞穴")
        is netdata.query.return_value
    )
    scoped = netdata.query.call_args.args[0]
    assert scoped.service_name is None
    assert scoped.filters == (
        ("attributes.dst.cluster.name", "dst-007"),
        ("attributes.dst.cluster.name", "dst-012"),
        ("attributes.dst.shard.name", "洞穴"),
        ("event_name", "dst.player.shard_entered"),
    )
    assert request.filters == (("event_name", "dst.player.shard_entered"),)
    assert host._systemd is None
    assert not host.cluster_root.exists()


@pytest.mark.parametrize(
    "field", ["attributes.dst.cluster.name", "attributes.dst.shard.name"]
)
async def test_repeated_native_filters_cannot_expand_host_scope(
    tmp_path: Path, field: str
) -> None:
    netdata = Mock(spec=NetdataLogs)
    netdata.query = AsyncMock()
    host = Host(tmp_path, tmp_path, netdata_logs=netdata)
    request = NetdataLogQuery(
        since=datetime.now(UTC) - timedelta(minutes=1),
        filters=((field, "another-room-or-shard"),),
    )
    with pytest.raises(ValueError, match="managed by Host"):
        await host.telemetry(7, request)
    netdata.query.assert_not_called()
