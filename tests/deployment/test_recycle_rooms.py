import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from ulid import ULID

from dst_server.activity import write_last_login
from dst_server.models.cluster import (
    ClusterStatus,
    ShardDesired,
    ShardPhase,
    ShardRuntimeStatus,
)
from dst_server.models.driver import DriverHealth
from dst_server.rpc import ClusterClient

sys.path.insert(0, str(Path(__file__).parents[2]))

from scripts import recycle_rooms as worker

NOW = datetime(2026, 9, 10, 23, tzinfo=UTC)
HEALTH = DriverHealth(
    protocol=2,
    generation=1,
    telemetry_status="active",
    last_error=None,
    events_emitted=0,
    errors=0,
)


def ready_status(suffix: str = "OLD") -> ClusterStatus:
    return ClusterStatus(
        epoch=ULID(),
        phase="running",
        prepared_revision=None,
        master="forest",
        shards=tuple(
            ShardRuntimeStatus(
                name=name,
                is_master=name == "forest",
                desired=ShardDesired.RUNNING,
                phase=ShardPhase.RUNNING,
                session_id=f"{name}_{suffix}",
                ready=True,
                telemetry_profile="history",
                driver_health=HEALTH,
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
    manager = Mock()
    manager.Manager.ListUnitsByNames.return_value = [
        (b"dst-000-pod.service", b"", b"loaded", b"active", b"", b"", b"/unit", 0)
    ]
    status = ready_status()
    client = Mock(spec=ClusterClient)
    client.status = AsyncMock(side_effect=[status, status, ready_status("NEW")])
    client.list_players = AsyncMock(return_value=())
    client.shard.return_value.world = AsyncMock(return_value=Mock(day=8))
    client.shard.return_value.connected_shards = AsyncMock(
        return_value=(Mock(ready=True), Mock(ready=True))
    )
    client.regenerate = AsyncMock()
    for shard in status.shards:
        assert shard.session_id is not None
        write_last_login(
            tmp_path / shard.name, shard.session_id, NOW - timedelta(days=10)
        )
    return manager, client, status


@pytest.mark.parametrize(
    ("day", "hours", "recent_shard"),
    [
        (8, 3, "forest"),
        (9, 12, "cave"),
        (30, 12, "forest"),
        (31, 36, "cave"),
        (70, 36, "forest"),
        (71, 120, "cave"),
        (280, 120, "forest"),
        (281, 168, "cave"),
    ],
)
@pytest.mark.parametrize("excess_seconds", [-1, 0, 1])
async def test_recycle_uses_latest_shard_login_and_strict_day_limit(
    room: tuple[Mock, Mock, ClusterStatus],
    tmp_path: Path,
    day: int,
    hours: int,
    recent_shard: str,
    excess_seconds: int,
) -> None:
    manager, client, _ = room
    assert worker.idle_limit(day) == timedelta(hours=hours)
    client.shard.return_value.world.return_value = Mock(day=day)
    write_last_login(
        tmp_path / recent_shard,
        f"{recent_shard}_OLD",
        NOW - timedelta(hours=hours, seconds=excess_seconds),
    )
    assert await worker.recycle(manager, client, tmp_path, 0) == (excess_seconds > 0)
    if excess_seconds > 0:
        client.regenerate.assert_awaited_once_with(
            expected_session_id="forest_OLD", require_empty=True
        )
    else:
        client.regenerate.assert_not_awaited()


def test_online_rooms_requires_open_loaded_active_idle_units() -> None:
    manager = Mock()
    manager.Manager.ListUnitsByNames.return_value = [
        (
            f"dst-{number:03d}-pod.service".encode(),
            b"",
            load,
            active,
            b"",
            b"",
            b"/unit",
            job,
        )
        for number, load, active, job in (
            (0, b"loaded", b"active", 0),
            (1, b"not-found", b"active", 0),
            (2, b"loaded", b"inactive", 0),
            (3, b"loaded", b"activating", 0),
            (20, b"loaded", b"active", 1),
            (99, b"loaded", b"active", 0),
        )
    ]
    assert worker.online_rooms(manager, [-1, 0, 1, 2, 3, 4, 20, 99, 100], NOW) == {
        0,
        99,
    }
    manager.Manager.ListUnitsByNames.assert_called_once_with([
        f"dst-{number:03d}-pod.service".encode() for number in (0, 1, 2, 3, 20, 99)
    ])
    manager.reset_mock()
    assert worker.online_rooms(manager, [-1, 4, 100], NOW) == set()
    manager.Manager.ListUnitsByNames.assert_not_called()


@pytest.mark.parametrize("contents", [(None, None), (b"invalid", None), (b"", b"\xff")])
async def test_recycle_skips_worlds_without_readable_activity(
    room: tuple[Mock, Mock, ClusterStatus],
    tmp_path: Path,
    contents: tuple[bytes | None, bytes | None],
) -> None:
    manager, client, status = room
    for shard, content in zip(status.shards, contents, strict=True):
        assert shard.session_id is not None
        marker = (
            tmp_path / shard.name / "save/session" / shard.session_id / ".last_login"
        )
        if content is None:
            marker.unlink()
        else:
            marker.write_bytes(content)
    assert not await worker.recycle(manager, client, tmp_path, 0)
    client.regenerate.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"ready": False},
        {"desired": ShardDesired.STOPPED},
        {"phase": ShardPhase.STARTING},
        {"session_id": None},
        {"driver_health": None},
        {"driver_health": HEALTH.replace(errors=1)},
        {"driver_health": HEALTH.replace(telemetry_status="degraded")},
        {"driver_error": "driver failed"},
        {"telemetry_invalid": 1},
        {"telemetry_dropped": 1},
    ],
)
async def test_recycle_skips_unready_or_incomplete_observation(
    room: tuple[Mock, Mock, ClusterStatus], tmp_path: Path, changes: dict[str, object]
) -> None:
    manager, client, status = room
    client.status.side_effect = [
        status.replace(shards=(status.shards[0].replace(**changes), status.shards[1]))
    ]
    assert not await worker.recycle(manager, client, tmp_path, 0)
    client.regenerate.assert_not_awaited()


@pytest.mark.parametrize("connections", [(True,), (True, False)])
async def test_recycle_requires_connected_game_shards(
    room: tuple[Mock, Mock, ClusterStatus],
    tmp_path: Path,
    connections: tuple[bool, ...],
) -> None:
    manager, client, _ = room
    client.shard.return_value.connected_shards.return_value = tuple(
        Mock(ready=ready) for ready in connections
    )
    assert not await worker.recycle(manager, client, tmp_path, 0)
    client.regenerate.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "dry-run",
        "players",
        "late-player",
        "session",
        "state",
        "marker",
        "one-session",
        "missing-shard",
    ],
)
async def test_recycle_rechecks_inputs_and_confirms_every_new_session(
    room: tuple[Mock, Mock, ClusterStatus], tmp_path: Path, change: str
) -> None:
    manager, client, status = room
    match change:
        case "players":
            client.list_players.return_value = (Mock(userid="KU_ONLINE"),)
        case "late-player":
            client.list_players.side_effect = [(), (Mock(userid="KU_ONLINE"),)]
        case "session":
            client.status.side_effect = [status, ready_status("CHANGED")]
        case "state":
            client.status.side_effect = [status, status.replace(phase="starting")]
        case "marker":

            def renew_login(*_: object) -> list[tuple[object, ...]]:
                write_last_login(tmp_path / "cave", "cave_OLD", NOW)
                return manager.Manager.ListUnitsByNames.return_value

            manager.Manager.ListUnitsByNames.side_effect = renew_login
        case "one-session" | "missing-shard":
            changed = ready_status("NEW")
            shards = (
                (changed.shards[0], status.shards[1])
                if change == "one-session"
                else changed.shards[:1]
            )
            client.status.side_effect = [
                status,
                status,
                changed.replace(shards=shards),
            ]
    if change in {"one-session", "missing-shard"}:
        with pytest.raises(
            RuntimeError, match="did not confirm new worlds on every shard"
        ):
            await worker.recycle(manager, client, tmp_path, 0)
    else:
        assert await worker.recycle(
            manager, client, tmp_path, 0, dry_run=change == "dry-run"
        ) == (change in {"none", "dry-run"})
    if change in {"none", "one-session", "missing-shard"}:
        client.regenerate.assert_awaited_once_with(
            expected_session_id="forest_OLD", require_empty=True
        )
    else:
        client.regenerate.assert_not_awaited()
