from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from ulid import ULID

from dst_server.activity import ActivityCheckpoint
from dst_server.host import recycling as worker
from dst_server.host.locking import room_lock
from dst_server.models.cluster import (
    ClusterStatus,
    ShardDesired,
    ShardPhase,
    ShardRuntimeStatus,
)
from dst_server.rooms import Control, read_control, write_control
from dst_server.rpc import ClusterClient

NOW = datetime(2026, 9, 10, 23, tzinfo=UTC)


def ready_status(suffix: str = "OLD") -> ClusterStatus:
    return ClusterStatus(
        epoch=ULID(),
        phase="running",
        prepared=True,
        master="forest",
        shards=tuple(
            ShardRuntimeStatus(
                name=name,
                is_master=name == "forest",
                desired=ShardDesired.RUNNING,
                phase=ShardPhase.RUNNING,
                session_id=f"{name}_{suffix}",
                ready=True,
                telemetry_profile="off",
            )
            for name in ("forest", "cave")
        ),
    )


@pytest.fixture
def room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Mock, Mock, ClusterStatus]:
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    monkeypatch.setattr(worker, "datetime", clock)
    host = Mock()
    host.status = AsyncMock(
        return_value={"load": "loaded", "active": "active", "job_id": 0}
    )
    host.rooms.policy.side_effect = lambda _: read_control(tmp_path)
    host.rooms.path.return_value = tmp_path
    host.rooms.numbers.return_value = (0,)
    status = ready_status()
    client = Mock(spec=ClusterClient)
    client.status = AsyncMock(side_effect=[status, ready_status("NEW")])
    client.list_players = AsyncMock(return_value=())
    client.shard.return_value.world = AsyncMock(return_value=Mock(day=8))
    client.shard.return_value.connected_shards = AsyncMock(
        return_value=(Mock(ready=True), Mock(ready=True))
    )
    client.regenerate = AsyncMock()
    connection = AsyncMock()
    connection.__aenter__.return_value = client
    host.connect.return_value = connection
    write_control(
        tmp_path,
        Control(
            recycle=True,
            activity=ActivityCheckpoint(
                sessions=worker.ready_sessions(status),
                last_active_at=NOW - timedelta(days=10),
            ),
        ),
    )
    return host, client, status


@pytest.mark.parametrize(
    ("day", "hours"),
    [(8, 6), (9, 24), (30, 24), (31, 36), (70, 36), (71, 72), (280, 72), (281, 168)],
)
@pytest.mark.parametrize("excess", [-1, 0, 1])
async def test_retention_uses_last_activity_and_strict_threshold(
    room: tuple[Mock, Mock, ClusterStatus],
    tmp_path: Path,
    day: int,
    hours: int,
    excess: int,
) -> None:
    host, client, status = room
    assert worker.idle_limit(day) == timedelta(hours=hours)
    client.shard.return_value.world.return_value = Mock(day=day)
    current = NOW - timedelta(hours=hours, seconds=excess)
    client.status.side_effect = [
        status.replace(
            shards=(status.shards[0], status.shards[1].replace(last_active_at=current))
        ),
        ready_status("NEW"),
    ]
    assert await worker.recycle(host, client, tmp_path, 0) == (excess > 0)
    activity = read_control(tmp_path).activity
    assert activity is not None
    if excess > 0:
        client.regenerate.assert_awaited_once_with(
            expected_session_id="forest_OLD", require_empty=True
        )
        assert activity.sessions == worker.ready_sessions(ready_status("NEW"))
    else:
        client.regenerate.assert_not_awaited()
        assert activity.last_active_at == current


@pytest.mark.parametrize("change", ["missing", "world", "occupied", "paused", "busy"])
async def test_new_baseline_or_current_activity_prevents_recycling(
    room: tuple[Mock, Mock, ClusterStatus], tmp_path: Path, change: str
) -> None:
    host, client, status = room
    control = read_control(tmp_path)
    if change == "missing":
        write_control(tmp_path, control.model_copy(update={"activity": None}))
    elif change == "world":
        client.status.side_effect = [ready_status("DIFFERENT")]
    elif change == "occupied":
        client.list_players.return_value = (Mock(userid="KU_LOBBY"),)
    elif change == "paused":
        write_control(tmp_path, control.model_copy(update={"paused": True}))
        client.status.side_effect = [
            status.replace(
                shards=(status.shards[0].replace(last_active_at=NOW), status.shards[1])
            )
        ]
    else:
        client.status.side_effect = [status.replace(busy=True)]
    assert not await worker.recycle(host, client, tmp_path, 0)
    client.regenerate.assert_not_awaited()
    if change != "busy":
        activity = read_control(tmp_path).activity
        assert activity is not None
        assert activity.last_active_at == NOW


@pytest.mark.parametrize(
    "changes",
    [
        {"ready": False},
        {"phase": ShardPhase.STARTING},
        {"session_id": None},
        {"driver_error": "failed"},
    ],
)
async def test_unready_rooms_are_skipped(
    room: tuple[Mock, Mock, ClusterStatus], tmp_path: Path, changes: dict[str, object]
) -> None:
    host, client, status = room
    client.status.side_effect = [
        status.replace(shards=(status.shards[0].replace(**changes), status.shards[1]))
    ]
    assert not await worker.recycle(host, client, tmp_path, 0)
    client.regenerate.assert_not_awaited()


async def test_export_losses_do_not_disable_recycling(
    room: tuple[Mock, Mock, ClusterStatus], tmp_path: Path
) -> None:
    host, client, status = room
    client.status.side_effect = [
        status.replace(
            shards=(
                status.shards[0].replace(telemetry_invalid=5, telemetry_dropped=10),
                status.shards[1],
            )
        ),
        ready_status("NEW"),
    ]
    assert await worker.recycle(host, client, tmp_path, 0)


async def test_dry_run_does_not_write_or_regenerate(
    room: tuple[Mock, Mock, ClusterStatus], tmp_path: Path
) -> None:
    host, client, _ = room
    before = read_control(tmp_path)
    assert await worker.recycle(host, client, tmp_path, 0, dry_run=True)
    assert read_control(tmp_path) == before
    client.regenerate.assert_not_awaited()


async def test_regeneration_must_confirm_every_new_world(
    room: tuple[Mock, Mock, ClusterStatus], tmp_path: Path
) -> None:
    host, client, status = room
    client.status.side_effect = [status, status]
    with pytest.raises(RuntimeError, match="every shard"):
        await worker.recycle(host, client, tmp_path, 0)


async def test_busy_host_lock_skips_timer_run(
    room: tuple[Mock, Mock, ClusterStatus], tmp_path: Path
) -> None:
    host, client, _ = room
    async with room_lock(tmp_path):
        assert await worker.run_recycle(host) == {0: {"status": "skipped"}}
    client.regenerate.assert_not_awaited()


async def test_game_rpc_wait_releases_host_lock(
    room: tuple[Mock, Mock, ClusterStatus], tmp_path: Path
) -> None:
    host, client, _ = room

    async def regenerate(**_: object) -> None:
        import asyncio

        await asyncio.sleep(0)
        async with room_lock(tmp_path):
            pass

    client.regenerate.side_effect = regenerate
    assert await worker.recycle(host, client, tmp_path, 0)
