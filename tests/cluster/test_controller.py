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
from dst_server.events.server import SavedEvent
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
async def test_cluster_start_updates_mods_after_stopping_all_games(
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
            assert (await instance.status()).prepared is False
            await instance.start()
        else:
            await instance.restart()
        assert prepare.await_count == 2
        assert (await instance.status()).phase == "running"
        await instance.start()
        assert prepare.await_count == 2


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
async def test_restart_does_not_start_games_after_stop_or_update_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    instance, master, caves, prepare, calls = await controller(tmp_path, monkeypatch)
    calls.clear()
    if stage == "stop":
        caves.handlers[c.Stop] = AsyncMock(side_effect=RuntimeError("stop failed"))
    else:
        prepare.side_effect = ModUpdateError("mod update failed")
    try:
        with pytest.raises(
            ControllerOperationError if stage == "stop" else ModUpdateError
        ):
            await instance.restart()
        assert not any(call.startswith(("start:", "restart:")) for call in calls)
        assert prepare.await_count == (1 if stage == "stop" else 2)
        assert master.phase == caves.phase == "stopped"
        assert (await instance.status()).phase in {"failed", "starting"}
        caves.handlers.pop(c.Stop, None)
        prepare.side_effect = None
        previous_updates = prepare.await_count
        await instance.start()
        assert prepare.await_count == previous_updates + 1
    finally:
        caves.handlers.pop(c.Stop, None)
        await instance.aclose()


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
        prepare.side_effect = None
        prepare.return_value = layout(root)
        instance._mod_maintenance.retry_at = 0
        await instance._maintain_mods()
        assert (await instance.status()).phase == "running"
    finally:
        await instance.aclose()


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
        with pytest.RaisesGroup(RuntimeError):
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
    with pytest.RaisesGroup(RuntimeError):
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
    instance, _, _, _, _ = await controller(tmp_path, monkeypatch)
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

            async with asyncio.timeout(1):
                await instance.aclose()
            assert not updating.done()

            release.set()
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
        saved = await instance.save(timeout=9)
        assert saved.snapshot == 91
        assert tuple(name for name, _ in saved.shards) == ("Master", "Caves")
        assert master.requests.count(c.Save(timeout=9)) == 1
        assert c.Save(timeout=9) not in secondary.requests
        assert (
            c.WaitSaved(cursor=secondary.save_cursor, snapshot=91, timeout=9)
            in secondary.requests
        )
        assert not any(
            isinstance(request, c.SaveMarker | c.WaitSaved)
            for request in master.requests
        )

        assert tuple(
            result.value
            for result in await instance.execute_all("return true", timeout=4)
        ) == ("Master:return true", "Caves:return true")
        caves = instance.shard("Caves")
        assert await caves.execute("return false", timeout=6) == "Caves:return false"
        assert await caves.save(timeout=8) == SavedEvent(
            path="session/Caves/0000000092", snapshot=92
        )
        assert c.Execute(source="return true", timeout=4) in master.requests
        assert c.Execute(source="return false", timeout=6) in secondary.requests
        assert c.Save(timeout=8) in secondary.requests

        with pytest.raises(ValueError, match="greater than 0"):
            await instance.execute_all("return true", timeout=0)
        with pytest.raises(ValueError, match="greater than 0"):
            await caves.execute("return true", timeout=0)
        with pytest.raises(ValueError, match="greater than 0"):
            await caves.save(timeout=0)

        await instance.reset(timeout=11)
        await instance.rollback(2, timeout=12)
        await instance.regenerate(
            expected_session_id="SESSION", require_empty=True, timeout=13
        )
        for command in (
            c.RollbackToSnapshot(session_id="Master", snapshot_id=92, timeout=11),
            c.RollbackToSnapshot(session_id="Master", snapshot_id=90, timeout=12),
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
        (c.Restart(notice=None, timeout=0.5), c.Save),
        (c.UpdateMods(restart=True, notice=None, timeout=0.5), c.Save),
    ],
)
@pytest.mark.parametrize("stage", ["command", "confirmation"])
async def test_cluster_timeout_after_submission_is_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: c.Request[Any],
    mutation: type[c.Save | c.RollbackToSnapshot | c.Regenerate],
    stage: str,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        for agent in (master, caves):
            agent.runtime = agent.runtime.replace(snapshot=4)

        async def hang(_: c.Request[Any]) -> None:
            await asyncio.Event().wait()

        async def submit(command: c.Request[Any]) -> Any:
            if stage == "command":
                await hang(command)
            result = await master.dispatch(command)
            if mutation is not c.Save:
                caves.ready = False
            return result

        master.handlers[mutation] = submit
        caves.handlers[c.WaitSaved] = hang
        with pytest.raises(IndeterminateError):
            await instance.invoke(operation)
        assert sum(isinstance(command, mutation) for command in master.requests) == 1
        assert instance._phase is None

        # An unfinished operation must not taint the next operation's preflight.
        caves.ready = True
        master.connected_ids = ("Master",)
        with pytest.raises(TimeoutError):
            await instance.save(timeout=0.01)


@pytest.mark.parametrize(
    "operation",
    [
        c.Restart(notice=None, timeout=0.5),
        c.UpdateMods(restart=True, notice=None, timeout=0.5),
    ],
)
async def test_restart_timeout_after_save_is_indeterminate(
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
        assert any(isinstance(command, c.Save) for command in master.requests)
        assert calls.count("stop:Master") == calls.count("start:Master") == 1
        assert calls.count("stop:Caves") == calls.count("start:Caves") == 1


async def test_cluster_operation_defaults_allow_saving_and_reloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, _, _ = room
        await instance.execute_all("return true")
        await instance.save()
        await instance.reset()
        await instance.rollback()
        await instance.regenerate()
        assert c.Execute(source="return true", timeout=120) in master.requests
        assert c.Save(timeout=300) in master.requests
        assert (
            c.WaitSaved(cursor=caves.save_cursor, snapshot=91, timeout=300)
            in caves.requests
        )
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
        ("wrong_day", IndeterminateError),
        ("wrong_session", IndeterminateError),
        ("wrong_snapshot_same_day", IndeterminateError),
    ],
)
async def test_rollback_to_day_uses_earliest_complete_snapshot_and_verifies_reload(
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
        instance, master, caves, _, _ = room
        master.handlers[c.Runtime] = AsyncMock(side_effect=DisconnectedError())
        with pytest.RaisesGroup(DisconnectedError):
            await instance.reset()
        master.handlers.pop(c.Runtime)
        master.handlers[c.RollbackToSnapshot] = AsyncMock(
            side_effect=DisconnectedError()
        )
        with pytest.raises(IndeterminateError):
            await instance.reset()

        caves.handlers[c.SaveMarker] = AsyncMock(side_effect=DisconnectedError())
        with pytest.RaisesGroup(DisconnectedError):
            await instance.save()
        caves.handlers.pop(c.SaveMarker)
        caves.handlers[c.WaitSaved] = AsyncMock(
            side_effect=RuntimeError("confirmation failed")
        )
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
        assert caves.requests[-1] == c.Restart(timeout=17, notice=None)
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
        assert calls == ["stop:Master", "stop:Caves"]
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
    "outcome", ["new-worlds", "old-cave", "old-generation", "new-process"]
)
async def test_regeneration_requires_new_worlds_in_the_same_processes(
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

        mutation = master.handlers[c.Regenerate] = AsyncMock(side_effect=regenerate)
        if outcome == "new-worlds":
            await instance.regenerate(timeout=1)
        else:
            with pytest.raises(IndeterminateError) as caught:
                await instance.regenerate(timeout=1)
            if outcome == "old-cave":
                assert caught.value.error.fields == ("Caves",)
        mutation.assert_awaited_once()


@pytest.mark.parametrize(
    "operation",
    [
        c.ClusterSave(timeout=0.01),
        c.Reset(timeout=0.01),
        c.Rollback(timeout=0.01),
        c.RollbackToDay(day=8, timeout=0.01),
        c.Regenerate(timeout=0.01),
        c.Restart(notice=None, timeout=0.01),
        c.UpdateMods(restart=True, notice=None, timeout=0.01),
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
