import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from ulid import ULID

import dst_server.mods.maintenance as maintenance_module
from dst_server import commands as c
from dst_server.concurrency import cancel_tasks
from dst_server.events.world import ModOutdatedData, ModOutdatedEvent
from dst_server.models.cluster import GameEventRecord, ShardPhase
from dst_server.mods import ModUpdateError
from dst_server.timeouts import operation_deadline
from tests.cluster.helpers import EndpointStub, Room, managed_controller
from tests.helpers import wait_for_event


@pytest.fixture
async def room(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Room]:
    async with managed_controller(tmp_path, monkeypatch) as result:
        instance = result[0]
        if instance._mod_task is not None:
            await cancel_tasks(instance._mod_task)
            instance._mod_task = None
        instance._mod_maintenance.retry_at = 0
        yield result


def report(endpoint: EndpointStub, *, attempt: ULID | None = None) -> None:
    attempt = attempt or endpoint.attempt
    endpoint.game_events.publish(
        GameEventRecord(
            shard=endpoint.name,
            game_attempt=attempt,
            sequence=1,
            observed_timestamp_ns=0,
            event=ModOutdatedEvent(
                v=2,
                nonce=str(attempt),
                generation=1,
                session_id="same-world-across-restarts",
                seq=1,
                event="dst.mod.outdated",
                tick=1,
                monotonic_ms=1,
                cycle=1,
                data=ModOutdatedData(name="Insight"),
            ),
        )
    )


@pytest.mark.parametrize("both_shards", [False, True])
async def test_current_shard_reports_merge_into_one_saved_room_update(
    room: Room, both_shards: bool
) -> None:
    instance, master, caves, prepare, calls = room
    caves.outdated_mods = ("Insight", "Other Mod")
    if both_shards:
        master.outdated_mods = ("Insight",)
    old_attempts = master.attempt, caves.attempt
    calls.clear()
    await asyncio.gather(*(instance._maintain_mods() for _ in range(8)))

    assert prepare.await_count == 2
    assert calls.count("start:Master") == calls.count("start:Caves") == 1
    assert calls.index("save:Master") < calls.index("stop:Master")
    assert calls.index("wait-saved:Caves") < calls.index("stop:Master")
    assert (master.attempt, caves.attempt) != old_attempts
    assert not (await instance.status()).mod_update.pending


async def test_new_instance_outdated_during_startup_survives_and_backs_off(
    room: Room,
) -> None:
    instance, master, caves, prepare, _ = room
    master.outdated_mods = ("Insight",)

    async def start(command: c.Start) -> None:
        await caves.dispatch(command)
        caves.outdated_mods = ("Insight",)
        report(caves)

    caves.handlers[c.Start] = start
    await instance._maintain_mods()
    await instance._maintain_mods()
    status = (await instance.status()).mod_update
    assert status.pending
    assert status.retry_in_seconds > 0
    assert prepare.await_count == 2

    instance._mod_maintenance.retry_at = 0
    await instance._maintain_mods()
    await instance._maintain_mods()
    status = (await instance.status()).mod_update
    assert prepare.await_count == 3
    assert status.pending
    assert 290 < status.retry_in_seconds <= 300


@pytest.mark.parametrize("stage", ["save", "stop", "download"])
async def test_failed_maintenance_retains_demand_and_recovers_after_backoff(
    room: Room, stage: str
) -> None:
    instance, master, caves, prepare, calls = room
    master.outdated_mods = ("Insight",)
    fail = AsyncMock(side_effect=RuntimeError("maintenance failure"))
    match stage:
        case "save":
            master.handlers[c.Save] = fail
        case "stop":
            caves.handlers[c.Stop] = fail
        case "download":
            prepare.side_effect = ModUpdateError("download failed")
    calls.clear()
    try:
        await instance._maintain_mods()
        status = (await instance.status()).mod_update
        assert status.pending
        assert status.error
        assert not status.updating
        assert status.retry_in_seconds > 0
        if stage == "save":
            assert not any(call.startswith("stop:") for call in calls)
            assert master.ready
            assert caves.ready
        else:
            assert master.pid is None
            assert caves.pid is None
        previous = calls.copy()
        await instance._maintain_mods()
        assert calls == previous
    finally:
        master.handlers.clear()
        caves.handlers.clear()
        prepare.side_effect = None

    instance._mod_maintenance.retry_at = 0
    await instance._maintain_mods()
    assert master.ready
    assert caves.ready
    assert not (await instance.status()).mod_update.pending


async def test_mutation_rejects_busy_room_and_automatic_check_skips(room: Room) -> None:
    instance, master, _, prepare, _ = room
    master.outdated_mods = ("Insight",)
    async with instance._serialized():
        pending = asyncio.create_task(instance.restart())
        with pytest.raises(RuntimeError, match="busy"):
            await pending
        await instance._maintain_mods()
        assert (await instance.status()).busy
    assert prepare.await_count == 1


@pytest.mark.parametrize("trigger", ["event", "status"])
async def test_background_watcher_uses_retained_state_and_own_deadline(
    room: Room, monkeypatch: pytest.MonkeyPatch, trigger: str
) -> None:
    instance, _, caves, prepare, _ = room
    entered = asyncio.Event()
    old_prepare = prepare.side_effect

    def update(*_: object, **__: object) -> object:
        entered.set()
        return prepare.return_value

    prepare.side_effect = update
    if trigger == "status":
        monkeypatch.setattr(maintenance_module, "STATUS_INTERVAL", 0.01)
    token = operation_deadline.set(asyncio.get_running_loop().time() - 1)
    try:
        instance._mod_task = asyncio.create_task(instance._watch_mods())
    finally:
        operation_deadline.reset(token)
    caves.outdated_mods = ("Insight",)
    if trigger == "event":
        report(caves)
    try:
        await wait_for_event(entered, instance._mod_task)
        async with instance._serialized():
            assert prepare.await_count == 2
            assert caves.ready
    finally:
        prepare.side_effect = old_prepare


async def test_late_event_payload_cannot_restart_new_instances(room: Room) -> None:
    instance, _, caves, prepare, _ = room
    old_attempt = caves.attempt
    await instance.restart()
    report(caves, attempt=old_attempt)
    async with asyncio.timeout(1):
        await instance._mod_maintenance.wait()
    await instance._maintain_mods()
    assert prepare.await_count == 2
    assert not (await instance.status()).mod_update.pending


async def test_close_cancels_automatic_update_and_cannot_start_games(
    room: Room,
) -> None:
    instance, master, caves, prepare, calls = room
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def update(*_: object, **__: object) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    prepare.side_effect = update
    master.outdated_mods = ("Insight",)
    calls.clear()
    instance._mod_task = asyncio.create_task(instance._watch_mods())
    instance._mod_maintenance.wake()
    await wait_for_event(entered, instance._mod_task)
    await instance.aclose()
    assert cancelled.is_set()
    assert instance._mod_task.cancelled()
    assert master.pid is None
    assert caves.pid is None
    assert not any(call.startswith("start:") for call in calls)


async def test_auto_switch_disables_startup_and_runtime_but_not_manual_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DST_SERVER_MOD_AUTO_UPDATE", "false")
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, prepare, _ = room
        assert prepare.await_args is not None
        assert prepare.await_args.kwargs["update_mods"] is False
        assert instance._mod_task is None
        master.outdated_mods = ("Insight",)
        await instance._maintain_mods()
        assert prepare.await_count == 1
        await instance.update_mods(restart=True)
        assert prepare.await_count == 2
        assert prepare.await_args.kwargs["update_mods"] is True
        assert master.ready
        assert caves.ready
        assert not (await instance.status()).mod_update.enabled
        await instance.stop()
        await instance.update_mods(restart=True)
        assert prepare.await_count == 3
        assert master.phase is ShardPhase.RUNNING
        assert caves.phase is ShardPhase.RUNNING


async def test_stop_cancels_download_and_prevents_automatic_reopening(
    room: Room,
) -> None:
    instance, master, caves, prepare, calls = room
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def download(*_: object, **__: object) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    prepare.side_effect = download
    master.outdated_mods = ("Insight",)
    automatic = asyncio.create_task(instance._maintain_mods())
    await wait_for_event(entered, automatic)
    await instance.stop(notice=None)
    assert cancelled.is_set()
    assert isinstance(
        (await asyncio.gather(automatic, return_exceptions=True))[0],
        asyncio.CancelledError,
    )
    assert master.pid is None
    assert caves.pid is None
    calls.clear()
    instance._mod_maintenance.retry_at = 0
    await instance._maintain_mods()
    assert calls == []


@pytest.mark.parametrize("manual", [False, True])
async def test_every_successful_download_starts_a_five_minute_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manual: bool,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, prepare, _ = room
        if manual:
            await instance.update_mods(restart=True, notice=None)
        previous_updates = prepare.await_count
        master.outdated_mods = ("Insight",)
        await instance._maintain_mods()
        status = (await instance.status()).mod_update
        assert status.pending
        assert 290 < status.retry_in_seconds <= 300
        assert prepare.await_count == previous_updates
