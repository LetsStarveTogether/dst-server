import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from dst_server import announcements as announcement_module
from dst_server import commands as c
from dst_server.announcements import Countdown, Repeat, Template, maintenance
from dst_server.concurrency import cancel_tasks
from dst_server.models.cluster import ShardDesired
from tests.cluster.helpers import Room, managed_controller, player
from tests.helpers import wait_for_event


@pytest.fixture
async def room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Room]:
    async with managed_controller(tmp_path, monkeypatch) as result:
        instance, master, _, _, _ = result
        if instance._mod_task is not None:
            await cancel_tasks(instance._mod_task)
            instance._mod_task = None
        instance._mod_maintenance.retry_at = 0
        master.players = (player("KU_A", active=True),)
        yield result


@pytest.fixture
def countdown_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = 0.0

    async def sleep(delay: float) -> None:  # ruff: ignore[unused-async]
        nonlocal elapsed
        elapsed += delay

    monkeypatch.setattr(
        announcement_module,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=lambda: elapsed),
            sleep=sleep,
        ),
    )


@pytest.mark.usefixtures("countdown_clock")
@pytest.mark.parametrize("operation", ["stop", "restart", "mods", "shard"])
async def test_lifecycle_announces_repeated_countdown_before_disrupting_players(
    room: Room, operation: str
) -> None:
    instance, master, _, prepare, calls = room
    calls.clear()
    notice = maintenance(Template.RESTART, delay=60, interval=30)
    match operation:
        case "mods":
            await instance.update_mods(restart=True, notice=notice)
        case "shard":
            await instance.shard("Master").restart(notice=notice)
        case _:
            await getattr(instance, operation)(notice=notice)
    announcements = [
        request for request in master.requests if isinstance(request, c.Announce)
    ]
    subject = "分片「Master」" if operation == "shard" else "本房间"
    assert [request.message.split("维护重启")[0] for request in announcements] == [
        f"{subject}将在约 1 分钟后",
        f"{subject}将在30 秒后",
        f"{subject}即将",
    ]
    assert all(request.count == 1 for request in announcements)
    mutation = "restart:Master" if operation == "shard" else "stop:Master"
    assert max(
        index for index, call in enumerate(calls) if call == "announce:Master"
    ) < calls.index(mutation)
    assert prepare.await_count == (2 if operation in {"restart", "mods"} else 1)


async def test_explicit_opt_out_and_empty_room_do_not_start_a_countdown(
    room: Room, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, master, _, _, _ = room
    run = AsyncMock()
    monkeypatch.setattr(Countdown, "run", run)
    await instance.restart(notice=None)
    master.players = ()
    await instance.restart()
    run.assert_not_awaited()


@pytest.mark.usefixtures("countdown_clock")
async def test_sdk_accepts_native_repeat_and_variable_countdown(room: Room) -> None:
    instance, master, _, prepare, _ = room
    await instance.announce(Repeat(message="hello", count=3, interval=4))
    assert master.requests[-1] == c.Announce(message="hello", count=3, interval=4)
    await instance.announce(
        Countdown(template="剩余 {remaining} 秒", delay=60, interval=30)
    )
    messages = [
        request.message
        for request in master.requests
        if isinstance(request, c.Announce)
    ]
    assert messages == ["hello", "剩余 60 秒", "剩余 30 秒", "剩余 0 秒"]
    assert prepare.await_count == 1


async def test_later_shard_stop_does_not_inherit_cancelled_cluster_restart_intent(
    room: Room,
) -> None:
    instance, master, caves, prepare, calls = room
    await instance.shard("Caves").stop(notice=None)
    first_notice = asyncio.Event()
    master.handlers[c.Announce] = AsyncMock(side_effect=lambda _: first_notice.set())
    restarting = asyncio.create_task(instance.restart())
    await wait_for_event(first_notice, restarting)
    assert instance._desired["Caves"] is ShardDesired.STOPPED
    await instance.shard("Master").stop(notice=None)
    result = (await asyncio.gather(restarting, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    assert master.pid is None
    assert caves.pid is None
    assert all(value is ShardDesired.STOPPED for value in instance._desired.values())
    assert prepare.await_count == 1
    assert calls.count("start:Caves") == 1


async def test_restart_rejects_busy_room_and_stop_cancels_current_notice(
    room: Room,
) -> None:
    instance, master, caves, prepare, _ = room
    first_notice = asyncio.Event()
    master.handlers[c.Announce] = AsyncMock(side_effect=lambda _: first_notice.set())
    first = asyncio.create_task(instance.restart())
    await wait_for_event(first_notice, first)
    second = asyncio.create_task(instance.restart())
    final = asyncio.create_task(instance.stop(notice=None))
    async with asyncio.timeout(3):
        results = await asyncio.gather(first, second, final, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], RuntimeError)
    assert "busy" in str(results[1])
    assert results[2] is None
    assert master.pid is None
    assert caves.pid is None
    assert prepare.await_count == 1
    assert sum(isinstance(request, c.Announce) for request in master.requests) == 1


async def test_replaced_mod_notice_is_not_recorded_as_an_update_failure(
    room: Room,
) -> None:
    instance, master, _, prepare, _ = room
    master.outdated_mods = ("Insight",)
    first_notice = asyncio.Event()
    master.handlers[c.Announce] = AsyncMock(side_effect=lambda _: first_notice.set())
    automatic = asyncio.create_task(instance._maintain_mods())
    await wait_for_event(first_notice, automatic)
    await instance.stop(notice=None)
    result = (await asyncio.gather(automatic, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    status = (await instance.status()).mod_update
    assert status.pending
    assert status.error is None
    assert status.retry_in_seconds == 0
    assert prepare.await_count == 1


async def test_lifecycle_deadline_cancels_notice_without_stopping_games(
    room: Room,
) -> None:
    instance, master, caves, prepare, calls = room
    with pytest.raises(TimeoutError):
        await instance.restart(timeout=0.03)
    assert master.ready
    assert caves.ready
    assert not any(call.startswith("stop:") for call in calls)
    assert prepare.await_count == 1


async def test_close_cancels_notice_and_never_restarts_games(room: Room) -> None:
    instance, master, caves, prepare, _ = room
    first_notice = asyncio.Event()
    master.handlers[c.Announce] = AsyncMock(side_effect=lambda _: first_notice.set())
    restarting = asyncio.create_task(instance.restart())
    await wait_for_event(first_notice, restarting)
    await instance.aclose()
    result = (await asyncio.gather(restarting, return_exceptions=True))[0]
    assert isinstance(result, asyncio.CancelledError)
    assert master.pid is None
    assert caves.pid is None
    assert prepare.await_count == 1


async def test_unavailable_peer_does_not_hide_reachable_players(room: Room) -> None:
    instance, master, caves, _, calls = room
    caves.handlers[c.ListPlayers] = AsyncMock(side_effect=RuntimeError("unavailable"))
    await instance.stop(notice=maintenance(Template.SHUTDOWN, delay=0))
    assert "announce:Master" in calls
    assert master.pid is None
    assert caves.pid is None


async def test_cancelled_announcement_propagates_without_disrupting_games(
    room: Room,
) -> None:
    instance, master, caves, prepare, calls = room
    master.handlers[c.Announce] = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await instance.restart(notice=maintenance(Template.RESTART, delay=0))
    assert master.ready
    assert caves.ready
    assert not any(call.startswith("stop:") for call in calls)
    assert prepare.await_count == 1
    assert not (await instance.status()).busy


async def test_failed_announcement_does_not_block_maintenance(room: Room) -> None:
    instance, master, caves, prepare, _ = room
    master.handlers[c.Announce] = AsyncMock(side_effect=ConnectionError("unavailable"))
    await instance.restart(notice=maintenance(Template.RESTART, delay=0))
    assert master.ready
    assert caves.ready
    assert prepare.await_count == 2


@pytest.mark.parametrize(
    "notice",
    [
        Repeat(message="hello").model_copy(update={"count": True}),
        Countdown(template="{remaining}").model_copy(update={"template": "{unknown}"}),
    ],
)
async def test_sdk_revalidates_notice_models_before_sending(
    room: Room, notice: Repeat | Countdown
) -> None:
    instance, master, _, _, _ = room
    with pytest.raises(ValidationError):
        await instance.announce(notice)
    assert not any(isinstance(request, c.Announce) for request in master.requests)
