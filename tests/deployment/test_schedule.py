import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, call
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).parents[2]))

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
