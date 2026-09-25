import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from weakref import ref

import pytest
from ulid import ULID

import dst_server.cluster.controller as controller_module
from dst_server import commands as c
from dst_server.cluster import service
from dst_server.cluster.controller import (
    AgentEndpoint,
    ClusterController,
)
from dst_server.cluster.subscriptions import Broadcast, Subscription
from dst_server.configuration.files import Shard
from dst_server.configuration.store import (
    ConfigurationStore,
)
from dst_server.errors import (
    ControllerOperationError,
    DisconnectedError,
    ErrorCode,
    ErrorInfo,
    IndeterminateError,
    PlayerLocationConflictError,
    RemoteError,
)
from dst_server.game.rpc import LuaRequestError
from dst_server.models.cluster import (
    LogRecord,
    ShardPhase,
    ShardRuntimeStatus,
)
from dst_server.models.snapshot import (
    Snapshot,
    SnapshotCatalog,
    SnapshotClock,
    WorldSnapshotMetadata,
)
from dst_server.mods import ModUpdateError
from tests.cluster.helpers import (
    EndpointStub,
    configuration,
    controller,
    layout,
    managed_controller,
    player,
)
from tests.helpers import wait_for_event


@pytest.fixture
async def empty_controller(tmp_path: Path) -> AsyncIterator[ClusterController]:
    root = tmp_path / "cluster"
    configuration().save(root)
    instance = ClusterController(ConfigurationStore(root))
    yield instance
    await instance.aclose()


@pytest.mark.parametrize(
    ("mismatch", "message"),
    [
        (None, None),
        ("invalid-ulid", "must be a ULID"),
        ("noncanonical-ulid", "must be a ULID"),
        ("endpoint-master", "does not match"),
        ("status-name", "does not match"),
        ("status-master", "does not match"),
        ("status-incarnation", "does not match"),
    ],
)
async def test_registration_validates_canonical_identity(
    empty_controller: ClusterController,
    mismatch: str | None,
    message: str | None,
) -> None:
    endpoint = EndpointStub("Caves", False, [])
    if mismatch == "invalid-ulid":
        endpoint.incarnation = "invalid"
    elif mismatch == "noncanonical-ulid":
        endpoint.incarnation = endpoint.incarnation.lower()
    elif mismatch == "endpoint-master":
        endpoint.master = True
    elif mismatch is not None:
        status = await endpoint.runtime_status()
        changes = {
            "status-name": {"name": "Other"},
            "status-master": {"is_master": True},
            "status-incarnation": {"agent_incarnation": ULID()},
        }[mismatch]
        endpoint.runtime_status = AsyncMock(return_value=status.replace(**changes))

    if message is None:
        await empty_controller.register(endpoint)
        assert empty_controller.agent("Caves") is endpoint
    else:
        with pytest.raises((TypeError, ValueError), match=message):
            await empty_controller.register(endpoint)
        with pytest.raises(DisconnectedError):
            empty_controller.agent("Caves")


async def test_complete_roster_starts_without_an_external_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, prepare, calls = await controller(tmp_path, monkeypatch)
    try:
        prepare.assert_awaited_once_with(
            tmp_path / "install",
            tmp_path / "cluster",
            update_mods=True,
        )
        assert calls[:4] == [
            "activate:Master",
            "activate:Caves",
            "start:Master",
            "start:Caves",
        ]
        assert (await instance.status()).phase == "running"

        await instance.start()
        assert prepare.await_count == 1
        assert calls.count("activate:Master") == 2
        assert calls.count("activate:Caves") == 2
        assert calls[-2:] == ["start:Master", "start:Caves"]
    finally:
        await instance.aclose()
    assert master.phase == caves.phase == "stopped"
    kills = calls.count("kill:Master") + calls.count("kill:Caves")
    assert not await instance.unregister(caves)
    assert not await instance.failed(caves)
    with pytest.raises(DisconnectedError, match="closed"):
        await instance.register(caves)
    assert calls.count("kill:Master") + calls.count("kill:Caves") == kills


@pytest.mark.parametrize("operation", ["start", "restart"])
async def test_cluster_start_and_restart_reuse_prepared_mods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, prepare, _ = room

        def update(*_: object, **__: object) -> tuple[Shard, ...]:
            assert master.phase == caves.phase == "stopped"
            return layout(tmp_path / "cluster")

        prepare.side_effect = update
        if operation == "start":
            await instance.stop()
            assert (await instance.status()).prepared is True
            await instance.start()
        else:
            await instance.restart()
        assert prepare.await_count == 1
        assert (await instance.status()).phase == "running"
        await instance.start()
        assert prepare.await_count == 1


async def test_manual_mod_update_is_reused_by_start_and_shard_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, _, prepare, calls = room
        await instance.stop()
        await instance.update_mods()
        await instance.start()
        await instance.shard("Caves").restart()
        assert calls[-1] == "restart:Caves"
        assert prepare.await_count == 2
        assert (await instance.status()).phase == "running"


async def test_failed_mod_update_is_retried_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, _, prepare, _ = room
        await instance.stop()
        await instance.update_mods()
        prepare.side_effect = ModUpdateError("mod update failed")
        with pytest.raises(RuntimeError, match="mod update failed"):
            await instance.update_mods()
        assert (await instance.status()).prepared is False
        with pytest.raises(ModUpdateError):
            await instance.start()
        assert prepare.await_count == 4


@pytest.mark.parametrize("stage", ["stop", "update"])
async def test_restart_continues_only_after_games_exit_and_mods_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, prepare, calls = room
        calls.clear()
        if stage == "stop":
            caves.handlers[c.Stop] = AsyncMock(side_effect=RuntimeError("stop failed"))
            await instance.restart(notice=None)
            assert master.phase == caves.phase == "running"
            assert "kill:Caves" in calls
            assert calls.index("kill:Caves") < calls.index("start:Caves")
        else:
            prepare.side_effect = ModUpdateError("mod update failed")
            with pytest.raises(ModUpdateError):
                await instance.update_mods(restart=True, notice=None)
            assert master.phase == caves.phase == "stopped"
            assert not any(call.startswith("start:") for call in calls)


async def test_registration_requires_stopped_game_processes(
    empty_controller: ClusterController,
) -> None:
    endpoint = EndpointStub("Caves", False, [])
    endpoint.phase, endpoint.ready, endpoint.pid = ShardPhase.RUNNING, True, 123
    with pytest.raises(RuntimeError, match="must be stopped"):
        await empty_controller.register(endpoint)


async def test_missing_initial_agent_has_a_bounded_registration_window(
    empty_controller: ClusterController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(controller_module, "DEFAULT_CONNECT_TIMEOUT", 0.01)
    with pytest.raises(TimeoutError):
        await empty_controller.wait_fatal()


async def test_initial_mod_download_failure_keeps_service_alive_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(controller_module, "HEALTH_INTERVAL", 0.005)
    monkeypatch.setattr(controller_module, "HEALTH_FAILURE_TIMEOUT", 0.02)
    root = tmp_path / "cluster"
    configuration().save(root)
    prepare = AsyncMock(side_effect=ModUpdateError("download unavailable"))
    monkeypatch.setattr(service, "prepare_shared", prepare)
    instance = ClusterController(ConfigurationStore(root))
    master, caves = EndpointStub("Master", True, []), EndpointStub("Caves", False, [])
    try:
        await instance.register(master)
        await instance.register(caves)
        await instance.wait_idle()
        status = await instance.status()
        assert not instance._fatal.is_set()
        assert status.mod_update.pending
        assert 290 < status.mod_update.retry_in_seconds <= 300
        assert master.pid is None
        assert caves.pid is None
        await asyncio.sleep(0.06)
        assert not instance._fatal.is_set()
        prepare.side_effect = None
        prepare.return_value = layout(root)
        instance._mod_maintenance.retry_at = 0
        await instance._maintain_mods()
        assert (await instance.status()).phase == "running"
    finally:
        await instance.aclose()


@pytest.mark.parametrize("command", [c.Start(), c.Restart(notice=None)])
@pytest.mark.parametrize("initial", [False, True])
async def test_shard_start_requires_successful_shared_preparation(
    empty_controller: ClusterController,
    monkeypatch: pytest.MonkeyPatch,
    command: c.Request[None],
    initial: bool,
) -> None:
    instance = empty_controller
    prepare = AsyncMock(return_value=layout(instance.cluster_path))
    if initial:
        prepare.side_effect = ModUpdateError("download unavailable")
    monkeypatch.setattr(service, "prepare_shared", prepare)
    calls: list[str] = []
    master = EndpointStub("Master", True, calls)
    caves = EndpointStub("Caves", False, calls)
    master.peers = caves.peers = (master, caves)
    await instance.register(master)
    await instance.register(caves)
    await instance.wait_idle()
    if not initial:
        await instance.stop(notice=None)
        prepare.side_effect = ModUpdateError("download unavailable")
        with pytest.raises(ModUpdateError):
            await instance.update_mods()
    calls.clear()
    prepared_attempts = prepare.await_count

    with pytest.raises(RuntimeError, match="room resources are not prepared"):
        await instance.shard("Caves").invoke(command)

    assert calls == []
    assert prepare.await_count == prepared_attempts
    assert not instance._prepared
    assert not instance._loading
    assert master.phase == caves.phase == ShardPhase.STOPPED
    prepare.side_effect = None
    await instance.start()
    assert prepare.await_count == prepared_attempts + 1
    assert (await instance.status()).phase == "running"


async def test_activation_failure_never_starts_a_game_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cluster"
    configuration().save(root)
    shards = layout(root)
    prepare = AsyncMock(return_value=shards)
    monkeypatch.setattr(service, "prepare_shared", prepare)
    calls: list[str] = []
    instance = ClusterController(ConfigurationStore(root))
    master = EndpointStub("Master", True, calls)
    caves = EndpointStub("Caves", False, calls)
    caves.handlers[c.Activate] = AsyncMock(
        side_effect=RuntimeError("activation secret")
    )
    await instance.register(master)
    await instance.register(caves)
    await instance.wait_idle()
    try:
        assert not any(call.startswith("start:") for call in calls)
        status = await instance.status()
        assert status.phase == "failed"
        assert status.error == "cluster initialization failed"
        assert "secret" not in status.error

        with pytest.raises(ControllerOperationError):
            await instance.wait_fatal()
        caves.handlers.pop(c.Activate, None)
    finally:
        await instance.aclose()


async def test_registered_disconnect_stops_peers_and_requires_service_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, calls = room
        assert await instance.unregister(caves)
        assert master.phase == "stopped"
        with pytest.raises(ControllerOperationError):
            await instance.wait_fatal()
        with pytest.raises(RuntimeError, match="cannot register"):
            await instance.register(EndpointStub("Caves", False, calls))
        with pytest.raises(RuntimeError, match="restart its service"):
            await instance.start()


async def test_fail_close_kills_only_peers_that_cannot_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, calls = await controller(tmp_path, monkeypatch)
    master.handlers[c.Stop] = AsyncMock(side_effect=RuntimeError("stop failed"))
    try:
        assert await instance.unregister(caves)
        assert calls[-2:] == ["stop:Master", "kill:Master"]
        assert "kill:Caves" not in calls
        status = await instance.status()
        assert status.phase == "degraded"
        assert status.shards[0].desired == "running"
    finally:
        master.handlers.pop(c.Stop, None)
        await instance.aclose()


async def test_fail_close_kill_failure_is_reported_and_shutdown_retries_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    fail_kill = AsyncMock(side_effect=RuntimeError())

    master.handlers[c.Stop] = AsyncMock(side_effect=RuntimeError("stop failed"))
    master.handlers[c.Kill] = fail_kill
    try:
        caves.phase = ShardPhase.FAILED
        with pytest.RaisesGroup(RuntimeError, RuntimeError):
            await instance.failed(caves)
        assert master.phase == "running"

        master.handlers.pop(c.Stop, None)
        master.handlers.pop(c.Kill, None)
        await instance.aclose()
        assert master.phase == "stopped"
    finally:
        master.handlers.pop(c.Stop, None)
        master.handlers.pop(c.Kill, None)
        await instance.aclose()


async def test_late_failure_cannot_fail_close_a_restarted_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, caves, _, calls = room
        caves.phase = ShardPhase.FAILED
        await instance.restart()
        stops = calls.count("stop:Master") + calls.count("stop:Caves")

        assert await instance.failed(caves)
        assert calls.count("stop:Master") + calls.count("stop:Caves") == stops


@pytest.mark.parametrize("action", ["stop", "restart", "close"])
async def test_failure_observation_cannot_override_a_completed_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, caves, _, calls = room
        caves.phase = ShardPhase.FAILED
        stale = await caves.runtime_status()
        original = caves.runtime_status
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_status() -> ShardRuntimeStatus:
            if not entered.is_set():
                entered.set()
                await release.wait()
                return stale
            return await original()

        monkeypatch.setattr(caves, "runtime_status", delayed_status)
        failure = asyncio.create_task(instance.failed(caves))
        try:
            await wait_for_event(entered, failure)
            if action == "close":
                await instance.aclose()
            else:
                await getattr(instance, action)(notice=None)
            completed = calls.copy()
            release.set()
            assert await failure is (action != "close")
            assert not instance._fatal.is_set()
            assert calls == completed
        finally:
            release.set()
            await asyncio.gather(failure, return_exceptions=True)


async def test_failure_report_does_not_cancel_an_in_progress_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, caves, _, _ = room
        caves.phase = ShardPhase.FAILED
        entered = caves.stop_entered = asyncio.Event()
        release = caves.stop_release = asyncio.Event()
        stopping = asyncio.create_task(instance.stop(notice=None))
        try:
            await wait_for_event(entered, stopping)
            assert await instance.failed(caves)
            assert not stopping.done()
            assert not instance._fatal.is_set()
        finally:
            release.set()
            await stopping
        assert (await instance.status()).phase == "stopped"


async def test_failure_report_fail_closes_when_status_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    caves.fail_status = True

    try:
        assert await instance.failed(caves)
        assert master.phase == caves.phase == "stopped"
    finally:
        caves.fail_status = False
        await instance.aclose()


async def test_close_kill_failure_does_not_seal_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    fail_kill = AsyncMock(side_effect=RuntimeError())

    master.handlers[c.Stop] = AsyncMock(side_effect=RuntimeError("stop failed"))
    master.handlers[c.Kill] = fail_kill
    with pytest.RaisesGroup(RuntimeError, RuntimeError):
        await instance.aclose()
    assert master.phase == "running"
    assert caves.phase == "stopped"

    master.handlers.pop(c.Stop, None)
    master.handlers.pop(c.Kill, None)
    await instance.aclose()
    assert master.phase == caves.phase == "stopped"


async def test_agent_status_deadline_releases_controller_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cluster"
    configuration().save(root)
    instance = ClusterController(ConfigurationStore(root))
    master = EndpointStub("Master", True, [])
    runtime_status = master.runtime_status

    async def hang() -> ShardRuntimeStatus:
        await asyncio.Event().wait()
        return await runtime_status()

    monkeypatch.setattr(controller_module, "AGENT_STATUS_TIMEOUT", 0.01)
    monkeypatch.setattr(master, "runtime_status", hang)
    with pytest.raises(TimeoutError):
        await instance.register(master)

    monkeypatch.setattr(master, "runtime_status", runtime_status)
    await instance.register(master)
    await instance.aclose()


async def test_close_cancels_active_update_without_waiting_for_its_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, _, _, _ = await controller(tmp_path, monkeypatch)
    await instance.stop()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def prepare(*_: object, **__: object) -> tuple[Shard, ...]:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        return layout(tmp_path / "cluster")

    monkeypatch.setattr(controller_module, "CONTROLLER_CANCEL_TIMEOUT", 0.01)
    monkeypatch.setattr(service, "prepare_shared", prepare)
    updating = asyncio.create_task(instance.update_mods())
    try:
        async with asyncio.timeout(5):
            await wait_for_event(entered, updating)

            master.stop_entered = asyncio.Event()
            closing = asyncio.create_task(instance.aclose())
            await wait_for_event(master.stop_entered, closing)
            assert not closing.done()
            assert not updating.done()

            release.set()
            await closing
            result = (await asyncio.gather(updating, return_exceptions=True))[0]
            assert isinstance(result, DisconnectedError)
            assert "closed" in str(result)
    finally:
        async with asyncio.timeout(5):
            release.set()
            updating.cancel()
            await asyncio.gather(updating, return_exceptions=True)
            await instance.aclose()


async def test_public_operations_reject_while_close_is_stopping_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, _, _, _ = await controller(tmp_path, monkeypatch)
    shard = instance.shard("Master")
    master.stop_entered = asyncio.Event()
    master.stop_release = asyncio.Event()
    closing = asyncio.create_task(instance.aclose())

    try:
        watchdog = asyncio.timeout(5)
        async with watchdog:
            await wait_for_event(master.stop_entered, closing)
            for operation in (
                instance.start,
                instance.restart,
                instance.status,
                instance.read_configuration,
                shard.status,
                shard.start,
                lambda: shard.execute("return true"),
            ):
                with pytest.raises(DisconnectedError, match="closed"):
                    await operation()
            with pytest.raises(DisconnectedError, match="closed"):
                instance.subscribe("logs")
        assert not watchdog.expired()
    finally:
        async with asyncio.timeout(5):
            master.stop_release.set()
            await closing
    await asyncio.wait_for(instance.aclose(), timeout=5)


async def test_registered_status_failure_is_reported_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, caves, _, _ = room
        caves.fail_status = True
        status = await instance.status()
        unavailable = next(item for item in status.shards if item.name == "Caves")
        assert status.phase == "degraded"
        assert unavailable.phase == "unavailable"
        assert unavailable.error == "shard agent is unavailable"
        assert "secret" not in unavailable.error
        assert (await instance.shard("Caves").status()).phase == "unavailable"


async def test_known_offline_shard_is_unavailable_not_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, caves, _, _ = room
        shard = instance.shard("Caves")
        assert await instance.unregister(caves)
        with pytest.raises(DisconnectedError, match="unavailable"):
            await shard.status()
        with pytest.raises(KeyError, match="unknown"):
            instance.shard("Unknown")


class ReadyBroadcast[T](Broadcast[T]):
    def __init__(self) -> None:
        super().__init__()
        self.subscribed = asyncio.Event()

    def subscribe(self) -> Subscription[T]:
        subscription = super().subscribe()
        self.subscribed.set()
        return subscription


async def test_internal_relay_continues_after_overflow(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cluster"
    configuration().save(root)
    calls: list[str] = []
    instance = ClusterController(ConfigurationStore(root))
    master = EndpointStub("Master", True, calls)
    master.logs = logs = ReadyBroadcast[LogRecord]()
    await instance.register(master)
    subscription = instance.subscribe("logs")
    await wait_for_event(logs.subscribed)
    attempt = ULID()
    for sequence in range(1025):
        master.logs.publish(
            LogRecord(
                shard="Master",
                game_attempt=attempt,
                sequence=sequence,
                observed_timestamp_ns=sequence,
                line="burst",
            )
        )
    async with asyncio.timeout(1):
        await instance._mod_maintenance.wait()
    master.logs.publish(
        LogRecord(
            shard="Master",
            game_attempt=attempt,
            sequence=1025,
            observed_timestamp_ns=1025,
            line="after-overflow",
        )
    )
    try:
        async with asyncio.timeout(1):
            record = (await subscription.next(1))[0]
            assert isinstance(record, LogRecord)
            assert record.line == "after-overflow"
        assert "stop:Master" not in calls
    finally:
        subscription.close()
        await instance.aclose()


async def test_internal_relay_releases_delivered_batch(tmp_path: Path) -> None:
    root = tmp_path / "cluster"
    configuration().save(root)
    instance = ClusterController(ConfigurationStore(root))
    source, target = ReadyBroadcast[LogRecord](), Broadcast[LogRecord]()
    subscription = target.subscribe()
    relay = instance._start_relay("Master", source, target.publish)
    await wait_for_event(source.subscribed, relay)
    references = []
    for sequence in range(3):
        record = LogRecord(
            shard="Master",
            game_attempt=ULID(),
            sequence=sequence,
            observed_timestamp_ns=sequence,
            line="x" * 1024 * 1024,
        )
        references.append(ref(record))
        source.publish(record)
    del record
    try:
        async with asyncio.timeout(1):
            batch = await subscription.next(3)
        assert len(batch) == 3
        del batch
        assert all(reference() is None for reference in references)
    finally:
        source.close()
        await relay
        subscription.close()
        await instance.aclose()


async def test_closed_internal_relay_does_not_stop_game_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, calls = room
        master.logs.close()
        async with asyncio.timeout(1):
            await instance._relays[master.name][0]
        status = await instance.status()
        assert status.phase == "running"
        assert status.error is None
        assert not any(call.startswith("stop:") for call in calls)
        assert master.phase == caves.phase == "running"


async def test_save_and_reload_coordinate_every_shard_from_master_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, secondary, _, _ = room
        assert await instance.save(timeout=9) is None
        assert master.requests.count(c.Save(timeout=9)) == 1
        assert c.Save(timeout=9) not in secondary.requests
        assert tuple(
            result.value
            for result in await instance.execute_all("return true", timeout=4)
        ) == ("Master:return true", "Caves:return true")
        caves = instance.shard("Caves")
        assert await caves.execute("return false", timeout=6) == "Caves:return false"
        assert not hasattr(caves, "save")
        assert c.Execute(source="return true", timeout=4) in master.requests
        assert c.Execute(source="return false", timeout=6) in secondary.requests

        with pytest.raises(ValueError, match="greater than 0"):
            await instance.execute_all("return true", timeout=0)
        with pytest.raises(ValueError, match="greater than 0"):
            await caves.execute("return true", timeout=0)

        await instance.reset(timeout=11)
        await instance.rollback(2, timeout=12)
        await instance.regenerate(
            expected_session_id="SESSION", require_empty=True, timeout=13
        )
        for command in (
            c.RollbackToSnapshot(session_id="Master", snapshot_id=91, timeout=11),
            c.RollbackToSnapshot(session_id="Master", snapshot_id=89, timeout=12),
            c.Regenerate(expected_session_id="SESSION", require_empty=True, timeout=13),
        ):
            assert command in master.requests
            assert command not in secondary.requests


@pytest.mark.parametrize(
    ("operation", "mutation"),
    [
        (c.ClusterSave(timeout=0.5), c.Save),
        (c.Reset(timeout=0.5), c.RollbackToSnapshot),
        (c.Rollback(timeout=0.5), c.RollbackToSnapshot),
        (c.RollbackToDay(day=8, timeout=0.5), c.RollbackToSnapshot),
        (c.Regenerate(timeout=0.5), c.Regenerate),
    ],
)
async def test_cluster_timeout_after_submission_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: c.Request[Any],
    mutation: type[c.Save | c.RollbackToSnapshot | c.Regenerate],
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        for agent in (master, caves):
            agent.runtime = agent.runtime.replace(snapshot=4)

        async def hang(_: c.Request[Any]) -> None:
            await asyncio.Event().wait()

        master.handlers[mutation] = hang
        with pytest.raises(IndeterminateError):
            await instance.invoke(operation)
        assert sum(isinstance(command, mutation) for command in master.requests) == 1
        assert instance._phase is None

        # Loading remains supervised even after an indeterminate reply.
        if mutation is not c.Save:
            assert instance._loading
            with pytest.raises(RuntimeError, match="busy"):
                await instance.save()
            await instance.stop(notice=None)
            assert not instance._loading


@pytest.mark.parametrize(
    "operation",
    [
        c.Restart(notice=None, timeout=0.5),
        c.UpdateMods(restart=True, notice=None, timeout=0.5),
    ],
)
async def test_restart_timeout_is_indeterminate_without_extra_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: c.Request[None],
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, calls = room
        calls.clear()

        async def start(command: c.Start) -> None:
            await caves.dispatch(command)
            master.connected_ids = ("Master",)

        caves.handlers[c.Start] = start
        with pytest.raises(IndeterminateError):
            await instance.invoke(operation)
        assert not any(isinstance(command, c.Save) for command in master.requests)
        assert calls.count("stop:Master") == calls.count("start:Master") == 1
        assert calls.count("stop:Caves") == calls.count("start:Caves") == 1


async def test_cluster_operation_defaults_allow_saving_and_reloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        await instance.execute_all("return true")
        await instance.save()
        await instance.reset()
        await instance.rollback()
        await instance.regenerate()
        assert c.Execute(source="return true", timeout=120) in master.requests
        assert c.Save(timeout=300) in master.requests
        assert (
            c.RollbackToSnapshot(session_id="Master", snapshot_id=91, timeout=900)
            in master.requests
        )
        assert (
            c.RollbackToSnapshot(session_id="Master", snapshot_id=90, timeout=900)
            in master.requests
        )
        assert c.Regenerate(timeout=900) in master.requests


@pytest.mark.parametrize(
    ("scenario", "error"),
    [
        ("success", None),
        ("missing_day", KeyError),
        ("missing_shard_copy", KeyError),
        ("session_changed", ValueError),
        ("restore_failed", IndeterminateError),
        ("wrong_day", None),
        ("wrong_session", None),
        ("wrong_snapshot_same_day", None),
    ],
)
async def test_rollback_to_day_selects_complete_snapshot_and_returns_on_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    error: type[Exception] | None,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        catalogs: dict[str, tuple[Snapshot, ...]] = {}
        for agent in (master, caves):
            catalogs[agent.name] = tuple(
                Snapshot(
                    snapshot_id=number,
                    world_file=f"session/{agent.name}/{number:010d}",
                    metadata=(
                        None
                        if number == 93
                        else WorldSnapshotMetadata(
                            clock=SnapshotClock(
                                cycles=7 if number in {90, 91, 92} else 8
                            )
                        )
                    ),
                )
                for number in range(220, 0, -1)
                if not (
                    agent is caves
                    and (
                        number == 92
                        or (scenario == "missing_shard_copy" and number in {90, 91})
                    )
                )
            )

            def snapshots(
                command: c.Snapshots,
                endpoint: EndpointStub = agent,
            ) -> SnapshotCatalog:
                available = tuple(
                    item
                    for item in catalogs[endpoint.name]
                    if command.before is None or item.snapshot_id < command.before
                )
                return SnapshotCatalog(
                    session_id=(
                        "changed"
                        if scenario == "session_changed" and command.before is not None
                        else endpoint.name
                    ),
                    snapshots=available[: command.limit],
                    has_more=len(available) > command.limit,
                )

            agent.handlers[c.Snapshots] = AsyncMock(side_effect=snapshots)

        def restore(command: c.RollbackToSnapshot) -> None:
            assert instance._lock.locked()
            assert command == c.RollbackToSnapshot(
                session_id="Master", snapshot_id=90, timeout=12
            )
            if scenario == "restore_failed":
                raise IndeterminateError
            master.generation += 1
            caves.generation += 1
            if scenario == "wrong_day":
                caves.world = caves.world.replace(day=9)
            if scenario == "wrong_session":
                caves.runtime = caves.runtime.replace(session_id="changed")
            if scenario == "wrong_snapshot_same_day":
                caves.runtime = caves.runtime.replace(snapshot=92)

        operation = AsyncMock(side_effect=restore)
        master.handlers[c.RollbackToSnapshot] = operation
        assert len((await instance.list_snapshots(limit=2)).snapshots) == 2
        assert (
            await instance.shard("Caves").list_snapshots(limit=1, before=93)
        ).snapshots[0].snapshot_id < 92
        for invalid in (0, -1, True):
            with pytest.raises(ValueError, match="day"):
                await instance.rollback_to_day(invalid)
        if error is None:
            selected = await instance.rollback_to_day(8, timeout=12)
            assert selected.snapshot_id == 90
        else:
            with pytest.raises(error):
                await instance.rollback_to_day(
                    100 if scenario == "missing_day" else 8, timeout=12
                )
        assert operation.await_count == (
            0
            if scenario in {"missing_day", "missing_shard_copy", "session_changed"}
            else 1
        )


async def test_save_and_reload_disconnects_are_stage_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        master.handlers[c.Runtime] = AsyncMock(side_effect=DisconnectedError())
        with pytest.RaisesGroup(DisconnectedError):
            await instance.reset()
        master.handlers.pop(c.Runtime)
        master.handlers[c.RollbackToSnapshot] = AsyncMock(
            side_effect=DisconnectedError()
        )
        with pytest.raises(IndeterminateError):
            await instance.reset()

        await instance.stop(notice=None)
        await instance.start()
        master.handlers[c.Save] = AsyncMock(side_effect=DisconnectedError())
        with pytest.raises(IndeterminateError):
            await instance.save()


async def test_partial_results_players_and_whitelist_are_cluster_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, calls = room
        master.players = (player("KU_one", active=False),)
        caves.players = (player("KU_one", active=True),)
        caves.handlers[c.Pause] = AsyncMock(side_effect=RuntimeError("pause secret"))
        paused = await instance.pause(True)
        assert paused[0].value is True
        assert paused[0].error is None
        assert paused[1].value is None
        assert paused[1].error is not None
        assert paused[1].error.code is ErrorCode.INVALID_STATE
        assert "secret" not in paused[1].error.message

        players = await instance.list_players()
        assert len(players) == 1
        assert players[0].shard == "Caves"
        assert players[0].player.state is not None
        assert (await instance.get_player("KU_one")) == players[0]

        assert await instance.is_whitelisted("KU_one")
        assert await instance.whitelist("KU_one")
        assert not await instance.unwhitelist("KU_one")
        assert "is-whitelisted:Caves" not in calls

        master.players = (player("KU_one", active=True),)
        with pytest.raises(PlayerLocationConflictError):
            await instance.list_players()


async def test_indeterminate_shard_outcomes_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    failure = IndeterminateError()
    caves.handlers[c.Pause] = AsyncMock(side_effect=failure)
    try:
        paused = await instance.pause(True)
        assert paused[1].error == failure.error

        caves.handlers[c.Pause] = AsyncMock(side_effect=TimeoutError())
        timed_out = (await instance.pause(True))[1].error
        assert timed_out is not None
        assert timed_out.code is ErrorCode.TIMEOUT

        def fail_start(_: c.Start) -> None:
            master.handlers[c.Stop] = AsyncMock(side_effect=RuntimeError("stop failed"))
            raise failure

        fail_kill = AsyncMock(side_effect=RuntimeError())
        master.handlers[c.Kill] = fail_kill
        caves.handlers[c.Start] = AsyncMock(side_effect=fail_start)
        with pytest.raises(IndeterminateError) as caught:
            await instance.restart()
        assert caught.value is failure
        fail_kill.assert_awaited_once()

        master.handlers.pop(c.Stop, None)
        master.handlers.pop(c.Kill, None)
        with pytest.raises(RuntimeError, match="restart its service"):
            await instance.restart()
    finally:
        master.handlers.pop(c.Stop, None)
        master.handlers.pop(c.Kill, None)
        await instance.aclose()


async def test_shard_restart_forwards_the_callers_lifecycle_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, caves, _, _ = room
        agent_call = AsyncMock(wraps=instance._agent_call)
        monkeypatch.setattr(instance, "_agent_call", agent_call)
        async with asyncio.timeout(5):
            await instance.shard("Caves").invoke(c.Restart(timeout=17))
        assert c.Restart(timeout=17, notice=None) in caves.requests
        assert any(
            call.kwargs["limit"] == 17 + controller_module.RPC_TIMEOUT_MARGIN
            for call in agent_call.await_args_list
        )
        assert caves.phase is ShardPhase.RUNNING


@pytest.mark.parametrize("phase", [ShardPhase.RUNNING, ShardPhase.FAILED])
async def test_shard_with_live_pid_cannot_update_shared_mods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: ShardPhase,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        master.phase, master.ready, master.pid = phase, False, 123
        caves.phase, caves.ready, caves.pid = ShardPhase.STOPPED, False, None
        with pytest.raises(RuntimeError, match="must be stopped"):
            await instance.update_mods()


async def test_dynamic_world_configuration_does_not_block_start_or_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cluster"
    configuration().save(root)
    path = root / "Master" / "worldgenoverride.lua"
    source = "return (function() return { override_enabled = true } end)()"
    path.write_text(source)
    shards = layout(root)
    prepare = AsyncMock(return_value=shards)
    monkeypatch.setattr(service, "prepare_shared", prepare)
    calls: list[str] = []
    instance = ClusterController(ConfigurationStore(root))
    master = EndpointStub("Master", True, calls)
    caves = EndpointStub("Caves", False, calls)
    await instance.register(master)
    await instance.register(caves)
    await instance.wait_idle()
    try:
        with pytest.raises(ValueError, match="literal return table"):
            await instance.read_configuration()
        status = await instance.status()
        assert status.phase == "running"
        assert status.error is None
        assert status.prepared is True
        prepare.assert_awaited_once()
        await instance.start()
        assert master.phase == caves.phase == "running"
        assert path.read_text() == source
    finally:
        await instance.aclose()


async def test_gather_failure_waits_for_peer_cancellation_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def operation(agent: AgentEndpoint) -> None:
        if agent is master:
            await entered.wait()
            message = "peer failed"
            raise RuntimeError(message)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    gathering = asyncio.create_task(instance._gather((master, caves), operation))
    try:
        await wait_for_event(cleaning, gathering)
        assert not gathering.done()
        release.set()
        with pytest.RaisesGroup(pytest.RaisesExc(RuntimeError, match="peer failed")):
            await asyncio.wait_for(asyncio.shield(gathering), timeout=5)
    finally:
        async with asyncio.timeout(5):
            release.set()
            await asyncio.gather(gathering, return_exceptions=True)
            await instance.aclose()


async def test_concurrent_close_survives_repeated_caller_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, master, caves, _, calls = await controller(tmp_path, monkeypatch)
    calls.clear()
    master.stop_entered = asyncio.Event()
    master.stop_release = asyncio.Event()
    closing = asyncio.create_task(instance.aclose())
    other = asyncio.create_task(instance.aclose())
    try:
        await wait_for_event(master.stop_entered, closing)
        closing.cancel("first cancellation")
        await asyncio.sleep(0)
        closing.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not closing.done()
        assert not other.done()
        master.stop_release.set()
        assert closing in (await asyncio.wait((closing,), timeout=5))[0]
        with pytest.raises(asyncio.CancelledError) as caught:
            closing.result()
        assert caught.value.args == ("first cancellation",)
        await asyncio.wait_for(asyncio.shield(other), timeout=5)
        assert master.phase == caves.phase == ShardPhase.STOPPED
        assert calls == ["drain:Master", "drain:Caves"]
    finally:
        async with asyncio.timeout(5):
            master.stop_release.set()
            await asyncio.gather(closing, other, return_exceptions=True)
            await instance.aclose()


async def test_cancelled_failed_close_can_retry_cleanup_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fail_stop(_: c.Stop) -> None:
        entered.set()
        await release.wait()
        message = "stop failed"
        raise RuntimeError(message)

    master.handlers[c.Stop] = fail_stop
    master.handlers[c.Kill] = AsyncMock(side_effect=RuntimeError("kill failed"))
    closing = asyncio.create_task(instance.aclose())
    try:
        watchdog = asyncio.timeout(5)
        async with watchdog:
            await wait_for_event(entered, closing)
            closing.cancel("caller cancelled")
            await asyncio.sleep(0)
            release.set()
            assert closing in (await asyncio.wait((closing,), timeout=5))[0]
            with pytest.raises(asyncio.CancelledError) as caught:
                closing.result()
            assert caught.value.args == ("caller cancelled",)
            assert isinstance(caught.value.__cause__, ExceptionGroup)
            master.handlers.clear()
            await instance.aclose()
            assert master.phase == caves.phase == ShardPhase.STOPPED
        assert not watchdog.expired()
    finally:
        async with asyncio.timeout(5):
            release.set()
            master.handlers.clear()
            await asyncio.gather(closing, return_exceptions=True)
            await instance.aclose()


@pytest.mark.parametrize("action", ["start", "save", "regenerate"])
async def test_cluster_operations_wait_for_actual_shard_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        checked = asyncio.Event()
        master.connected_ids = ("Master", "unrelated-shard")
        master.requests.clear()

        async def connected(command: c.ConnectedShards) -> Any:
            checked.set()
            return await master.dispatch(command)

        master.handlers[c.ConnectedShards] = connected
        command = {"start": c.Start, "save": c.ClusterSave, "regenerate": c.Regenerate}[
            action
        ](timeout=2)
        task = asyncio.create_task(instance.invoke(command))
        try:
            await wait_for_event(checked, task)
            assert not task.done()
            assert (await instance.status()).phase != "running"
            assert not any(
                isinstance(command, c.Save | c.Regenerate)
                for command in master.requests
            )
            master.connected_ids = ("Master", "Caves")
            await asyncio.wait_for(task, 2)
            assert (await instance.status()).phase == "running"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "outcome",
    ["new-worlds", "old-cave", "old-generation", "new-process", "disconnected"],
)
async def test_reload_acceptance_is_separate_from_loading_supervision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room

        async def regenerate(command: c.Regenerate) -> None:
            previous = caves.runtime
            await master.dispatch(command)
            if outcome == "old-cave":
                caves.runtime = previous
            elif outcome == "old-generation":
                caves.generation -= 1
            elif outcome == "new-process":
                caves.attempt = ULID()
            elif outcome == "disconnected":
                master.connected_ids = ("Master",)

        mutation = master.handlers[c.Regenerate] = AsyncMock(side_effect=regenerate)
        await instance.regenerate(timeout=1)
        status = await instance.status()
        assert status.busy is (
            outcome in {"old-generation", "new-process", "disconnected"}
        )
        if status.busy:
            with pytest.raises(RuntimeError, match="busy"):
                await instance.regenerate()
            instance._load_deadline = 0
            with pytest.raises(TimeoutError, match="loading"):
                await instance._runtime_ready()
        mutation.assert_awaited_once()


@pytest.mark.parametrize("action", ["stop", "kill"])
@pytest.mark.parametrize("name", ["Master", "Caves"])
async def test_stopping_one_shard_preserves_other_shards_loading_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, name: str
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room

        async def regenerate(command: c.Regenerate) -> None:
            await master.dispatch(command)
            master.ready = caves.ready = False

        master.handlers[c.Regenerate] = regenerate
        await instance.regenerate()
        deadline = instance._load_deadline
        command = c.Stop(notice=None) if action == "stop" else c.Kill()
        await instance.shard(name).invoke(command)
        remaining = caves if name == "Master" else master
        assert tuple(instance._loading) == (remaining.name,)
        assert instance._load_deadline == deadline
        assert await instance._runtime_ready()
        assert not instance._fatal.is_set()
        with pytest.raises(RuntimeError, match="busy"):
            await instance.save()
        remaining.ready = True
        assert await instance._runtime_ready()
        assert not instance._loading


@pytest.mark.parametrize("hang", [False, True])
async def test_loading_survives_failed_connection_query_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hang: bool
) -> None:
    monkeypatch.setattr(controller_module, "AGENT_STATUS_TIMEOUT", 0.1)
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        await instance.regenerate()

        async def connections(_: c.ConnectedShards) -> Any:
            if hang:
                await asyncio.Event().wait()
            raise TimeoutError

        master.handlers[c.ConnectedShards] = connections
        assert (await instance.invoke(c.ClusterStatusQuery(timeout=0.05))).busy
        assert await instance._runtime_ready()
        assert instance._loading
        del master.handlers[c.ConnectedShards]
        assert not (await instance.status()).busy


async def test_delayed_loading_observation_cannot_complete_a_new_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        await instance.regenerate()
        entered, release = asyncio.Event(), asyncio.Event()

        async def connections(command: c.ConnectedShards) -> Any:
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await master.dispatch(command)

        master.handlers[c.ConnectedShards] = connections
        status = asyncio.create_task(instance.status())
        try:
            await wait_for_event(entered, status)
            assert not (await instance.status()).busy
            await instance.regenerate()
            release.set()
            await status
            assert instance._loading
        finally:
            release.set()
            await status


@pytest.mark.parametrize(
    "operation",
    [
        c.ClusterSave(timeout=0.01),
        c.Reset(timeout=0.01),
        c.Rollback(timeout=0.01),
        c.RollbackToDay(day=8, timeout=0.01),
        c.Regenerate(timeout=0.01),
    ],
)
async def test_connection_timeout_does_not_submit_a_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: c.Request[Any],
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        master.connected_ids = ("Master",)
        master.requests.clear()
        with pytest.raises(TimeoutError):
            await instance.invoke(operation)
        assert not any(
            isinstance(
                command, c.Save | c.Regenerate | c.RollbackToSnapshot | c.Start | c.Stop
            )
            for command in master.requests
        )


@pytest.mark.parametrize("count", [0, 1, 2])
async def test_count_rollback_selects_an_explicit_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        await instance.rollback(count)
        assert master.runtime.snapshot == caves.runtime.snapshot == 91 - count
        assert (
            c.RollbackToSnapshot(session_id="Master", snapshot_id=90 - count)
            in master.requests
        )
        assert not any(
            isinstance(command, c.Rollback | c.Reset) for command in master.requests
        )


@pytest.mark.parametrize(
    ("shard", "command", "starting"),
    [
        (None, c.Start(), True),
        (None, c.Restart(notice=None), True),
        (None, c.UpdateMods(restart=True, notice=None), True),
        (None, c.Stop(notice=None), False),
        (None, c.Kill(), False),
        ("Caves", c.Stop(notice=None), False),
    ],
    ids=["start", "restart", "update-mods", "stop", "kill", "shard-stop"],
)
async def test_manual_lifecycle_replaces_pending_initialization(
    empty_controller: ClusterController,
    monkeypatch: pytest.MonkeyPatch,
    shard: str | None,
    command: c.Request[None],
    starting: bool,
) -> None:
    instance = empty_controller
    prepare = AsyncMock(return_value=layout(instance.cluster_path))
    monkeypatch.setattr(service, "prepare_shared", prepare)
    calls: list[str] = []
    master = EndpointStub("Master", True, calls)
    caves = EndpointStub("Caves", False, calls)
    master.peers = caves.peers = (master, caves)
    await instance.register(master)
    await instance.register(caves)

    target = instance if shard is None else instance.shard(shard)
    await target.invoke(command)
    await asyncio.wait_for(instance.wait_idle(), 1)

    assert not instance._fatal.is_set()
    if starting:
        assert calls.count("start:Master") == calls.count("start:Caves") == 1
        prepare.assert_awaited_once()
    else:
        assert master.phase == caves.phase == ShardPhase.STOPPED
        assert not any(call.startswith("start:") for call in calls)
        prepare.assert_not_awaited()
        await instance.start()
    assert (await instance.status()).phase == "running"


@pytest.mark.parametrize("command", [c.Start(), c.Restart(notice=None)])
@pytest.mark.parametrize("preparing", [False, True])
async def test_shard_start_preserves_initialization(
    empty_controller: ClusterController,
    monkeypatch: pytest.MonkeyPatch,
    command: c.Request[None],
    preparing: bool,
) -> None:
    instance = empty_controller
    entered, release = asyncio.Event(), asyncio.Event()

    async def prepare_shared(*_: object, **__: object) -> tuple[Shard, ...]:
        entered.set()
        await release.wait()
        return layout(instance.cluster_path)

    prepare = AsyncMock(side_effect=prepare_shared)
    monkeypatch.setattr(service, "prepare_shared", prepare)
    calls: list[str] = []
    master = EndpointStub("Master", True, calls)
    caves = EndpointStub("Caves", False, calls)
    master.peers = caves.peers = (master, caves)
    await instance.register(master)
    await instance.register(caves)
    initial = instance._initial_task
    assert initial is not None
    if preparing:
        await wait_for_event(entered, initial)

    try:
        message = "busy" if preparing else "room resources are not prepared"
        with pytest.raises(RuntimeError, match=message):
            await instance.shard("Caves").invoke(command)
    finally:
        release.set()

    assert calls == []
    assert not initial.cancelling()
    await asyncio.wait_for(instance.wait_idle(), 1)
    prepare.assert_awaited_once()
    assert calls == [
        "activate:Master",
        "activate:Caves",
        "start:Master",
        "start:Caves",
    ]
    assert (await instance.status()).phase == "running"


async def test_stop_interrupts_initialization_waiting_for_native_connections(
    empty_controller: ClusterController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = empty_controller
    monkeypatch.setattr(
        service, "prepare_shared", AsyncMock(return_value=layout(instance.cluster_path))
    )
    master = EndpointStub("Master", True, [])
    caves = EndpointStub("Caves", False, [])
    master.peers = caves.peers = (master, caves)
    master.connected_ids = ("Master",)
    checked = asyncio.Event()

    async def connected(command: c.ConnectedShards) -> Any:
        checked.set()
        return await master.dispatch(command)

    master.handlers[c.ConnectedShards] = connected
    await instance.register(master)
    await instance.register(caves)
    initial = instance._initial_task
    assert initial is not None
    await wait_for_event(checked, initial)

    with pytest.raises(RuntimeError, match="busy"):
        await instance.start()
    await instance.stop(notice=None)
    await asyncio.wait_for(instance.wait_idle(), 1)

    assert initial.cancelled()
    assert not instance._fatal.is_set()
    assert master.phase == caves.phase == ShardPhase.STOPPED
    assert (await instance.status()).phase == "stopped"


@pytest.mark.parametrize("shard_only", [False, True])
async def test_initialization_respects_stop_before_all_agents_register(
    empty_controller: ClusterController,
    monkeypatch: pytest.MonkeyPatch,
    shard_only: bool,
) -> None:
    instance = empty_controller
    monkeypatch.setattr(service, "prepare_shared", AsyncMock())
    master = EndpointStub("Master", True, [])
    caves = EndpointStub("Caves", False, [])
    master.peers = caves.peers = (master, caves)
    await instance.register(master)
    target = instance.shard("Master") if shard_only else instance
    await target.stop(notice=None)
    await instance.register(caves)
    await asyncio.wait_for(instance.wait_idle(), 1)

    assert not instance._fatal.is_set()
    assert master.phase == "stopped"
    assert caves.phase == ("running" if shard_only else "stopped")
    await instance.start()
    assert (await instance.status()).phase == "running"


@pytest.mark.parametrize("budget", [0.01, 30])
async def test_status_preserves_diagnostics_when_native_queries_time_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget: float,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        before = await instance.status()
        cancelled = asyncio.Event()

        async def hang(_: c.Runtime) -> None:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        master.handlers[c.Runtime] = hang
        monkeypatch.setattr(controller_module, "AGENT_STATUS_TIMEOUT", budget)
        status = await instance.invoke(c.ClusterStatusQuery(timeout=0.5))

        assert cancelled.is_set()
        assert status.phase == "degraded"
        assert status.shards == before.shards
        assert status.error == before.error


@pytest.mark.parametrize(
    "failure",
    [
        LuaRequestError("not_ready"),
        RemoteError(ErrorInfo(ErrorCode.INVALID_STATE, ULID(), "not ready")),
        RemoteError(ErrorInfo(ErrorCode.TIMEOUT, ULID(), "command timed out")),
        IndeterminateError(),
    ],
)
async def test_shard_mutation_preserves_a_confirmed_error_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, caves, _, _ = room
        caves.handlers[c.Pause] = AsyncMock(side_effect=failure)
        with pytest.raises(type(failure)) as caught:
            await instance.shard("Caves").invoke(c.Pause(paused=True))
        assert caught.value is failure


@pytest.mark.parametrize(
    "operation",
    [
        c.ExecuteAll(source="return true", timeout=0.5),
        c.ClusterPause(paused=True, timeout=0.5),
        c.Announce(message="test", timeout=0.5),
        c.Whitelist(userid="KU_one", timeout=0.5),
    ],
)
async def test_mutation_forwarding_timeout_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: c.Request[Any],
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room

        async def hang(_: c.Request[Any]) -> None:
            await asyncio.Event().wait()

        for kind in (c.Execute, c.Pause, c.Announce, c.Whitelist):
            master.handlers[kind] = hang
        with pytest.raises(IndeterminateError):
            await instance.invoke(operation)


async def test_save_only_submits_once_without_optional_presence_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        for agent in (master, caves):
            agent.handlers[c.Presence] = AsyncMock(
                side_effect=RuntimeError("unavailable")
            )
            agent.handlers[c.Room] = AsyncMock(side_effect=RuntimeError("unavailable"))
            agent.requests.clear()
        assert await instance.save() is None
        assert sum(isinstance(request, c.Save) for request in master.requests) == 1
        assert not any(isinstance(request, c.Save) for request in caves.requests)


@pytest.mark.parametrize("code", [ErrorCode.INDETERMINATE, ErrorCode.INVALID_ARGUMENT])
@pytest.mark.parametrize("shard", [None, "Caves"])
async def test_remote_reload_rejection_and_unknown_result_have_distinct_busy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: ErrorCode, shard: str | None
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        endpoint = master if shard is None else caves
        command = c.Regenerate if shard is None else c.RegenerateShard
        endpoint.handlers[command] = AsyncMock(
            side_effect=RemoteError(ErrorInfo(code, ULID(), "test"))
        )
        target = instance if shard is None else instance.shard(shard)
        with pytest.raises(RemoteError):
            await target.invoke(command())
        assert (await instance.status()).busy is (code is ErrorCode.INDETERMINATE)
        assert sum(isinstance(item, command) for item in endpoint.requests) == 1


@pytest.mark.parametrize("responsive", [True, False])
async def test_runtime_monitor_ignores_optional_telemetry_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, responsive: bool
) -> None:
    monkeypatch.setattr(controller_module, "HEALTH_INTERVAL", 0.005)
    monkeypatch.setattr(controller_module, "HEALTH_FAILURE_TIMEOUT", 0.025)
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room

        original_status = master.runtime_status

        async def status() -> ShardRuntimeStatus:
            value = await original_status()
            health = value.driver_health
            return value.replace(
                driver_health=health.replace(telemetry_status="degraded", errors=2)
                if health is not None
                else None
            )

        monkeypatch.setattr(master, "runtime_status", status)

        async def runtime(command: c.Runtime) -> Any:
            if not responsive:
                await asyncio.Event().wait()
            return await master.dispatch(command)

        master.handlers[c.Runtime] = runtime
        if responsive:
            await asyncio.sleep(0.05)
            assert not instance._fatal.is_set()
        else:
            with pytest.raises(ControllerOperationError):
                await asyncio.wait_for(instance.wait_fatal(), 1)
            assert "5 minutes" in ((await instance.status()).error or "")
        assert master.ready
        assert caves.ready
        assert not any(isinstance(item, c.Restart) for item in master.requests)


async def test_empty_regeneration_checks_secondary_connections_before_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        presence = await caves.dispatch(c.Presence())
        caves.handlers[c.Presence] = AsyncMock(
            return_value=presence.replace(client_count=1)
        )
        with pytest.raises(RuntimeError, match="empty shards"):
            await instance.regenerate(require_empty=True)
        assert not any(isinstance(item, c.Regenerate) for item in master.requests)


async def test_close_joins_owned_finalizers_after_native_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller_module, "CONTROLLER_CANCEL_TIMEOUT", 0)
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    cleaning, release = asyncio.Event(), asyncio.Event()

    async def background() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    task = instance._mod_task = asyncio.create_task(background())
    await asyncio.sleep(0)
    closing = asyncio.create_task(instance.aclose())
    try:
        await wait_for_event(cleaning, closing)
        assert not closing.done()
        release.set()
        await asyncio.wait_for(closing, 1)
        assert task.done()
        assert master.pid is None
        assert caves.pid is None
    finally:
        release.set()
        await closing


async def test_stopped_shard_loading_is_reconciled_after_lost_stop_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room

        async def regenerate(command: c.Regenerate) -> None:
            await master.dispatch(command)
            master.ready = caves.ready = False

        async def stop(command: c.Stop) -> None:
            await caves.dispatch(command)
            raise RemoteError(ErrorInfo(ErrorCode.INDETERMINATE, ULID(), "lost reply"))

        master.handlers[c.Regenerate] = regenerate
        await instance.regenerate()
        caves.handlers[c.Stop] = stop
        with pytest.raises(RemoteError):
            await instance.shard("Caves").stop(notice=None)
        del caves.handlers[c.Stop]
        assert caves.phase == "stopped"
        assert caves.pid is None
        original_status = caves.runtime_status
        observed_attempt = ULID()

        async def stopped_status() -> ShardRuntimeStatus:
            return (await original_status()).replace(game_attempt=observed_attempt)

        monkeypatch.setattr(caves, "runtime_status", stopped_status)
        assert (await instance.status()).busy
        assert tuple(instance._loading) == ("Master", "Caves")
        observed_attempt = caves.attempt
        assert (await instance.status()).busy
        assert tuple(instance._loading) == ("Master",)
        master.ready = True
        assert not (await instance.status()).busy


async def test_loading_monitor_rechecks_operation_after_awaiting_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room

        async def regenerate(command: c.Regenerate) -> None:
            await master.dispatch(command)
            master.ready = caves.ready = False

        master.handlers[c.Regenerate] = regenerate
        await instance.regenerate()
        observed, release_observation = asyncio.Event(), asyncio.Event()
        original_status = master.runtime_status

        async def delayed_status() -> ShardRuntimeStatus:
            status = await original_status()
            observed.set()
            await release_observation.wait()
            return status

        monkeypatch.setattr(master, "runtime_status", delayed_status)
        monitor = asyncio.create_task(instance._runtime_ready())
        caves.stop_entered, caves.stop_release = asyncio.Event(), asyncio.Event()
        stopping: asyncio.Task[None] | None = None
        try:
            await wait_for_event(observed, monitor)
            stopping = asyncio.create_task(instance.shard("Caves").stop(notice=None))
            await wait_for_event(caves.stop_entered, stopping)
            instance._load_deadline = 0
            release_observation.set()
            assert await monitor is None
            assert not stopping.done()
            assert instance._lock.locked()
        finally:
            release_observation.set()
            caves.stop_release.set()
            await asyncio.gather(
                monitor,
                *((stopping,) if stopping is not None else ()),
                return_exceptions=True,
            )


@pytest.mark.parametrize("shard", [None, "Caves"])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_startup_readiness_outlives_the_callers_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shard: str | None,
    restart: bool,
    cancel: bool,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        if not restart:
            await instance.stop(notice=None)
            if shard is not None:
                await instance.shard("Master").start()
        master.connected_ids = ("Master",)
        checked = asyncio.Event()

        async def connected(command: c.ConnectedShards) -> Any:
            checked.set()
            return await master.dispatch(command)

        master.handlers[c.ConnectedShards] = connected
        timeout = 10 if cancel else 0.05
        command = (
            c.Restart(notice=None, timeout=timeout)
            if restart
            else c.Start(timeout=timeout)
        )
        target = instance if shard is None else instance.shard(shard)
        task = asyncio.create_task(target.invoke(command))
        try:
            await wait_for_event(checked, task)
            if cancel:
                task.cancel()
            with pytest.raises(
                asyncio.CancelledError if cancel else IndeterminateError
            ):
                await task
            assert instance._loading
            assert (await instance.status()).busy
            assert await instance._runtime_ready()
            with pytest.raises(RuntimeError, match="busy"):
                await instance.shard("Master").execute("return true")
            master.connected_ids = ("Master", "Caves")
            assert await instance._runtime_ready()
            assert not instance._loading
            assert not instance._fatal.is_set()
        finally:
            await asyncio.gather(task, return_exceptions=True)


async def test_room_startup_deadline_bounds_a_longer_rpc_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        await instance.stop(notice=None)
        master.connected_ids = ("Master",)
        monkeypatch.setattr(controller_module, "DEFAULT_STARTUP_TIMEOUT", 0.025)
        with pytest.raises(ControllerOperationError):
            await asyncio.wait_for(instance.invoke(c.Start(timeout=10)), 0.5)
        assert instance._fatal.is_set()
        assert master.phase == caves.phase == "stopped"


async def test_cancelled_restart_requires_a_new_process_before_becoming_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, caves, _, _ = room

        async def restart(_: c.Restart) -> None:
            await asyncio.Event().wait()

        caves.handlers[c.Restart] = restart
        with pytest.raises(IndeterminateError):
            await instance.shard("Caves").restart(notice=None, timeout=0.02)
        assert await instance._runtime_ready()
        assert instance._loading
        await caves.dispatch(c.Restart())
        assert await instance._runtime_ready()
        assert not instance._loading


async def test_old_status_cannot_complete_a_later_process_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        queried, release = asyncio.Event(), asyncio.Event()
        original_status = master.runtime_status

        async def delayed_status() -> ShardRuntimeStatus:
            status = await original_status()
            if not queried.is_set():
                queried.set()
                await release.wait()
            return status

        monkeypatch.setattr(master, "runtime_status", delayed_status)
        status_task = asyncio.create_task(instance.status())
        try:
            await wait_for_event(queried, status_task)
            await instance.shard("Master").restart(notice=None)

            async def restarting(_: c.Restart) -> None:
                await asyncio.Event().wait()

            master.handlers[c.Restart] = restarting
            with pytest.raises(IndeterminateError):
                await instance.shard("Master").restart(notice=None, timeout=0.02)
            loading = instance._loading
            release.set()
            await status_task
            assert instance._loading is loading
            assert await instance._runtime_ready()
            assert instance._loading
        finally:
            release.set()
            await status_task


async def test_startup_deadline_survives_a_shorter_rpc_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller_module, "HEALTH_INTERVAL", 0.005)
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        await instance.stop(notice=None)
        master.connected_ids = ("Master",)
        monkeypatch.setattr(controller_module, "DEFAULT_STARTUP_TIMEOUT", 0.04)
        with pytest.raises(IndeterminateError):
            await instance.invoke(c.Start(timeout=0.01))
        with pytest.raises(ControllerOperationError):
            await asyncio.wait_for(instance.wait_fatal(), 0.5)


async def test_reload_deadline_also_bounds_the_command_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room

        async def regenerate(_: c.Regenerate) -> None:
            await asyncio.Event().wait()

        master.handlers[c.Regenerate] = regenerate
        monkeypatch.setattr(controller_module, "DEFAULT_RELOAD_TIMEOUT", 0.025)
        with pytest.raises(IndeterminateError):
            await asyncio.wait_for(instance.regenerate(timeout=10), 0.5)
        with pytest.raises(TimeoutError, match="loading"):
            await instance._runtime_ready()


async def test_monitor_reconciles_lost_stop_reply_when_all_shards_are_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room

        async def regenerate(command: c.Regenerate) -> None:
            await master.dispatch(command)
            master.ready = caves.ready = False

        master.handlers[c.Regenerate] = regenerate
        await instance.regenerate()
        for endpoint in (master, caves):
            status = await endpoint.runtime_status()
            monkeypatch.setattr(
                endpoint,
                "runtime_status",
                AsyncMock(
                    return_value=status.replace(phase=ShardPhase.STOPPED, pid=None)
                ),
            )

        async def stop(command: c.Stop) -> None:
            await master.dispatch(command)
            await asyncio.Event().wait()

        master.handlers[c.Stop] = stop
        with pytest.raises(IndeterminateError):
            await instance.stop(notice=None, timeout=0.01)
        assert instance._loading
        assert await instance._runtime_ready()
        assert not instance._loading
        assert not instance._fatal.is_set()
        del master.handlers[c.Stop]


@pytest.mark.parametrize("outcome", ["reconnect", "disconnected", "query-failed"])
async def test_running_topology_has_one_failure_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.setattr(controller_module, "HEALTH_INTERVAL", 0.005)
    monkeypatch.setattr(controller_module, "HEALTH_FAILURE_TIMEOUT", 0.04)
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        master.connected_ids = ("Master",)
        assert not await instance._runtime_ready()
        assert not instance._fatal.is_set()
        if outcome == "reconnect":
            master.connected_ids = ("Master", "Caves")
            await asyncio.sleep(0.08)
            assert not instance._fatal.is_set()
        else:
            if outcome == "query-failed":
                master.handlers[c.ConnectedShards] = AsyncMock(side_effect=TimeoutError)
            with pytest.raises(ControllerOperationError):
                await asyncio.wait_for(instance.wait_fatal(), 1)


@pytest.mark.parametrize("stopped", ["Master", "Caves", "all"])
async def test_monitor_only_requires_intentionally_running_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stopped: str
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        if stopped == "all":
            await instance.stop(notice=None)
        else:
            await instance.shard(stopped).stop(notice=None)
        for endpoint in (master, caves):
            endpoint.connected_ids = (endpoint.name,)
            if endpoint.phase == "stopped":
                endpoint.fail_status = True
                endpoint.handlers[c.Runtime] = AsyncMock(side_effect=AssertionError)
        try:
            assert await instance._runtime_ready()
            assert not instance._fatal.is_set()
        finally:
            master.fail_status = caves.fail_status = False


async def test_stale_connection_probe_cannot_fail_an_intentional_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room
        queried, release = asyncio.Event(), asyncio.Event()

        async def connected(_: c.ConnectedShards) -> tuple[()]:
            queried.set()
            await release.wait()
            return ()

        master.handlers[c.ConnectedShards] = connected
        monitor = asyncio.create_task(instance._runtime_ready())
        try:
            await wait_for_event(queried, monitor)
            await instance.shard("Master").stop(notice=None)
            release.set()
            assert await monitor is None
        finally:
            release.set()
            await monitor


async def test_mod_preparation_does_not_consume_the_startup_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller_module, "HEALTH_INTERVAL", 0.005)
    monkeypatch.setattr(controller_module, "HEALTH_FAILURE_TIMEOUT", 0.02)
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, _, prepare, _ = room
        entered, release = asyncio.Event(), asyncio.Event()

        async def preparing(*_: Any, **__: Any) -> None:
            entered.set()
            await release.wait()

        prepare.side_effect = preparing
        monkeypatch.setattr(controller_module, "DEFAULT_STARTUP_TIMEOUT", 0.04)
        updating = asyncio.create_task(instance.update_mods(restart=True, notice=None))
        try:
            await wait_for_event(entered, updating)
            await asyncio.sleep(0.08)
            assert not instance._loading
            assert not instance._fatal.is_set()
            release.set()
            await asyncio.wait_for(updating, 1)
            assert (await instance.status()).phase == "running"
        finally:
            release.set()
            await updating


async def test_mod_observation_cannot_renew_a_disconnected_rooms_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dst_server.mods import maintenance

    monkeypatch.setattr(controller_module, "HEALTH_INTERVAL", 0.01)
    monkeypatch.setattr(controller_module, "HEALTH_FAILURE_TIMEOUT", 0.05)
    monkeypatch.setattr(maintenance, "STATUS_INTERVAL", 0.01)
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, _, _, _ = room

        async def presence(command: c.Presence) -> Any:
            await asyncio.sleep(0.025)
            return await master.dispatch(command)

        master.handlers[c.Presence] = presence
        master.connected_ids = ("Master",)
        with pytest.raises(ControllerOperationError):
            await asyncio.wait_for(instance.wait_fatal(), 0.5)
