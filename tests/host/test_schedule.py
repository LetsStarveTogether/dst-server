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
    read_control,
    write_control,
)

TIMEZONE = ZoneInfo("Asia/Shanghai")


def make_room(number: int = 16, *windows: tuple[str, str]) -> Room:
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
        (16, set(range(10, 18))),
        (20, set(range(18, 24))),
        (28, set(range(8))),
        (66, set(range(10, 18))),
        (67, set(range(18, 24))),
        (69, set(range(8))),
        (0, set(range(24))),
        (200, set(range(24))),
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


def test_unscheduled_and_paused_are_not_automatically_managed() -> None:
    assert schedule.effective_state(Control(), now(10)) is None
    control = Control(schedule=make_room().schedule, paused=True)
    assert schedule.effective_state(control, now(10)) is None


@pytest.mark.parametrize(
    ("number", "closing", "opening"),
    [(16, 18, "10:00"), (20, 0, "18:00"), (28, 8, "00:00")],
)
@pytest.mark.parametrize("minutes", range(1, 9))
@pytest.mark.parametrize("seconds", [0, 1, 59])
def test_shutdown_notice_uses_minutes_then_seconds_in_the_final_minute(
    number: int, closing: int, opening: str, minutes: int, seconds: int
) -> None:
    instant = now(closing) - timedelta(minutes=minutes, seconds=-seconds)
    remaining = (
        f"{60 - seconds} 秒" if minutes == 1 and seconds else f"约 {minutes} 分钟"
    )
    assert schedule.shutdown_notice(
        Control(schedule=make_room(number).schedule), instant
    ) == (
        f"本房间将在{remaining}后定时关闭，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
        f"下次开放时间：{opening}。"  # ruff: ignore[ambiguous-unicode-character-string]
    )


def fake_host(tmp_path: Path, numbers: tuple[int, ...]) -> Mock:
    host = Mock()
    rooms = {number: make_room(number) for number in numbers}
    for number in numbers:
        (tmp_path / str(number)).mkdir()
    host.rooms.numbers.return_value = tuple(rooms)

    def policy(number: int) -> Control:
        directory = tmp_path / str(number)
        return (
            read_control(directory)
            if (directory / CONTROL_FILE).exists()
            else Control(schedule=rooms[number].schedule)
        )

    host.rooms.policy.side_effect = policy
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


@pytest.mark.parametrize(
    ("hour", "load", "active", "job_type", "expected"),
    [
        (10, "loaded", "inactive", "", "started"),
        (10, "loaded", "active", "", "unchanged"),
        (10, "loaded", "activating", "", "unchanged"),
        (10, "loaded", "deactivating", "", "unchanged"),
        (18, "loaded", "inactive", "", "unchanged"),
        (18, "loaded", "active", "", "stopped"),
        (18, "loaded", "activating", "", "stopped"),
        (18, "loaded", "deactivating", "", "unchanged"),
        (10, "masked", "inactive", "", "unchanged"),
        (18, "masked", "active", "", "unchanged"),
        (10, "loaded", "failed", "", "failed"),
        (18, "loaded", "failed", "", "failed"),
        (10, "not-found", "inactive", "", "failed"),
        (10, "loaded", "inactive", "start", "unchanged"),
        (10, "loaded", "inactive", "restart", "unchanged"),
        (10, "loaded", "inactive", "stop", "unchanged"),
        (18, "loaded", "inactive", "stop", "unchanged"),
        (10, "loaded", "active", "stop", "unchanged"),
        (18, "loaded", "active", "stop", "unchanged"),
    ],
)
async def test_schedule_respects_unit_state_and_pending_jobs(
    tmp_path: Path, hour: int, load: str, active: str, job_type: str, expected: str
) -> None:
    host = fake_host(tmp_path, (16,))
    host.status.return_value.update(
        load=load, active=active, job_id=int(bool(job_type)), job_type=job_type
    )
    result = await schedule.run_schedule(host, now(hour))
    assert result[16]["status"] == expected
    assert host.start.await_count == (expected == "started")
    assert host.stop.await_count == (expected == "stopped")


async def test_each_schedule_run_announces_without_persistence_and_respects_manual_stop(
    tmp_path: Path,
) -> None:
    host = fake_host(tmp_path, (16,))
    await schedule.run_schedule(host, now(17, 55))
    await schedule.run_schedule(host, now(17, 55, 20))
    assert host.announce.await_count == 2
    assert not (host.rooms.path(16) / CONTROL_FILE).exists()
    await schedule.set_paused(host, (16,), True)
    results = await schedule.run_schedule(host, now(17, 56))
    assert results[16]["status"] == "paused"
    host.stop.assert_not_awaited()
    assert host.announce.await_count == 2


@pytest.mark.parametrize("job_type", ["start", "restart"])
async def test_closure_stops_pending_start(tmp_path: Path, job_type: str) -> None:
    host = fake_host(tmp_path, (16,))
    host.status.return_value.update(active="inactive", job_id=1, job_type=job_type)
    assert (await schedule.run_schedule(host, now(18)))[16]["status"] == "stopped"
    host.stop.assert_awaited_once()
    host.start.assert_not_awaited()


async def test_room_errors_do_not_cancel_other_rooms(tmp_path: Path) -> None:
    host = fake_host(tmp_path, (16, 17))
    host.status.return_value["active"] = "inactive"
    host.start.side_effect = [RuntimeError("unavailable"), None]
    result = await schedule.run_schedule(host, now(10))
    assert result[16] == {"status": "failed", "error": "unavailable"}
    assert result[17] == {"status": "started"}


async def test_announcement_errors_do_not_cancel_other_rooms(tmp_path: Path) -> None:
    host = fake_host(tmp_path, (16, 17))
    host.announce.side_effect = [RuntimeError("unavailable"), None]
    result = await schedule.run_schedule(host, now(17, 55))
    assert result[16] == {"status": "failed", "error": "unavailable"}
    assert "announcement" in result[17]


async def test_pausing_and_resuming_controls_automation(tmp_path: Path) -> None:
    host = fake_host(tmp_path, (16,))
    await schedule.set_paused(host, (16,), True)
    assert schedule.effective_state(host.rooms.policy(16), now(10)) is None
    await schedule.set_paused(host, (16,), False)
    assert schedule.effective_state(host.rooms.policy(16), now(10)) is True


async def test_pause_after_status_check_prevents_shutdown_announcement(
    tmp_path: Path,
) -> None:
    host = fake_host(tmp_path, (16,))

    def status(*_: object, **__: object) -> object:
        control = host.rooms.policy(16)
        write_control(host.rooms.path(16), control.model_copy(update={"paused": True}))
        return host.status.return_value

    host.status.side_effect = status
    assert await schedule.run_schedule(host, now(17, 55)) == {
        16: {"status": "unchanged"}
    }
    host.announce.assert_not_awaited()


async def test_notice_expiring_during_connection_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = fake_host(tmp_path, (16,))
    clock = Mock(side_effect=[now(17, 59, 59), now(17, 59, 59), now(18)])
    monkeypatch.setattr(schedule, "local_now", clock)
    await schedule.run_schedule(host)
    host.announce.assert_not_awaited()


async def test_pause_continues_after_room_failure(tmp_path: Path) -> None:
    host = fake_host(tmp_path, (16, 17))
    host.rooms.policy.side_effect = [
        ValueError("invalid room"),
        Control(schedule=make_room(17).schedule),
    ]
    result = await schedule.set_paused(host, (16, 17), True)
    assert result[16] == {"status": "failed", "error": "invalid room"}
    assert result[17] == {"status": "paused"}


async def test_malformed_policy_does_not_stop_other_scheduled_rooms(
    tmp_path: Path,
) -> None:
    host = fake_host(tmp_path, (16, 17))
    host.rooms = RoomStore(tmp_path)
    host.rooms.save(make_room(16))
    host.rooms.save(make_room(17))
    (host.rooms.path(17) / CONTROL_FILE).write_text("invalid JSON")
    host.status.return_value["active"] = "inactive"
    result = await schedule.run_schedule(host, now(10))
    assert result[16]["status"] == "started"
    assert result[17]["status"] == "failed"
    host.start.assert_awaited_once_with(
        16,
        automatic=True,
        wait=False,
    )


async def test_schedule_skips_busy_room_without_waiting(
    tmp_path: Path,
) -> None:
    from dst_server.host.locking import room_lock

    host = fake_host(tmp_path, (16,))
    host.rooms = RoomStore(tmp_path)
    room = make_room(16)
    host.rooms.save(room)
    async with room_lock(host.rooms.path(16)):
        assert await schedule.run_schedule(host, now(10)) == {16: {"status": "busy"}}
    host.start.assert_not_awaited()


async def test_schedule_and_pause_do_not_parse_dynamic_game_configuration(
    tmp_path: Path,
) -> None:
    host = fake_host(tmp_path, (16,))
    host.rooms = RoomStore(tmp_path)
    host.rooms.save(make_room(16))
    world = host.rooms.path(16) / "forest/worldgenoverride.lua"
    world.write_text("return require('custom_world')\n")
    before = world.read_bytes()
    host.status.return_value["active"] = "inactive"

    result = await schedule.run_schedule(host, now(10))
    assert result[16] == {"status": "started"}
    assert await schedule.set_paused(host, (16,), True) == {16: {"status": "paused"}}
    assert read_control(host.rooms.path(16)).schedule == make_room(16).schedule
    assert world.read_bytes() == before
