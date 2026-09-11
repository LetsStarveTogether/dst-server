import asyncio
import fcntl
import json
import os
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call

import pytest
from pydantic import SecretStr
from ulid import ULID

from dst_server.activity import write_last_login
from dst_server.concurrency import cancel_tasks
from dst_server.host import Host, schedule
from dst_server.host import recycling as worker
from dst_server.host.locking import room_lock
from dst_server.host.systemd import Systemd, UnitStatus
from dst_server.models.cluster import (
    ClusterStatus,
    ShardDesired,
    ShardPhase,
    ShardRuntimeStatus,
)
from dst_server.models.driver import DriverHealth
from dst_server.presets.lst import fleet_room
from dst_server.rooms import (
    CONTROL_FILE,
    DailyWindow,
    RoomStore,
    read_control,
    write_control,
)
from dst_server.rpc import ClusterClient

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
    host = Mock()
    host.status = AsyncMock(
        return_value={"load": "loaded", "active": "active", "job_id": 0}
    )
    host.rooms.policy.return_value = SimpleNamespace(recycle=True, schedule=())
    host.rooms.path.return_value = tmp_path
    monkeypatch.setattr(worker, "effective_state", lambda *_: None)
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
    return host, client, status


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
    host, client, _ = room
    assert worker.idle_limit(day) == timedelta(hours=hours)
    client.shard.return_value.world.return_value = Mock(day=day)
    write_last_login(
        tmp_path / recent_shard,
        f"{recent_shard}_OLD",
        NOW - timedelta(hours=hours, seconds=excess_seconds),
    )
    assert await worker.recycle(host, client, tmp_path, 0) == (excess_seconds > 0)
    if excess_seconds > 0:
        client.regenerate.assert_awaited_once_with(
            expected_session_id="forest_OLD", require_empty=True
        )
    else:
        client.regenerate.assert_not_awaited()


@pytest.mark.parametrize(
    ("load", "active", "job", "recycle", "schedule", "state", "expected"),
    [
        ("loaded", "active", 0, True, (), None, True),
        ("not-found", "active", 0, True, (), None, False),
        ("loaded", "inactive", 0, True, (), None, False),
        ("loaded", "activating", 0, True, (), None, False),
        ("loaded", "active", 1, True, (), None, False),
        ("loaded", "active", 0, False, (), None, False),
        ("loaded", "active", 0, True, ("window",), None, False),
        ("loaded", "active", 0, True, ("window",), False, False),
        ("loaded", "active", 0, True, ("window",), True, True),
        ("loaded", "active", 0, True, (), False, False),
    ],
)
async def test_online_requires_enabled_open_loaded_active_idle_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load: str,
    active: str,
    job: int,
    recycle: bool,
    schedule: tuple[str, ...],
    state: bool | None,
    expected: bool,
) -> None:
    host = Mock()
    host.rooms.policy.return_value = SimpleNamespace(recycle=recycle, schedule=schedule)
    host.rooms.path.return_value = tmp_path
    host.status = AsyncMock(
        return_value={"load": load, "active": active, "job_id": job}
    )
    monkeypatch.setattr(worker, "effective_state", lambda *_: state)
    assert await worker.online(host, 140, NOW) == expected
    host.rooms.policy.assert_called_once_with(140)


@pytest.mark.parametrize("contents", [(None, None), (b"invalid", None), (b"", b"\xff")])
async def test_recycle_skips_worlds_without_readable_activity(
    room: tuple[Mock, Mock, ClusterStatus],
    tmp_path: Path,
    contents: tuple[bytes | None, bytes | None],
) -> None:
    host, client, status = room
    for shard, content in zip(status.shards, contents, strict=True):
        assert shard.session_id is not None
        marker = (
            tmp_path / shard.name / "save/session" / shard.session_id / ".last_login"
        )
        if content is None:
            marker.unlink()
        else:
            marker.write_bytes(content)
    assert not await worker.recycle(host, client, tmp_path, 0)
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
    host, client, status = room
    client.status.side_effect = [
        status.replace(shards=(status.shards[0].replace(**changes), status.shards[1]))
    ]
    assert not await worker.recycle(host, client, tmp_path, 0)
    client.regenerate.assert_not_awaited()


@pytest.mark.parametrize("connections", [(True,), (True, False)])
async def test_recycle_requires_connected_game_shards(
    room: tuple[Mock, Mock, ClusterStatus],
    tmp_path: Path,
    connections: tuple[bool, ...],
) -> None:
    host, client, _ = room
    client.shard.return_value.connected_shards.return_value = tuple(
        Mock(ready=ready) for ready in connections
    )
    assert not await worker.recycle(host, client, tmp_path, 0)
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
    host, client, status = room
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

            def renew_login(*_: object, **_kwargs: object) -> dict[str, object]:
                write_last_login(tmp_path / "cave", "cave_OLD", NOW)
                return host.status.return_value

            host.status.side_effect = renew_login
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
            await worker.recycle(host, client, tmp_path, 0)
    else:
        assert await worker.recycle(
            host, client, tmp_path, 0, dry_run=change == "dry-run"
        ) == (change in {"none", "dry-run"})
    if change in {"none", "one-session", "missing-shard"}:
        client.regenerate.assert_awaited_once_with(
            expected_session_id="forest_OLD", require_empty=True
        )
    else:
        client.regenerate.assert_not_awaited()


async def test_run_recycle_preserves_partial_results_and_skips_disabled_rooms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definitions = {
        number: SimpleNamespace(number=number, recycle=number != 4, schedule=())
        for number in range(1, 5)
    }
    host = Mock()
    host.rooms.numbers.return_value = tuple(definitions)
    host.rooms.policy.side_effect = definitions.__getitem__
    host.rooms.path.return_value = tmp_path
    host.status = AsyncMock(
        side_effect=[
            {"load": "loaded", "active": "active", "job_id": 0},
            RuntimeError("status unavailable"),
            {"load": "loaded", "active": "active", "job_id": 0},
        ]
    )
    connection = AsyncMock()
    host.connect.return_value = connection
    monkeypatch.setattr(worker, "effective_state", lambda *_: None)
    regenerate = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(worker, "recycle", regenerate)
    assert await worker.run_recycle(host, dry_run=True) == {
        1: {"status": "would-regenerate"},
        2: {"status": "failed", "error": "status unavailable"},
        3: {"status": "skipped"},
        4: {"status": "skipped"},
    }
    assert host.connect.call_count == 2
    assert regenerate.await_count == 2
    assert all(call.kwargs["dry_run"] for call in regenerate.await_args_list)


async def test_malformed_policy_does_not_block_other_rooms(tmp_path: Path) -> None:
    host = Mock()
    host.rooms = RoomStore(tmp_path)
    host.status = AsyncMock()
    host.rooms.save(fleet_room(1, token=SecretStr("test")).replace(recycle=False))
    broken = host.rooms.path(0)
    host.rooms.save(fleet_room(0, token=SecretStr("test")))
    (broken / CONTROL_FILE).write_text("{")
    results = await worker.run_recycle(host)
    assert results[0]["status"] == "failed"
    assert "Invalid JSON" in results[0]["error"]
    assert results[1] == {"status": "skipped"}
    host.status.assert_not_awaited()
    host.connect.assert_not_called()


@pytest.fixture
def managed_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Host, Mock, Mock]:
    clock = Mock(wraps=datetime)
    clock.now.return_value = NOW
    monkeypatch.setattr(worker, "datetime", clock)
    manager = Mock(spec=Systemd)
    manager.list_units.side_effect = lambda names: {
        name: UnitStatus(name, "loaded", "active", "running", 0, "", "/")
        for name in names
    }
    manager.stop.return_value = "stop-job"
    host = Host(tmp_path / "rooms", tmp_path / "quadlets", systemd=manager)
    host.rooms.save(fleet_room(0, token=SecretStr("test-token")))
    status = ready_status()
    client = MagicMock(spec=ClusterClient)
    client.__aenter__.return_value = client
    client.status.side_effect = [status, status, ready_status("NEW")]
    client.list_players.return_value = ()
    client.shard.return_value.world = AsyncMock(return_value=Mock(day=8))
    client.shard.return_value.connected_shards = AsyncMock(
        return_value=(Mock(ready=True), Mock(ready=True))
    )
    for shard in status.shards:
        assert shard.session_id is not None
        write_last_login(
            host.rooms.path(0) / shard.name,
            shard.session_id,
            NOW - timedelta(days=10),
        )
    return host, client, clock


async def test_paused_unscheduled_room_is_not_recycled(
    managed_room: tuple[Host, Mock, Mock],
) -> None:
    host, client, _ = managed_room
    assert not host.rooms.policy(0).schedule
    assert await schedule.set_paused(host, [0], True) == {0: {"status": "paused"}}
    assert not await worker.online(host, 0, NOW)
    assert not await worker.recycle(host, client, host.rooms.path(0), 0)
    client.regenerate.assert_not_awaited()


@pytest.mark.parametrize("change", ["pause", "stop", "policy", "closing"])
async def test_control_and_eligibility_changes_prevent_regeneration(
    managed_room: tuple[Host, Mock, Mock], change: str
) -> None:
    host, client, clock = managed_room
    directory = host.rooms.path(0)
    if change == "closing":
        hour = NOW.astimezone().hour
        write_control(
            directory,
            read_control(directory).model_copy(
                update={
                    "schedule": (
                        DailyWindow(start=time(hour), end=time((hour + 1) % 24)),
                    ),
                }
            ),
        )

    async def players() -> tuple:
        if client.list_players.await_count == 2:
            match change:
                case "pause":
                    await schedule.set_paused(host, [0], True)
                case "stop":
                    await host.stop(0, wait=False)
                case "policy":
                    write_control(
                        directory,
                        read_control(directory).model_copy(
                            update={
                                "recycle": False,
                            }
                        ),
                    )
                case "closing":
                    clock.now.return_value = NOW + timedelta(hours=1)
        return ()

    client.list_players.side_effect = players
    assert not await worker.recycle(host, client, directory, 0)
    assert client.list_players.await_count == 2
    client.regenerate.assert_not_awaited()


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_rpc_submits_under_lock_and_waits_without_holding_it(
    managed_room: tuple[Host, Mock, Mock], cancel: bool
) -> None:
    host, client, _ = managed_room
    directory = host.rooms.path(0)
    submitted = asyncio.Event()
    response = asyncio.get_running_loop().create_future()

    def call(*, request: bytes) -> asyncio.Future:
        payload = json.loads(request)
        assert payload["method"] == "regenerate"
        assert payload["arguments"] == {
            "expected_session_id": "forest_OLD",
            "require_empty": True,
        }
        descriptor = os.open(directory / ".dst-operation.lock", os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        submitted.set()
        return response

    capability = Mock(call=Mock(side_effect=call))
    real = ClusterClient(Mock(), Mock(), capability)
    client.regenerate = real.regenerate
    task = asyncio.create_task(worker.recycle(host, client, directory, 0))
    try:
        await asyncio.wait_for(submitted.wait(), timeout=1)
        # Manual controls can acquire the lock while the game RPC is unfinished.
        async with room_lock(directory, wait=False):
            assert not response.done()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            assert response.cancelled()
        else:
            outcome = Mock(value=b"null")
            outcome.which.return_value = "value"
            response.set_result(Mock(result=outcome))
            assert await asyncio.wait_for(task, timeout=1)
        capability.call.assert_called_once()
    finally:
        await cancel_tasks(task)
        real.close()


async def test_batch_cancellation_stops_before_next_room(
    managed_room: tuple[Host, Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    host, client, _ = managed_room
    host.rooms.save(fleet_room(1, token=SecretStr("test-token")))
    connected = AsyncMock(return_value=client)
    monkeypatch.setattr(ClusterClient, "connect", connected)
    submitted = asyncio.Event()
    response = asyncio.get_running_loop().create_future()

    def submit(*, request: bytes) -> asyncio.Future:
        assert json.loads(request)["method"] == "regenerate"
        submitted.set()
        return response

    capability = Mock(call=Mock(side_effect=submit))
    real = ClusterClient(Mock(), Mock(), capability)
    client.regenerate = real.regenerate
    task = asyncio.create_task(worker.run_recycle(host))
    try:
        await asyncio.wait_for(submitted.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert task.cancelled()
        assert response.cancelled()
        assert connected.await_args_list == [
            call(host.rooms.path(0) / ".dst-server.sock")
        ]
        capability.call.assert_called_once()
        client.__aexit__.assert_awaited_once()
    finally:
        await cancel_tasks(task)
        real.close()


async def test_recycling_uses_runtime_worlds_with_dynamic_configuration(
    managed_room: tuple[Host, Mock, Mock],
) -> None:
    host, client, _ = managed_room
    directory = host.rooms.path(0)
    world = directory / "forest/worldgenoverride.lua"
    world.write_text("return require('custom_world')\n")
    before = world.read_bytes()

    assert await worker.recycle(host, client, directory, 0)
    client.regenerate.assert_awaited_once_with(
        expected_session_id="forest_OLD", require_empty=True
    )
    assert world.read_bytes() == before
