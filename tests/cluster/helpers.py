import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from ulid import ULID

from dst_server import commands as c
from dst_server.cluster import service
from dst_server.cluster.controller import (
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
from dst_server.events.server import SavedEvent
from dst_server.models import Player, PlayerState, Runtime, World
from dst_server.models.cluster import (
    GameEventRecord,
    LifecycleRecord,
    LogRecord,
    ObservationCursor,
    ShardDesired,
    ShardPhase,
    ShardRuntimeStatus,
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


def layout(_root: Path) -> tuple[Shard, ...]:
    return (
        Shard("Master", True),
        Shard("Caves", False),
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
        self.outdated_mods: tuple[str, ...] = ()
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
            game_attempt=self.attempt if self.pid is not None else None,
            outdated_mods=self.outdated_mods if self.pid is not None else (),
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
                if self.phase is not ShardPhase.RUNNING or isinstance(
                    command, c.Restart
                ):
                    self.attempt = ULID()
                    self.outdated_mods = ()
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


type Room = tuple[ClusterController, EndpointStub, EndpointStub, AsyncMock, list[str]]


async def controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Room:
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


@asynccontextmanager
async def managed_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Room]:
    result = await controller(tmp_path, monkeypatch)
    try:
        yield result
    finally:
        await result[0].aclose()
