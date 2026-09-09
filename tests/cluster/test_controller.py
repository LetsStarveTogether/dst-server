import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from weakref import ref

import pytest
from pydantic import SecretStr
from ulid import ULID

import dst_server.cluster.controller as controller_module
from dst_server import commands as c
from dst_server.cluster import service
from dst_server.cluster.controller import (
    AgentEndpoint,
    ClusterController,
)
from dst_server.cluster.subscriptions import Broadcast
from dst_server.configuration.files import Shard
from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.configuration.store import (
    ConfigurationStore,
)
from dst_server.errors import (
    ControllerOperationError,
    DisconnectedError,
    ErrorCode,
    GamesRunningError,
    IndeterminateError,
    InvalidConfigurationError,
    PlayerLocationConflictError,
    TopologyChangeError,
)
from dst_server.events.server import SavedEvent
from dst_server.models import Player, PlayerState, Runtime, World
from dst_server.models.cluster import (
    ConfigurationSnapshot,
    GameEventRecord,
    InvalidConfiguration,
    LifecycleRecord,
    LogRecord,
    ObservationCursor,
    ShardDesired,
    ShardPhase,
    ShardRuntimeStatus,
)
from dst_server.models.snapshot import (
    Snapshot,
    SnapshotCatalog,
    SnapshotClock,
    WorldSnapshotMetadata,
)


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


def world(day: int = 8) -> World:
    return World(
        age=7.0,
        cycles=day - 1,
        day=day,
        time=0.0,
        time_in_phase=0.0,
        phase="day",
        is_day=True,
        is_dusk=False,
        is_night=False,
        moon_phase="new",
        is_waxing_moon=False,
        is_full_moon=False,
        is_new_moon=True,
        season="autumn",
        is_spring=False,
        is_summer=False,
        is_autumn=True,
        is_winter=False,
        elapsed_days_in_season=7,
        season_progress=0.5,
        remaining_days_in_season=7,
        spring_length=20,
        summer_length=20,
        autumn_length=20,
        winter_length=20,
        temperature=20.0,
        moisture=0.0,
        moisture_ceiling=100.0,
        precipitation_probability=0.0,
        precipitation_rate=0.0,
        precipitation="none",
        is_raining=False,
        is_snowing=False,
        is_lunar_hailing=False,
        is_acid_raining=False,
        is_snow_covered=False,
        snow_level=0.0,
        lunar_hail_level=0.0,
        lunar_hail_rate=0.0,
        wetness=0.0,
        is_wet=False,
        is_cave=False,
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
        self.requests: list[c.Request[Any]] = []
        self.handlers: dict[type[c.Request[Any]], Callable[[Any], Awaitable[Any]]] = {}
        self.phase = ShardPhase.STOPPED
        self.ready = False
        self.pid: int | None = None
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[LifecycleRecord]()
        self.game_events = Broadcast[GameEventRecord]()
        self.fail_status = False
        self.stop_entered: asyncio.Event | None = None
        self.stop_release: asyncio.Event | None = None
        self.players: tuple[Player, ...] = ()
        self.attempt = ULID()
        self.save_cursor = ObservationCursor(
            attempt=self.attempt, sequence=10 if master else 20
        )
        self.generation_cursor = ObservationCursor(
            attempt=self.attempt, sequence=30 if master else 40
        )
        self.runtime = Runtime(
            session_id=name,
            snapshot=91,
            build_version="123",
            save_version=1.0,
            generated_on_save_version=1.0,
            seed=1,
            level_id="SURVIVAL_TOGETHER",
            branch="release",
            app_version="1",
            shard_id=name,
            is_master_shard=master,
            is_cave=not master,
        )
        self.world = world()

    async def runtime_status(self) -> ShardRuntimeStatus:
        if self.fail_status:
            message = "status secret"
            raise RuntimeError(message)
        return ShardRuntimeStatus(
            name=self.name,
            is_master=self.master,
            desired=ShardDesired.RUNNING,
            phase=self.phase,
            agent_incarnation=ULID.from_str(self.incarnation),
            pid=self.pid,
            ready=self.ready,
            telemetry_profile="critical",
        )

    async def activate(self) -> None:
        await self.invoke(c.Activate())

    async def invoke[T](self, command: c.Request[T]) -> T:
        c.operation("agent", command)
        self.requests.append(command)
        self.calls.append(f"{command.method.replace('_', '-')}:{self.name}")
        result = (
            await handler(command)
            if (handler := self.handlers.get(type(command))) is not None
            else await self.dispatch(command)
        )
        return c.operation("agent", command).response.validate_python(
            result, strict=True
        )

    async def dispatch(self, command: c.Request[Any]) -> Any:  # ruff: ignore[complex-structure, too-many-branches]
        match command:
            case c.Start() | c.Restart():
                self.phase, self.ready, self.pid = ShardPhase.RUNNING, True, 1
            case c.Stop():
                if self.stop_entered is not None:
                    self.stop_entered.set()
                if self.stop_release is not None:
                    await self.stop_release.wait()
                self.phase, self.ready, self.pid = ShardPhase.STOPPED, False, None
            case c.Kill():
                self.phase, self.ready, self.pid = ShardPhase.STOPPED, False, None
            case c.Execute(source=source):
                return f"{self.name}:{source}"
            case c.SaveMarker():
                return self.save_cursor
            case c.Save():
                return SavedEvent(path="session/7", snapshot=7)
            case c.WaitSaved(snapshot=snapshot):
                return SavedEvent(path=f"{self.name}/{snapshot}", snapshot=snapshot)
            case c.GenerationMarker():
                return self.generation_cursor
            case c.WaitGeneration(cursor=cursor):
                return cursor.sequence + 1
            case c.Pause(paused=paused):
                return paused
            case c.ListPlayers():
                return self.players
            case c.GetPlayer(userid=userid):
                return next(
                    (item for item in self.players if item.userid == userid), None
                )
            case c.IsWhitelisted() | c.Whitelist():
                return True
            case c.Unwhitelist():
                return False
            case c.Runtime():
                return self.runtime
            case c.World():
                return self.world
            case (
                c.Activate()
                | c.Announce()
                | c.Reset()
                | c.Rollback()
                | c.Regenerate()
                | c.RollbackToSnapshot()
            ):
                return None
            case _:
                raise AssertionError(command)
        return None


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
    await instance.register(master)
    assert not prepare.await_count
    await instance.register(caves)
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
    with pytest.raises(RuntimeError, match="closed"):
        await instance.register(caves)
    assert calls.count("kill:Master") + calls.count("kill:Caves") == kills


@pytest.mark.parametrize("operation", ["start", "restart"])
async def test_cluster_start_updates_mods_after_stopping_all_games(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    instance, master, caves, prepare, calls = await controller(tmp_path, monkeypatch)
    calls.clear()

    def update(*_: object, **__: object) -> tuple[Shard, ...]:
        assert master.phase == caves.phase == "stopped"
        calls.append("prepare")
        return layout(tmp_path / "cluster")

    prepare.side_effect = update
    try:
        if operation == "start":
            await instance.stop()
            assert (await instance.status()).prepared_revision is None
            await instance.start()
        else:
            await instance.restart()
        assert prepare.await_count == 2
        assert calls == [
            "stop:Master",
            "stop:Caves",
            "prepare",
            "activate:Master",
            "activate:Caves",
            "start:Master",
            "start:Caves",
        ]
        assert (await instance.status()).phase == "running"
        await instance.start()
        assert prepare.await_count == 2
    finally:
        await instance.aclose()


async def test_manual_mod_update_is_reused_by_start_and_shard_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, _, prepare, calls = await controller(tmp_path, monkeypatch)
    try:
        await instance.stop()
        await instance.update_mods()
        await instance.start()
        await instance.shard("Caves").restart()
        assert calls[-1] == "restart:Caves"
        assert prepare.await_count == 2
        assert (await instance.status()).phase == "running"
    finally:
        await instance.aclose()


async def test_failed_mod_update_is_retried_before_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, _, prepare, _ = await controller(tmp_path, monkeypatch)
    try:
        await instance.stop()
        await instance.update_mods()
        prepare.side_effect = RuntimeError("mod update failed")
        with pytest.raises(RuntimeError, match="mod update failed"):
            await instance.update_mods()
        assert (await instance.status()).prepared_revision is None
        with pytest.raises(ControllerOperationError):
            await instance.start()
        assert prepare.await_count == 4
    finally:
        await instance.aclose()


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
        prepare.side_effect = RuntimeError("mod update failed")
    try:
        with pytest.raises(ControllerOperationError):
            await instance.restart()
        assert not any(call.startswith(("start:", "restart:")) for call in calls)
        assert prepare.await_count == (1 if stage == "stop" else 2)
        assert master.phase == caves.phase == "stopped"
        assert (await instance.status()).phase == "failed"
        caves.handlers.pop(c.Stop, None)
        prepare.side_effect = None
        previous_updates = prepare.await_count
        await instance.start()
        assert prepare.await_count == previous_updates + 1
    finally:
        caves.handlers.pop(c.Stop, None)
        await instance.aclose()


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
    master.phase = caves.phase = ShardPhase.RUNNING
    master.ready = caves.ready = True

    await instance.register(master)
    await instance.register(caves)
    await instance.wait_idle()
    try:
        assert master.phase == caves.phase == "running"
        assert calls == [
            "activate:Master",
            "activate:Caves",
            "start:Master",
            "start:Caves",
        ]
        prepare.assert_not_awaited()
        assert (await instance.status()).prepared_revision is None
        await instance.start()
        prepare.assert_not_awaited()
        assert not any(call.startswith(("stop:", "kill:")) for call in calls)

        await instance.stop()
        await instance.start()
        prepare.assert_awaited_once()
        assert (await instance.status()).prepared_revision is not None
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
        assert status.error == "cluster start failed"
        assert "secret" not in status.error

        caves.handlers.pop(c.Activate, None)
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
            await instance.register(caves)
        unknown = EndpointStub("Unknown", False, calls)
        with pytest.raises(KeyError, match="unknown shard"):
            await instance.register(unknown)

        stale = EndpointStub("Caves", False, calls)
        assert not await instance.unregister(stale)
        assert await instance.unregister(caves)
        assert calls[-1] == "stop:Master"
        assert "kill:Master" not in calls
        reconnected = EndpointStub(
            "Caves",
            False,
            calls,
            incarnation=caves.incarnation,
        )
        await instance.register(reconnected)
        await instance.wait_idle()
        assert master.phase == reconnected.phase == "running"

        assert not await instance.failed(caves)
        reconnected.phase = ShardPhase.FAILED
        assert await instance.failed(reconnected)
        assert (await instance.status()).phase == "failed"
        assert await instance.unregister(reconnected)

        replacement = EndpointStub("Caves", False, calls)
        await instance.register(replacement)
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


async def test_fail_close_kill_failure_is_reported_and_retryable(
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
        assert await instance.failed(caves)
        assert master.phase == "stopped"
    finally:
        master.handlers.pop(c.Stop, None)
        master.handlers.pop(c.Kill, None)
        await instance.aclose()


async def test_late_failure_cannot_fail_close_a_restarted_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, caves, _, calls = await controller(tmp_path, monkeypatch)
    caves.phase = ShardPhase.FAILED
    await instance.restart()
    stops = calls.count("stop:Master") + calls.count("stop:Caves")

    try:
        assert await instance.failed(caves)
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
        assert await instance.failed(caves)
        assert master.phase == caves.phase == "stopped"
    finally:
        caves.fail_status = False
        await instance.aclose()


async def test_disconnect_cleanup_failure_blocks_automatic_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, calls = await controller(tmp_path, monkeypatch)
    fail_kill = AsyncMock(side_effect=RuntimeError())
    master.handlers[c.Stop] = AsyncMock(side_effect=RuntimeError("stop failed"))
    master.handlers[c.Kill] = fail_kill
    try:
        with pytest.RaisesGroup(RuntimeError):
            await instance.unregister(caves)
        assert master.phase == "running"
        with pytest.raises(DisconnectedError):
            instance.agent("Caves")
        assert not await instance.unregister(caves)

        master.handlers.pop(c.Stop, None)
        master.handlers.pop(c.Kill, None)
        replacement = EndpointStub(
            "Caves",
            False,
            calls,
            incarnation=caves.incarnation,
        )
        await instance.register(replacement)
        await instance.wait_idle()
        assert master.phase == "running"
        assert replacement.phase == "stopped"

        await instance.kill()
        assert master.phase == replacement.phase == "stopped"
    finally:
        master.handlers.pop(c.Stop, None)
        master.handlers.pop(c.Kill, None)
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
        assert await instance.unregister(caves)
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
    await instance.register(master)
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


async def test_internal_relay_releases_delivered_batch(tmp_path: Path) -> None:
    root = tmp_path / "cluster"
    configuration().save(root)
    instance = ClusterController(ConfigurationStore(root))
    source, target = Broadcast[LogRecord](), Broadcast[LogRecord]()
    subscription = target.subscribe()
    relay = instance._start_relay("Master", source, target)
    await asyncio.sleep(0)
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


async def test_completed_reconcile_releases_failure_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cluster"
    configuration().save(root)
    instance = ClusterController(ConfigurationStore(root))
    references = []

    async def failed() -> None:
        await asyncio.sleep(0)
        record = LogRecord(
            shard="Master",
            game_attempt=ULID(),
            sequence=0,
            observed_timestamp_ns=0,
            line="x" * 1024 * 1024,
        )
        references.append(ref(record))
        msg = "prepare failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(instance, "_reconcile", failed)
    instance._schedule_reconcile()
    try:
        await instance.wait_idle()
        await asyncio.sleep(0)
        assert references
        assert references[0]() is None
    finally:
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
    instance, master, secondary, _, calls = await controller(tmp_path, monkeypatch)
    try:
        saved = await instance.save(timeout=9)
        assert saved.snapshot == 7
        assert tuple(name for name, _ in saved.shards) == ("Master", "Caves")
        assert master.requests.count(c.Save(timeout=9)) == 1
        assert c.Save(timeout=9) not in secondary.requests
        for endpoint in (master, secondary):
            assert (
                c.WaitSaved(cursor=endpoint.save_cursor, snapshot=7, timeout=9)
                in endpoint.requests
            )

        assert tuple(
            result.value
            for result in await instance.execute_all("return true", timeout=4)
        ) == ("Master:return true", "Caves:return true")
        caves = instance.shard("Caves")
        assert await caves.execute("return false", timeout=6) == "Caves:return false"
        assert await caves.save(timeout=8) == SavedEvent(path="session/7", snapshot=7)
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
            c.Reset(timeout=11),
            c.Rollback(count=2, timeout=12),
            c.Regenerate(expected_session_id="SESSION", require_empty=True, timeout=13),
        ):
            assert command in master.requests
            assert command not in secondary.requests
        assert calls.count("generation-marker:Master") == 3
        assert calls.count("generation-marker:Caves") == 3
    finally:
        await instance.aclose()


@pytest.mark.parametrize(
    ("command_type", "mutation", "confirmation"),
    [(c.ClusterSave, c.Save, c.WaitSaved), (c.Reset, c.Reset, c.WaitGeneration)],
)
async def test_cluster_completion_timeout_covers_master_and_peer_waits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_type: type[c.ClusterSave | c.Reset],
    mutation: type[c.Save | c.Reset],
    confirmation: type[c.WaitSaved | c.WaitGeneration],
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    completed = False

    async def command(_: c.Request[Any]) -> SavedEvent | None:
        await asyncio.sleep(0.04)
        return SavedEvent(path="session/7", snapshot=7) if mutation is c.Save else None

    async def wait(command: c.Request[Any]) -> object:
        nonlocal completed
        await asyncio.sleep(0.04)
        completed = True
        return await caves.dispatch(command)

    master.handlers[mutation] = command
    caves.handlers[confirmation] = wait
    try:
        with pytest.raises(IndeterminateError):
            await instance.invoke(command_type(timeout=0.06))
        assert not completed
    finally:
        await instance.aclose()


async def test_cluster_operation_defaults_allow_saving_and_reloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    try:
        await instance.execute_all("return true")
        await instance.save()
        await instance.reset()
        await instance.rollback()
        await instance.regenerate()
        assert c.Execute(source="return true", timeout=120) in master.requests
        assert c.Save(timeout=300) in master.requests
        assert (
            c.WaitSaved(cursor=caves.save_cursor, snapshot=7, timeout=300)
            in caves.requests
        )
        assert c.Reset(timeout=900) in master.requests
        assert c.Rollback(count=1, timeout=900) in master.requests
        assert c.Regenerate(timeout=900) in master.requests
    finally:
        await instance.aclose()


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
async def test_rollback_to_day_uses_earliest_complete_snapshot_and_verifies_reload(  # ruff: ignore[complex-structure]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    error: type[Exception] | None,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
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
                        clock=SnapshotClock(cycles=7 if number in {90, 91, 92} else 8)
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
        if scenario == "wrong_day":
            caves.world = caves.world.replace(day=9)
        if scenario == "wrong_session":
            caves.runtime = caves.runtime.replace(session_id="changed")
        if scenario == "wrong_snapshot_same_day":
            caves.runtime = caves.runtime.replace(snapshot=92)

    operation = AsyncMock(side_effect=restore)
    master.handlers[c.RollbackToSnapshot] = operation
    try:
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
            for endpoint in (master, caves):
                assert (
                    c.WaitGeneration(cursor=endpoint.generation_cursor, timeout=12)
                    in endpoint.requests
                )
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
    finally:
        await instance.aclose()


async def test_save_and_reload_disconnects_are_stage_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, caves, _, _ = await controller(tmp_path, monkeypatch)
    try:
        master.handlers[c.GenerationMarker] = AsyncMock(side_effect=DisconnectedError())
        with pytest.raises(ControllerOperationError):
            await instance.reset()
        master.handlers.pop(c.GenerationMarker)
        caves.handlers[c.WaitGeneration] = AsyncMock(side_effect=DisconnectedError())
        with pytest.raises(IndeterminateError):
            await instance.reset()

        master.handlers[c.SaveMarker] = AsyncMock(side_effect=DisconnectedError())
        with pytest.raises(ControllerOperationError):
            await instance.save()
        master.handlers.pop(c.SaveMarker)
        caves.handlers[c.WaitSaved] = AsyncMock(
            side_effect=RuntimeError("confirmation failed")
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
    caves.handlers[c.Pause] = AsyncMock(side_effect=RuntimeError("pause secret"))
    try:
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
    finally:
        await instance.aclose()


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
        caves.handlers[c.Start] = AsyncMock(side_effect=TimeoutError())
        with pytest.raises(IndeterminateError):
            await instance.restart()
    finally:
        master.handlers.pop(c.Stop, None)
        master.handlers.pop(c.Kill, None)
        await instance.aclose()


async def test_shard_restart_has_a_separate_stop_and_start_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, caves, _, _ = await controller(tmp_path, monkeypatch)

    async def delayed_restart(command: c.Restart) -> None:
        await asyncio.sleep(0.02)
        await caves.dispatch(command)

    monkeypatch.setattr(controller_module, "AGENT_START_TIMEOUT", 0.001)
    monkeypatch.setattr(controller_module, "AGENT_RESTART_TIMEOUT", 0.1)
    caves.handlers[c.Restart] = delayed_restart
    try:
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
    master.phase, master.ready, master.pid = ShardPhase.FAILED, False, 123
    caves.phase, caves.ready, caves.pid = ShardPhase.STOPPED, False, None
    try:
        with pytest.raises(GamesRunningError):
            await instance.save_configuration(current.revision, current.configuration)
        with pytest.raises(RuntimeError, match="must be stopped"):
            await instance.update_mods()
    finally:
        await instance.aclose()


async def test_start_rejects_configuration_drift_while_games_are_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _, _, prepare, calls = await controller(tmp_path, monkeypatch)
    calls.clear()
    try:
        current = await instance.read_configuration()
        assert isinstance(current, ConfigurationSnapshot)
        current.configuration.replace(
            settings=current.configuration.settings.replace(max_players=12)
        ).save(tmp_path / "cluster")

        with pytest.raises(ControllerOperationError):
            await instance.start()
        assert prepare.await_count == 1
        assert not any(call.startswith(("activate:", "start:")) for call in calls)

        await instance.start()
        assert prepare.await_count == 2
        assert (await instance.status()).phase == "running"
    finally:
        await instance.aclose()


async def test_start_rejects_external_deployment_port_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, master, _, prepare, _ = await controller(tmp_path, monkeypatch)
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

        master.handlers[c.Stop] = AsyncMock(side_effect=RuntimeError("stop failed"))
        fail_kill = AsyncMock(side_effect=RuntimeError())
        master.handlers[c.Kill] = fail_kill
        with pytest.raises(TopologyChangeError):
            await instance.start()

        fail_kill.assert_awaited_once()
        timeout_kill = AsyncMock(side_effect=TimeoutError())
        master.handlers[c.Kill] = timeout_kill
        with pytest.raises(IndeterminateError):
            await instance.start()

        assert prepare.await_count == 1
        assert (await instance.status()).error == "cluster start failed"
    finally:
        master.handlers.pop(c.Stop, None)
        master.handlers.pop(c.Kill, None)
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
    await instance.register(master)
    await instance.register(caves)
    await instance.wait_idle()
    try:
        invalid = await instance.read_configuration()
        assert isinstance(invalid, InvalidConfiguration)
        status = await instance.status()
        assert status.revision is not None
        assert status.revision == invalid.revision
        with pytest.raises(InvalidConfigurationError):
            await instance.start()

        saved = await instance.save_configuration(status.revision, desired)
        assert saved.configuration.shards["Caves"].settings.server_port == 11000
        await instance.start()
        assert master.phase == caves.phase == "running"
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
        await cleaning.wait()
        assert not gathering.done()
        release.set()
        with pytest.RaisesGroup(pytest.RaisesExc(RuntimeError, match="peer failed")):
            await gathering
    finally:
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
    await master.stop_entered.wait()
    other = asyncio.create_task(instance.aclose())
    try:
        closing.cancel("first cancellation")
        await asyncio.sleep(0)
        closing.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not closing.done()
        assert not other.done()
        master.stop_release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await closing
        assert caught.value.args == ("first cancellation",)
        await other
        assert master.phase == caves.phase == ShardPhase.STOPPED
        assert calls == ["stop:Master", "stop:Caves"]
    finally:
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
    await entered.wait()
    try:
        closing.cancel("caller cancelled")
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await closing
        assert caught.value.args == ("caller cancelled",)
        assert isinstance(caught.value.__cause__, ExceptionGroup)
        master.handlers.clear()
        await instance.aclose()
        assert master.phase == caves.phase == ShardPhase.STOPPED
    finally:
        release.set()
        master.handlers.clear()
        await asyncio.gather(closing, return_exceptions=True)
        await instance.aclose()
