import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, call
from zoneinfo import ZoneInfo

import pytest

from dst_server.rpc import ClusterClient

sys.path.insert(0, str(Path(__file__).parents[2]))

from scripts import manage_rooms as worker
from scripts.manage_rooms import check, is_open

TIMEZONE = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("number", "hours"),
    [
        (4, {9, 10, 11}),
        (8, {13, 14, 15, 16, 17}),
        (12, {19, 20, 21, 22, 23}),
        (16, {22, 23, 0, 1, 2, 3, 4}),
        (0, set(range(24))),
        (100, set(range(24))),
    ],
)
def test_open_hours_include_start_and_exclude_end(number: int, hours: set[int]) -> None:
    for hour in range(24):
        for minute, second in ((0, 0), (59, 59)):
            now = datetime(2026, 9, 9, hour, minute, second, tzinfo=TIMEZONE)
            assert is_open(number, now) == (hour in hours)


@pytest.mark.parametrize("numbers", [[], [0, 20, 50, 70, 100, 139]])
def test_no_scheduled_rooms_does_not_query_systemd(numbers: list[int]) -> None:
    manager = Mock()

    assert check(manager, numbers, datetime(2026, 9, 9, 10, tzinfo=TIMEZONE)) == 0
    assert manager.mock_calls == []


@pytest.mark.parametrize(
    ("number", "load", "active", "action", "result"),
    [
        (4, b"loaded", b"inactive", "StartUnit", 0),
        (4, b"loaded", b"active", None, 0),
        (4, b"loaded", b"activating", None, 0),
        (4, b"loaded", b"deactivating", None, 0),
        (8, b"loaded", b"inactive", None, 0),
        (8, b"loaded", b"active", "StopUnit", 0),
        (8, b"loaded", b"activating", "StopUnit", 0),
        (8, b"loaded", b"deactivating", None, 0),
        (4, b"masked", b"inactive", None, 0),
        (8, b"masked", b"active", None, 0),
        (4, b"loaded", b"failed", None, 1),
        (8, b"loaded", b"failed", None, 1),
        (4, b"not-found", b"inactive", None, 1),
    ],
)
def test_check_uses_loaded_and_active_states(
    number: int, load: bytes, active: bytes, action: str | None, result: int
) -> None:
    name = f"dst-{number:03d}-pod.service".encode()
    manager = Mock()
    manager.Manager.ListUnitsByNames.return_value = [
        (name, b"", load, active, b"", b"", b"/unit", 0, b"", b"/")
    ]
    manager.Manager.StartUnit.return_value = b"/job/1"
    manager.Manager.StopUnit.return_value = b"/job/2"

    assert (
        check(manager, [0, number, 100], datetime(2026, 9, 9, 10, tzinfo=TIMEZONE))
        == result
    )
    expected = [call.Manager.ListUnitsByNames([name])]
    if action:
        expected.append(getattr(call.Manager, action)(name, b"replace"))
    assert manager.mock_calls == expected


@pytest.mark.parametrize("fail_stop", [False, True])
def test_check_stops_before_starting_and_continues_after_request_failure(
    fail_stop: bool,
) -> None:
    manager = Mock()
    morning = b"dst-004-pod.service"
    lunch = b"dst-008-pod.service"
    another_lunch = b"dst-009-pod.service"
    manager.Manager.ListUnitsByNames.return_value = [
        (name, b"", b"loaded", active, b"", b"", b"/unit", 0, b"", b"/")
        for name, active in (
            (morning, b"inactive"),
            (lunch, b"active"),
            (another_lunch, b"activating"),
        )
    ]
    manager.Manager.StopUnit.side_effect = [
        RuntimeError("D-Bus request failed") if fail_stop else b"/job/1",
        b"/job/2",
    ]
    manager.Manager.StartUnit.return_value = b"/job/3"

    assert (
        check(manager, [4, 8, 9], datetime(2026, 9, 9, 10, tzinfo=TIMEZONE))
        == fail_stop
    )
    assert manager.mock_calls == [
        call.Manager.ListUnitsByNames([morning, lunch, another_lunch]),
        call.Manager.StopUnit(lunch, b"replace"),
        call.Manager.StopUnit(another_lunch, b"replace"),
        call.Manager.StartUnit(morning, b"replace"),
    ]


@pytest.mark.parametrize(
    ("hour", "active", "job_type", "stop"),
    [
        (9, b"inactive", b"start", False),
        (12, b"inactive", b"start", True),
        (9, b"inactive", b"restart", False),
        (12, b"inactive", b"restart", True),
        (9, b"inactive", b"stop", False),
        (12, b"inactive", b"stop", False),
        (9, b"active", b"stop", False),
        (12, b"active", b"stop", False),
    ],
)
def test_pending_jobs_are_preserved_except_starting_after_closure(
    hour: int, active: bytes, job_type: bytes, stop: bool
) -> None:
    name = b"dst-004-pod.service"
    manager = Mock()
    manager.Manager.ListUnitsByNames.return_value = [
        (name, b"", b"loaded", active, b"", b"", b"/unit", 1, job_type, b"/job/1")
    ]
    manager.Manager.StopUnit.return_value = b"/job/2"

    assert check(manager, [4], datetime(2026, 9, 9, hour, tzinfo=TIMEZONE)) == 0
    expected = [call.Manager.ListUnitsByNames([name])]
    if stop:
        expected.append(call.Manager.StopUnit(name, b"replace"))
    assert manager.mock_calls == expected


@pytest.mark.parametrize(
    ("number", "end", "start"), [(4, 12, 9), (8, 18, 13), (12, 0, 19), (16, 5, 22)]
)
@pytest.mark.parametrize("minutes", range(1, 9))
@pytest.mark.parametrize("jitter_seconds", [0, 1, 59])
def test_shutdown_notices_every_minute_before_closure(
    number: int, end: int, start: int, minutes: int, jitter_seconds: int
) -> None:
    closing = datetime(2026, 9, 10, end, tzinfo=TIMEZONE)
    now = closing - timedelta(minutes=minutes, seconds=-jitter_seconds)
    message = worker.shutdown_notice(number, now)

    assert message == (
        f"本房间将在约 {minutes} 分钟后定时关闭，请提前安排游戏进度。"  # ruff: ignore[ambiguous-unicode-character-string]
        f"下次开放时间：{start:02d}:00。"  # ruff: ignore[ambiguous-unicode-character-string]
    )


@pytest.mark.parametrize(
    ("number", "hour", "minute"),
    [
        (0, 11, 55),
        (100, 11, 55),
        (4, 11, 51),
        (4, 12, 0),
        (4, 8, 55),
        (4, 10, 55),
        (12, 0, 0),
    ],
)
async def test_no_imminent_closure_does_not_query_systemd(
    number: int, hour: int, minute: int
) -> None:
    manager = Mock()
    now = datetime(2026, 9, 9, hour, minute, tzinfo=TIMEZONE)

    assert worker.shutdown_notice(number, now) is None
    assert await worker.announce_closures(manager, [number], now) == 0
    assert manager.mock_calls == []


@pytest.mark.parametrize("failure", [False, True])
async def test_announcements_skip_unavailable_rooms_and_isolate_failures(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    now = datetime(2026, 9, 9, 11, 55).astimezone()
    clock = Mock(wraps=datetime)
    clock.now.return_value = now
    monkeypatch.setattr(worker, "datetime", clock)
    manager = Mock()
    numbers = [4, 5, 6, 7, 26, 27, 28]
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
            (4, b"loaded", b"active", 0),
            (5, b"masked", b"active", 0),
            (6, b"loaded", b"inactive", 0),
            (7, b"loaded", b"activating", 0),
            (26, b"loaded", b"active", 1),
            (27, b"loaded", b"failed", 0),
            (28, b"loaded", b"active", 0),
        )
    ]
    clients = [AsyncMock(spec=ClusterClient), AsyncMock(spec=ClusterClient)]
    for client in clients:
        client.__aenter__.return_value = client
    if failure:
        clients[0].announce.side_effect = RuntimeError("RPC unavailable")
    connect = AsyncMock(side_effect=clients)
    monkeypatch.setattr(worker.ClusterClient, "connect", connect)

    assert (
        await worker.announce_closures(manager, [0, *numbers, 8, 100], now) == failure
    )
    assert manager.mock_calls == [
        call.Manager.ListUnitsByNames([
            f"dst-{number:03d}-pod.service".encode() for number in numbers
        ])
    ]
    assert connect.await_args_list == [
        call(Path(f"/srv/dst/{number:03d}/.dst-server.sock"), timeout=5)
        for number in (4, 28)
    ]
    for client in clients:
        client.announce.assert_awaited_once_with(worker.shutdown_notice(4, now))


@pytest.mark.parametrize("minute", [52, 59])
async def test_announcement_expiring_during_connection_is_discarded(
    monkeypatch: pytest.MonkeyPatch, minute: int
) -> None:
    now = datetime(2026, 9, 9, 11, minute, 59).astimezone()
    clock = Mock(wraps=datetime)
    clock.now.return_value = now + timedelta(seconds=1)
    monkeypatch.setattr(worker, "datetime", clock)
    manager = Mock()
    manager.Manager.ListUnitsByNames.return_value = [
        (b"dst-004-pod.service", b"", b"loaded", b"active", b"", b"", b"/unit", 0)
    ]
    client = AsyncMock(spec=ClusterClient)
    client.__aenter__.return_value = client
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr(worker.ClusterClient, "connect", connect)

    assert await worker.announce_closures(manager, [4], now) == 0
    connect.assert_awaited_once()
    client.announce.assert_not_awaited()


@pytest.mark.parametrize("check_result", [0, 1])
def test_main_preserves_schedule_result_when_announcement_fails(
    monkeypatch: pytest.MonkeyPatch, check_result: int
) -> None:
    monkeypatch.setattr(worker, "ROOM_NUMBERS", ())
    monkeypatch.setattr(worker, "DBus", MagicMock())
    monkeypatch.setattr(worker, "Manager", Mock())
    check_mock = Mock(return_value=check_result)
    announce = AsyncMock(return_value=1)
    monkeypatch.setattr(worker, "check", check_mock)
    monkeypatch.setattr(worker, "announce_closures", announce)

    assert worker.main() == check_result
    check_mock.assert_called_once()
    announce.assert_awaited_once_with(*check_mock.call_args.args)
