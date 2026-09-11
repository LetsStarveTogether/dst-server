from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from dst_server.host import schedule
from dst_server.presets.lst import fleet_room
from dst_server.rooms import (
    CONTROL_FILE,
    Control,
    DailyWindow,
    Room,
    RoomStore,
    control_revision,
    read_control,
    write_control,
)

TIMEZONE = ZoneInfo("Asia/Shanghai")


def make_room(number: int = 4, *windows: tuple[str, str]) -> Room:
    room = fleet_room(number, token=SecretStr("token"))
    if windows:
        room = room.replace(
            schedule=tuple(
                DailyWindow(
                    start=time.fromisoformat(start), end=time.fromisoformat(end)
                )
                for start, end in windows
            )
        )
    return room


def now(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, 9, hour, minute, second, tzinfo=TIMEZONE)


@pytest.mark.parametrize(
    ("number", "hours"),
    [
        (4, {9, 10, 11}),
        (8, {13, 14, 15, 16, 17}),
        (12, {19, 20, 21, 22, 23}),
        (16, {22, 23, 0, 1, 2, 3, 4}),
        (0, set(range(24))),
    ],
)
def test_daily_windows_include_start_exclude_end(number: int, hours: set[int]) -> None:
    room = make_room(number)
    for hour in range(24):
        for minute, second in ((0, 0), (59, 59)):
            assert schedule.is_open(room, now(hour, minute, second)) == (hour in hours)


def test_overlapping_touching_and_midnight_windows_merge() -> None:
    room = make_room(0, ("22:00", "03:00"), ("02:00", "05:00"), ("05:00", "09:00"))
    assert schedule.next_boundary(room, now(23)) == now(9) + timedelta(days=1)
    assert schedule.next_boundary(room, now(9)) == now(22)
    all_day = make_room(0, ("00:00", "12:00"), ("12:00", "00:00"))
    assert schedule.next_boundary(all_day, now(9)) is None


def test_manual_stop_expires_at_next_boundary(tmp_path: Path) -> None:
    room = make_room()
    schedule.record_override(tmp_path, room, False, now(10))
    assert control_revision(tmp_path) == 1
    assert schedule.effective_state(tmp_path, room, now(11)) is False
    assert schedule.effective_state(tmp_path, room, now(12)) is False
    assert schedule.effective_state(tmp_path, room, now(9) + timedelta(days=1)) is True
    assert schedule.shutdown_notice(tmp_path, room, now(11, 55)) is None


def test_manual_start_during_closure_follows_effective_next_close(
    tmp_path: Path,
) -> None:
    room = make_room()
    schedule.record_override(tmp_path, room, True, now(8))
    assert schedule.next_close(tmp_path, room, now(8)) == now(12)
    assert schedule.effective_state(tmp_path, room, now(8, 30)) is True
    assert schedule.effective_state(tmp_path, room, now(9)) is True


def test_unscheduled_and_paused_are_not_automatically_managed(tmp_path: Path) -> None:
    schedule.record_override(tmp_path, make_room(0), False, now(10))
    assert schedule.effective_state(tmp_path, make_room(0), now(10)) is None
    write_control(tmp_path, Control(paused=True))
    assert schedule.effective_state(tmp_path, make_room(), now(10)) is None


@pytest.mark.parametrize("minutes", range(1, 9))
@pytest.mark.parametrize("seconds", [0, 1, 59])
def test_shutdown_notice_rounds_remaining_minutes(
    tmp_path: Path, minutes: int, seconds: int
) -> None:
    instant = now(12) - timedelta(minutes=minutes, seconds=-seconds)
    assert schedule.shutdown_notice(tmp_path, make_room(), instant) == (
        f"本房间将在约 {minutes} 分钟后定时关闭，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
        "下次开放时间：09:00。"  # ruff: ignore[ambiguous-unicode-character-string]
    )


def fake_host(tmp_path: Path, numbers: tuple[int, ...]) -> Mock:
    host = Mock()
    rooms = {number: make_room(number) for number in numbers}
    for number in numbers:
        (tmp_path / str(number)).mkdir()
    host.rooms.numbers.return_value = tuple(rooms)
    host.rooms.policy.side_effect = rooms.__getitem__
    host.rooms.path.side_effect = lambda number: tmp_path / str(number)
    host.status = AsyncMock(
        return_value={"load": "loaded", "active": "active", "job_id": 0, "job_type": ""}
    )
    host.start = AsyncMock()
    host.stop = AsyncMock()
    host.announce = AsyncMock()
    connection = AsyncMock()
    connection.__aenter__.return_value = host
    host.connect.return_value = connection
    return host


async def test_each_schedule_run_announces_without_persistence_and_respects_manual_stop(
    tmp_path: Path,
) -> None:
    host = fake_host(tmp_path, (4,))
    await schedule.run_schedule(host, now(11, 55))
    await schedule.run_schedule(host, now(11, 55, 20))
    assert host.announce.await_count == 2
    assert not (host.rooms.path(4) / CONTROL_FILE).exists()
    schedule.record_override(host.rooms.path(4), make_room(), False, now(11, 55, 30))
    results = await schedule.run_schedule(host, now(11, 56))
    assert results[4]["status"] == "stopped"
    host.stop.assert_awaited_once_with(
        4, override=False, wait=False, expected_revision=1
    )
    assert host.announce.await_count == 2


@pytest.mark.parametrize("job_type", ["start", "restart"])
async def test_closure_stops_pending_start(tmp_path: Path, job_type: str) -> None:
    host = fake_host(tmp_path, (4,))
    host.status.return_value.update(active="inactive", job_id=1, job_type=job_type)
    assert (await schedule.run_schedule(host, now(12)))[4]["status"] == "stopped"
    host.stop.assert_awaited_once()
    host.start.assert_not_awaited()


async def test_room_errors_do_not_cancel_other_rooms(tmp_path: Path) -> None:
    host = fake_host(tmp_path, (4, 5))
    host.status.return_value["active"] = "inactive"
    host.start.side_effect = [RuntimeError("unavailable"), None]
    result = await schedule.run_schedule(host, now(10))
    assert result[4] == {"status": "failed", "error": "unavailable"}
    assert result[5] == {"status": "started"}


async def test_pausing_and_resuming_invalidates_overrides(tmp_path: Path) -> None:
    host = fake_host(tmp_path, (4,))
    schedule.record_override(host.rooms.path(4), make_room(), False, now(10))
    await schedule.set_paused(host, (4,), True)
    assert schedule.effective_state(host.rooms.path(4), make_room(), now(10)) is None
    await schedule.set_paused(host, (4,), False)
    assert schedule.effective_state(host.rooms.path(4), make_room(), now(10)) is True
    assert control_revision(host.rooms.path(4)) == 3


def test_automatic_transition_invalidates_pending_work_without_changing_override(
    tmp_path: Path,
) -> None:
    room = make_room()
    schedule.record_override(tmp_path, room, True, now(10))
    before = read_control(tmp_path)
    assert schedule.record_transition(tmp_path, room, False, override=False) == 2
    after = read_control(tmp_path)
    assert after.override == before.override
    assert after.until == before.until
    assert schedule.clear_override(tmp_path) == 3
    assert read_control(tmp_path).override is None


async def test_notice_expiring_during_connection_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = fake_host(tmp_path, (4,))
    clock = Mock(side_effect=[now(11, 59, 59), now(11, 59, 59), now(12)])
    monkeypatch.setattr(schedule, "local_now", clock)
    await schedule.run_schedule(host)
    host.announce.assert_not_awaited()


async def test_pause_continues_after_room_failure(tmp_path: Path) -> None:
    host = fake_host(tmp_path, (4, 5))
    host.rooms.policy.side_effect = [ValueError("invalid room"), make_room(5)]
    result = await schedule.set_paused(host, (4, 5), True)
    assert result[4] == {"status": "failed", "error": "invalid room"}
    assert result[5] == {"status": "paused"}


async def test_malformed_policy_does_not_stop_other_scheduled_rooms(
    tmp_path: Path,
) -> None:
    host = fake_host(tmp_path, (4, 5))
    host.rooms = RoomStore(tmp_path)
    host.rooms.save(make_room(4))
    host.rooms.save(make_room(5))
    (host.rooms.path(5) / CONTROL_FILE).write_text("invalid JSON")
    host.status.return_value["active"] = "inactive"
    result = await schedule.run_schedule(host, now(10))
    assert result[4]["status"] == "started"
    assert result[5]["status"] == "failed"
    host.start.assert_awaited_once_with(
        4,
        override=False,
        wait=False,
        expected_revision=control_revision(host.rooms.path(4)),
    )


async def test_schedule_reads_policy_after_waiting_for_room_lock(
    tmp_path: Path,
) -> None:
    import asyncio

    from dst_server.host.locking import room_lock

    host = fake_host(tmp_path, (4,))
    host.rooms = RoomStore(tmp_path)
    room = make_room(4)
    host.rooms.save(room)
    async with room_lock(host.rooms.path(4)):
        check = asyncio.create_task(schedule.run_schedule(host, now(10)))
        await asyncio.sleep(0)
        host.rooms.save_policy(room.replace(schedule=()))
    assert await check == {4: {"status": "unmanaged"}}
    host.start.assert_not_awaited()


async def test_schedule_and_pause_do_not_parse_dynamic_game_configuration(
    tmp_path: Path,
) -> None:
    host = fake_host(tmp_path, (4,))
    host.rooms = RoomStore(tmp_path)
    host.rooms.save(make_room(4))
    world = host.rooms.path(4) / "forest/worldgenoverride.lua"
    world.write_text("return require('custom_world')\n")
    before = world.read_bytes()
    host.status.return_value["active"] = "inactive"

    result = await schedule.run_schedule(host, now(10))
    assert result[4] == {"status": "started"}
    assert await schedule.set_paused(host, (4,), True) == {4: {"status": "paused"}}
    assert read_control(host.rooms.path(4)).schedule == make_room(4).schedule
    assert world.read_bytes() == before
