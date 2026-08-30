import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from ulid import ULID

import dst_server.cluster.controller as controller_module
from dst_server.cluster import service
from dst_server.cluster.config import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.cluster.configuration import (
    ConfigurationSnapshot,
    ConfigurationStore,
    GamesRunningError,
    InvalidConfiguration,
    InvalidConfigurationError,
    TopologyChangeError,
)
from dst_server.cluster.controller import (
    AgentEndpoint,
    ClusterController,
    ControllerOperationError,
    PlayerLocationConflictError,
)
from dst_server.cluster.layout import Shard
from dst_server.cluster.subscriptions import Broadcast
from dst_server.events.server import SavedEvent
from dst_server.models import Player, PlayerState
from dst_server.rpc.errors import DisconnectedError, IndeterminateError
from dst_server.rpc.models import (
    GameEventRecord,
    LifecycleRecord,
    LogRecord,
    ShardRuntimeStatus,
)
from dst_server.rpc.models import ShardPhase as RpcShardPhase


def configuration() -> ClusterConfig:
    return ClusterConfig(
        settings=ClusterSettings(
            master_ip="127.0.0.1",
            cluster_key=SecretStr("test-key"),
        ),
        shards={
            "Master": ShardConfig(
                settings=ShardSettings(
                    is_master=True,
                    server_port=10999,
                    master_server_port=27016,
                )
            ),
            "Caves": ShardConfig(
                settings=ShardSettings(
                    is_master=False,
                    name="Caves",
                    server_port=11000,
                    master_server_port=27017,
                )
            ),
        },
    )


def layout(root: Path) -> tuple[Shard, ...]:
    return (
        Shard("Master", True, root / "console"),
        Shard("Caves", False, root / "Caves" / "console"),
    )


def player(userid: str, *, active: bool) -> Player:
    state = (
        PlayerState(
            network_score=None,
            combat_target=None,
            weapon=None,
            mount=None,
            follower_count=0,
            followers=(),
            skill_xp=None,
            available_skill_points=None,
            activated_skills=None,
        )
        if active
        else None
    )
    return Player(
        userid=userid,
        name=userid,
        prefab="wilson",
        admin=False,
        moderator=False,
        is_ghost=False,
        position=None,
        age=None,
        vitals=None,
        state=state,
    )


class EndpointStub:
    def __init__(
        self,
        name: str,
        master: bool,
        calls: list[str],
        *,
        incarnation: str | None = None,
    ) -> None:
        self.name = name
        self.master = master
        self.incarnation = incarnation or str(ULID())
        self.calls = calls
        self.phase: RpcShardPhase = "stopped"
        self.ready = False
        self.pid: int | None = None
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[LifecycleRecord]()
        self.game_events = Broadcast[GameEventRecord]()
        self.fail_activate = False
        self.fail_pause = False
        self.fail_status = False
        self.fail_stop = False
        self.stop_entered: asyncio.Event | None = None
        self.stop_release: asyncio.Event | None = None
        self.players: tuple[Player, ...] = ()
        self.save_sequence = 10 if master else 20
        self.generation = 30 if master else 40

    async def runtime_status(self) -> ShardRuntimeStatus:
        if self.fail_status:
            message = "status secret"
            raise RuntimeError(message)
        return ShardRuntimeStatus(
            name=self.name,
            is_master=self.master,
            desired="running",
            phase=self.phase,
            agent_incarnation=ULID.from_str(self.incarnation),
            pid=self.pid,
            ready=self.ready,
            telemetry_profile="critical",
        )

    def __getattr__(self, operation: str) -> object:  # ruff: ignore[complex-structure]
        async def invoke(  # ruff: ignore[complex-structure, too-many-branches]
            *args: object,
            **kwargs: object,
        ) -> object:
            del kwargs
            rendered = ":".join(str(value) for value in args)
            self.calls.append(
                f"{operation.replace('_', '-')}:{self.name}"
                + (f":{rendered}" if rendered else "")
            )
            if operation == "activate" and self.fail_activate:
                message = "activation secret"
                raise RuntimeError(message)
            if operation == "pause" and self.fail_pause:
                message = "pause secret"
                raise RuntimeError(message)
            if operation == "stop":
                if self.stop_entered is not None:
                    self.stop_entered.set()
                if self.stop_release is not None:
                    await self.stop_release.wait()
                if self.fail_stop:
                    message = "stop failed"
                    raise RuntimeError(message)
            if operation in {"start", "restart"}:
                self.phase, self.ready, self.pid = "running", True, 1
            elif operation in {"stop", "kill"}:
                self.phase, self.ready, self.pid = "stopped", False, None
            if operation == "execute":
                return f"{self.name}:{args[0]}"
            if operation == "save_marker":
                return self.save_sequence
            if operation == "save":
                return SavedEvent(path="session/7", snapshot=7)
            if operation == "wait_saved":
                snapshot = cast(int | None, args[1])
                return SavedEvent(path=f"{self.name}/{snapshot}", snapshot=snapshot)
            if operation == "generation_marker":
                return self.generation
            if operation == "wait_generation":
                return cast(int, args[0]) + 1
            if operation == "pause":
                return args[0]
            if operation == "list_players":
                return self.players
            if operation == "get_player":
                return next(
                    (item for item in self.players if item.userid == args[0]),
                    None,
                )
            if operation in {"is_whitelisted", "whitelist"}:
                return True
            if operation == "unwhitelist":
                return False
            return None

        return invoke


async def controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    ClusterController,
    EndpointStub,
    EndpointStub,
    AsyncMock,
    list[str],
]:
    root = tmp_path / "cluster"
    configuration().save(root)
    shards = layout(root)
    prepare = AsyncMock(return_value=shards)
    monkeypatch.setattr(service, "prepare_shared", prepare)
    calls: list[str] = []
    instance = ClusterController(
        ConfigurationStore(root),
        install_path=tmp_path / "install",
    )
    master = EndpointStub("Master", True, calls)
    caves = EndpointStub("Caves", False, calls)
    await instance.register(cast(AgentEndpoint, master))
    assert not prepare.await_count
    await instance.register(cast(AgentEndpoint, caves))
    await instance.wait_idle()
    return instance, master, caves, prepare, calls


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
        await empty_controller.register(cast(AgentEndpoint, endpoint))
        assert empty_controller.agent("Caves") is endpoint
    else:
        with pytest.raises((TypeError, ValueError), match=message):
            await empty_controller.register(cast(AgentEndpoint, endpoint))
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

        await instance.restart()
        assert prepare.await_count == 1
        assert calls.count("activate:Master") == 2
        assert calls.count("activate:Caves") == 2
        assert calls[-2:] == ["restart:Master", "restart:Caves"]
    finally:
        await instance.aclose()
    assert master.phase == caves.phase == "stopped"
    kills = calls.count("kill:Master") + calls.count("kill:Caves")
    assert not await instance.unregister(cast(AgentEndpoint, caves))
    assert not await instance.failed(cast(AgentEndpoint, caves))
    with pytest.raises(RuntimeError, match="closed"):
        await instance.register(cast(AgentEndpoint, caves))
    assert calls.count("kill:Master") + calls.count("kill:Caves") == kills


async def test_new_controller_adopts_already_running_agents(
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
    master.phase = caves.phase = "running"
    master.ready = caves.ready = True

    await instance.register(cast(AgentEndpoint, master))
    await instance.register(cast(AgentEndpoint, caves))
    await instance.wait_idle()
    try:
        assert master.phase == caves.phase == "running"
        assert calls == [
            "activate:Master",
            "activate:Caves",
            "start:Master",
            "start:Caves",
        ]
        assert prepare.await_count == 1
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
    caves.fail_activate = True
    await instance.register(cast(AgentEndpoint, master))
    await instance.register(cast(AgentEndpoint, caves))
    await instance.wait_idle()
    try:
        assert not any(call.startswith("start:") for call in calls)
        status = await instance.status()
        assert status.phase == "failed"
        assert status.error == "cluster start failed"
        assert "secret" not in status.error

        caves.fail_activate = False
        await instance.start()
        assert prepare.await_count == 1
        assert master.phase == caves.phase == "running"
    finally:
        await instance.aclose()


async def test_registry_fail_closes_and_new_incarnation_resumes_desired_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, prepare, calls = await controller(tmp_path, monkeypatch)
    try:
        with pytest.raises(RuntimeError, match="already registered"):
            await instance.register(cast(AgentEndpoint, caves))
        unknown = EndpointStub("Unknown", False, calls)
        with pytest.raises(KeyError, match="unknown shard"):
            await instance.register(cast(AgentEndpoint, unknown))

        stale = EndpointStub("Caves", False, calls)
        assert not await instance.unregister(cast(AgentEndpoint, stale))
        assert await instance.unregister(cast(AgentEndpoint, caves))
        assert calls[-1] == "stop:Master"
        assert "kill:Master" not in calls
        reconnected = EndpointStub(
            "Caves",
            False,
            calls,
            incarnation=caves.incarnation,
        )
        await instance.register(cast(AgentEndpoint, reconnected))
        await instance.wait_idle()
        assert master.phase == reconnected.phase == "running"

        assert not await instance.failed(cast(AgentEndpoint, caves))
        reconnected.phase = "failed"
        assert await instance.failed(cast(AgentEndpoint, reconnected))
        assert (await instance.status()).phase == "failed"
        assert await instance.unregister(cast(AgentEndpoint, reconnected))

        replacement = EndpointStub("Caves", False, calls)
        await instance.register(cast(AgentEndpoint, replacement))
        await instance.wait_idle()

        assert prepare.await_count == 1
        assert calls.count("activate:Master") == 3
        assert calls.count("activate:Caves") == 3
        assert master.phase == replacement.phase == "running"
        assert (await instance.status()).phase == "running"
    finally:
        await instance.aclose()


async def test_fail_close_kills_only_peers_that_cannot_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, calls = await controller(tmp_path, monkeypatch)
    master.fail_stop = True
    try:
        assert await instance.unregister(cast(AgentEndpoint, caves))
        assert calls[-2:] == ["stop:Master", "kill:Master"]
        assert "kill:Caves" not in calls
        status = await instance.status()
        assert status.phase == "degraded"
        assert status.shards[0].desired == "running"
    finally:
        master.fail_stop = False
        await instance.aclose()


async def test_fail_close_kill_failure_is_reported_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    original_kill = master.kill
    fail_kill = AsyncMock(side_effect=RuntimeError())

    master.fail_stop = True
    monkeypatch.setattr(master, "kill", fail_kill)
    try:
        caves.phase = "failed"
        with pytest.RaisesGroup(RuntimeError):
            await instance.failed(cast(AgentEndpoint, caves))
        assert master.phase == "running"

        master.fail_stop = False
        monkeypatch.setattr(master, "kill", original_kill)
        assert await instance.failed(cast(AgentEndpoint, caves))
        assert master.phase == "stopped"
    finally:
        master.fail_stop = False
        monkeypatch.setattr(master, "kill", original_kill)
        await instance.aclose()


async def test_late_failure_cannot_fail_close_a_restarted_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, caves, _, calls = await controller(tmp_path, monkeypatch)
    caves.phase = "failed"
    await instance.restart()
    stops = calls.count("stop:Master") + calls.count("stop:Caves")

    try:
        assert await instance.failed(cast(AgentEndpoint, caves))
        assert calls.count("stop:Master") + calls.count("stop:Caves") == stops
    finally:
        await instance.aclose()


async def test_failure_report_fail_closes_when_status_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    caves.fail_status = True

    try:
        assert await instance.failed(cast(AgentEndpoint, caves))
        assert master.phase == caves.phase == "stopped"
    finally:
        caves.fail_status = False
        await instance.aclose()


async def test_disconnect_cleanup_failure_blocks_automatic_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, calls = await controller(tmp_path, monkeypatch)
    original_kill = master.kill
    fail_kill = AsyncMock(side_effect=RuntimeError())
    master.fail_stop = True
    monkeypatch.setattr(master, "kill", fail_kill)
    try:
        with pytest.RaisesGroup(RuntimeError):
            await instance.unregister(cast(AgentEndpoint, caves))
        assert master.phase == "running"
        with pytest.raises(DisconnectedError):
            instance.agent("Caves")
        assert not await instance.unregister(cast(AgentEndpoint, caves))

        master.fail_stop = False
        monkeypatch.setattr(master, "kill", original_kill)
        replacement = EndpointStub(
            "Caves",
            False,
            calls,
            incarnation=caves.incarnation,
        )
        await instance.register(cast(AgentEndpoint, replacement))
        await instance.wait_idle()
        assert master.phase == "running"
        assert replacement.phase == "stopped"

        await instance.kill()
        assert master.phase == replacement.phase == "stopped"
    finally:
        master.fail_stop = False
        monkeypatch.setattr(master, "kill", original_kill)
        await instance.aclose()


async def test_close_kill_failure_does_not_seal_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    original_kill = master.kill
    fail_kill = AsyncMock(side_effect=RuntimeError())

    master.fail_stop = True
    monkeypatch.setattr(master, "kill", fail_kill)
    with pytest.RaisesGroup(RuntimeError):
        await instance.aclose()
    assert master.phase == "running"
    assert caves.phase == "stopped"

    master.fail_stop = False
    monkeypatch.setattr(master, "kill", original_kill)
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
        await instance.register(cast(AgentEndpoint, master))

    monkeypatch.setattr(master, "runtime_status", runtime_status)
    await instance.register(cast(AgentEndpoint, master))
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
    await entered.wait()

    async with asyncio.timeout(1):
        await instance.aclose()
    assert not updating.done()

    release.set()
    result = (await asyncio.gather(updating, return_exceptions=True))[0]
    assert isinstance(result, RuntimeError)
    assert "closed" in str(result)


async def test_public_operations_reject_while_close_is_stopping_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, _, _, _ = await controller(tmp_path, monkeypatch)
    shard = instance.shard("Master")
    current = await instance.read_configuration()
    assert isinstance(current, ConfigurationSnapshot)
    master.stop_entered = asyncio.Event()
    master.stop_release = asyncio.Event()
    closing = asyncio.create_task(instance.aclose())
    await master.stop_entered.wait()

    try:
        with pytest.raises(RuntimeError, match="closed"):
            await instance.start()
        with pytest.raises(RuntimeError, match="closed"):
            await instance.restart()
        with pytest.raises(RuntimeError, match="closed"):
            await instance.status()
        with pytest.raises(RuntimeError, match="closed"):
            await instance.read_configuration()
        with pytest.raises(RuntimeError, match="closed"):
            await instance.save_configuration(current.revision, current.configuration)
        with pytest.raises(RuntimeError, match="closed"):
            await shard.status()
        with pytest.raises(RuntimeError, match="closed"):
            await shard.start()
        with pytest.raises(RuntimeError, match="closed"):
            await shard.execute("return true")
        with pytest.raises(RuntimeError, match="closed"):
            instance.subscribe_logs()
    finally:
        master.stop_release.set()
        await closing
    await instance.aclose()


async def test_registered_status_failure_is_reported_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, caves, _, _ = await controller(tmp_path, monkeypatch)
    caves.fail_status = True
    try:
        status = await instance.status()
        unavailable = next(item for item in status.shards if item.name == "Caves")
        assert status.phase == "degraded"
        assert unavailable.phase == "unavailable"
        assert unavailable.error == "shard agent is unavailable"
        assert "secret" not in unavailable.error
        assert (await instance.shard("Caves").status()).phase == "unavailable"
    finally:
        await instance.aclose()


async def test_known_offline_shard_is_unavailable_not_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, caves, _, _ = await controller(tmp_path, monkeypatch)
    shard = instance.shard("Caves")
    try:
        assert await instance.unregister(cast(AgentEndpoint, caves))
        with pytest.raises(DisconnectedError, match="unavailable"):
            await shard.status()
        with pytest.raises(KeyError, match="unknown"):
            instance.shard("Unknown")
    finally:
        await instance.aclose()


async def test_internal_relay_resubscribes_after_overflow(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cluster"
    configuration().save(root)
    calls: list[str] = []
    instance = ClusterController(ConfigurationStore(root))
    master = EndpointStub("Master", True, calls)
    await instance.register(cast(AgentEndpoint, master))
    subscription = instance.subscribe_logs()
    await asyncio.sleep(0)
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
    await asyncio.sleep(0)
    await asyncio.sleep(0)
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
            assert (await subscription.next(1))[0].line == "after-overflow"
        assert "stop:Master" not in calls
    finally:
        subscription.close()
        await instance.aclose()


async def test_closed_internal_relay_does_not_stop_game_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, calls = await controller(tmp_path, monkeypatch)
    master.logs.close()
    try:
        await asyncio.sleep(0)
        status = await instance.status()
        assert status.phase == "running"
        assert status.error is None
        assert not any(call.startswith("stop:") for call in calls)
        assert master.phase == caves.phase == "running"
    finally:
        await instance.aclose()


async def test_save_and_reload_coordinate_every_shard_from_master_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, _, _, calls = await controller(tmp_path, monkeypatch)
    try:
        saved = await instance.save(9)
        assert saved.snapshot == 7
        assert tuple(name for name, _ in saved.shards) == ("Master", "Caves")
        assert calls.count("save:Master:9.0") == 1
        assert "save:Caves:9.0" not in calls
        assert "wait-saved:Master:10:7:9.0" in calls
        assert "wait-saved:Caves:20:7:9.0" in calls

        assert tuple(
            result.value for result in await instance.execute_all("return true", 4)
        ) == ("Master:return true", "Caves:return true")
        caves = instance.shard("Caves")
        assert await caves.execute("return false", 6) == "Caves:return false"
        assert await caves.save(8) == SavedEvent(path="session/7", snapshot=7)
        assert "execute:Master:return true:4.0" in calls
        assert "execute:Caves:return false:6.0" in calls
        assert "save:Caves:8.0" in calls

        with pytest.raises(ValueError, match="positive"):
            await instance.execute_all("return true", 0)
        with pytest.raises(ValueError, match="positive"):
            await caves.execute("return true", 0)
        with pytest.raises(ValueError, match="positive"):
            await caves.save(0)

        await instance.reset(11)
        await instance.rollback(2, 12)
        await instance.regenerate(13)
        assert "reset:Master:11.0" in calls
        assert "rollback:Master:2:12.0" in calls
        assert "regenerate:Master:13.0" in calls
        assert not any(call.startswith("reset:Caves") for call in calls)
        assert calls.count("generation-marker:Master") == 3
        assert calls.count("generation-marker:Caves") == 3
    finally:
        await instance.aclose()


async def test_save_and_reload_disconnects_are_stage_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    generation_marker = master.generation_marker
    save_marker = master.save_marker
    try:
        monkeypatch.setattr(
            master,
            "generation_marker",
            AsyncMock(side_effect=DisconnectedError()),
        )
        with pytest.raises(ControllerOperationError):
            await instance.reset()
        monkeypatch.setattr(master, "generation_marker", generation_marker)
        monkeypatch.setattr(
            caves,
            "wait_generation",
            AsyncMock(side_effect=DisconnectedError()),
        )
        with pytest.raises(IndeterminateError):
            await instance.reset()

        monkeypatch.setattr(
            master, "save_marker", AsyncMock(side_effect=DisconnectedError())
        )
        with pytest.raises(ControllerOperationError):
            await instance.save()
        monkeypatch.setattr(master, "save_marker", save_marker)
        monkeypatch.setattr(
            caves,
            "wait_saved",
            AsyncMock(side_effect=RuntimeError("confirmation failed")),
        )
        with pytest.raises(IndeterminateError):
            await instance.save()
    finally:
        await instance.aclose()


async def test_partial_results_players_and_whitelist_are_cluster_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, calls = await controller(tmp_path, monkeypatch)
    master.players = (player("KU_one", active=False),)
    caves.players = (player("KU_one", active=True),)
    caves.fail_pause = True
    try:
        paused = await instance.pause(True)
        assert paused[0].value is True
        assert paused[0].error is None
        assert paused[1].value is None
        assert isinstance(paused[1].error, RuntimeError)

        players = await instance.list_players()
        assert len(players) == 1
        assert players[0].shard == "Caves"
        assert players[0].player.state is not None
        assert (await instance.get_player("KU_one")) == players[0]

        assert await instance.is_whitelisted("KU_one")
        assert await instance.whitelist("KU_one")
        assert not await instance.unwhitelist("KU_one")
        assert "is-whitelisted:Caves:KU_one" not in calls

        master.players = (player("KU_one", active=True),)
        with pytest.raises(PlayerLocationConflictError):
            await instance.list_players()
    finally:
        await instance.aclose()


async def test_indeterminate_shard_outcomes_are_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    failure = IndeterminateError()
    original_kill = master.kill
    monkeypatch.setattr(caves, "pause", AsyncMock(side_effect=failure))
    try:
        paused = await instance.pause(True)
        assert paused[1].error is failure

        monkeypatch.setattr(caves, "pause", AsyncMock(side_effect=TimeoutError()))
        assert isinstance((await instance.pause(True))[1].error, TimeoutError)

        master.fail_stop = True
        fail_kill = AsyncMock(side_effect=RuntimeError())
        monkeypatch.setattr(master, "kill", fail_kill)
        monkeypatch.setattr(caves, "restart", AsyncMock(side_effect=failure))
        with pytest.raises(IndeterminateError) as caught:
            await instance.restart()
        assert caught.value is failure
        fail_kill.assert_awaited_once()

        master.fail_stop = False
        monkeypatch.setattr(master, "kill", original_kill)
        monkeypatch.setattr(caves, "restart", AsyncMock(side_effect=TimeoutError()))
        with pytest.raises(IndeterminateError):
            await instance.restart()
    finally:
        master.fail_stop = False
        monkeypatch.setattr(master, "kill", original_kill)
        await instance.aclose()


async def test_restart_has_a_separate_stop_and_start_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, caves, _, _ = await controller(tmp_path, monkeypatch)
    restart = cast(Callable[[], Awaitable[object]], caves.restart)

    async def delayed_restart() -> None:
        await asyncio.sleep(0.02)
        await restart()

    monkeypatch.setattr(controller_module, "AGENT_START_TIMEOUT", 0.001)
    monkeypatch.setattr(controller_module, "AGENT_RESTART_TIMEOUT", 0.1)
    monkeypatch.setattr(caves, "restart", delayed_restart)
    try:
        await instance.restart()
        await instance.shard("Caves").restart()
    finally:
        await instance.aclose()


async def test_configuration_save_requires_stopped_games_and_invalidates_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, _, prepare, _ = await controller(tmp_path, monkeypatch)
    try:
        current = await instance.read_configuration()
        assert isinstance(current, ConfigurationSnapshot)
        desired = current.configuration.replace(
            settings=current.configuration.settings.replace(max_players=12)
        )
        with pytest.raises(GamesRunningError):
            await instance.save_configuration(current.revision, desired)

        await instance.stop()
        await instance.update_mods()
        assert prepare.await_count == 2
        prepare.assert_awaited_with(
            tmp_path / "install",
            tmp_path / "cluster",
            update_mods=True,
        )
        saved = await instance.save_configuration(current.revision, desired)
        assert saved.configuration.settings.max_players == 12
        assert (await instance.status()).prepared_revision is None
    finally:
        await instance.aclose()


async def test_failed_shard_with_live_pid_is_not_treated_as_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    current = await instance.read_configuration()
    assert isinstance(current, ConfigurationSnapshot)
    master.phase, master.ready, master.pid = "failed", False, 123
    caves.phase, caves.ready, caves.pid = "stopped", False, None
    try:
        with pytest.raises(GamesRunningError):
            await instance.save_configuration(current.revision, current.configuration)
        with pytest.raises(RuntimeError, match="must be stopped"):
            await instance.update_mods()
    finally:
        await instance.aclose()


async def test_start_rejects_external_deployment_port_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, _, prepare, _ = await controller(tmp_path, monkeypatch)
    original_kill = master.kill
    try:
        await instance.stop()
        server_ini = tmp_path / "cluster" / "Caves" / "server.ini"
        server_ini.write_text(
            server_ini.read_text(encoding="utf-8").replace(
                "server_port = 11000",
                "server_port = 11001",
            ),
            encoding="utf-8",
        )

        master.fail_stop = True
        fail_kill = AsyncMock(side_effect=RuntimeError())
        monkeypatch.setattr(master, "kill", fail_kill)
        with pytest.raises(TopologyChangeError):
            await instance.start()

        fail_kill.assert_awaited_once()
        timeout_kill = AsyncMock(side_effect=TimeoutError())
        monkeypatch.setattr(master, "kill", timeout_kill)
        with pytest.raises(IndeterminateError):
            await instance.start()

        assert prepare.await_count == 1
        assert (await instance.status()).error == "cluster start failed"
    finally:
        master.fail_stop = False
        monkeypatch.setattr(master, "kill", original_kill)
        await instance.aclose()


async def test_cold_invalid_configuration_can_be_read_and_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "cluster"
    desired = configuration()
    desired.save(root)
    (root / "Caves" / "server.ini").write_text(
        """[SHARD]
is_master = false
id = invalid

[STEAM]
master_server_port = 27017

[NETWORK]
server_port = 11000
""",
        encoding="utf-8",
    )
    shards = layout(root)
    prepare = AsyncMock(return_value=shards)
    monkeypatch.setattr(service, "prepare_shared", prepare)
    calls: list[str] = []
    instance = ClusterController(ConfigurationStore(root))
    master = EndpointStub("Master", True, calls)
    caves = EndpointStub("Caves", False, calls)
    await instance.register(cast(AgentEndpoint, master))
    await instance.register(cast(AgentEndpoint, caves))
    await instance.wait_idle()
    try:
        invalid = await instance.read_configuration()
        assert isinstance(invalid, InvalidConfiguration)
        status = await instance.status()
        assert status.revision is not None
        assert status.revision == ULID.from_str(invalid.revision)
        with pytest.raises(InvalidConfigurationError):
            await instance.start()

        saved = await instance.save_configuration(status.revision, desired)
        assert saved.configuration.shards["Caves"].settings.server_port == 11000
        await instance.start()
        assert master.phase == caves.phase == "running"
    finally:
        await instance.aclose()
