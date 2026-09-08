# ruff: file-ignore[async-function-with-timeout]
from abc import ABC, abstractmethod

from pydantic import JsonValue
from ulid import ULID

from dst_server import commands as c
from dst_server.configuration.models import ClusterConfig
from dst_server.events.server import SavedEvent
from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World
from dst_server.models.cluster import (
    ClusterSaveResult,
    ClusterStatus,
    ConfigurationRead,
    ConfigurationSnapshot,
    LocatedPlayer,
    ShardResult,
    ShardRuntimeStatus,
)
from dst_server.models.driver import DriverHealth
from dst_server.models.snapshot import Snapshot, SnapshotCatalog
from dst_server.timeouts import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_RELOAD_TIMEOUT,
    DEFAULT_SAVE_TIMEOUT,
)


class EndpointAPI(ABC):
    @abstractmethod
    async def invoke[T](self, command: c.Request[T]) -> T:
        raise NotImplementedError


class _LifecycleAPI(EndpointAPI):
    async def start(self) -> None:
        return await self.invoke(c.Start())

    async def stop(self) -> None:
        return await self.invoke(c.Stop())

    async def restart(self) -> None:
        return await self.invoke(c.Restart())

    async def kill(self) -> None:
        return await self.invoke(c.Kill())

    async def list_snapshots(
        self, *, limit: int = 100, before: int | None = None
    ) -> SnapshotCatalog:
        return await self.invoke(c.Snapshots(limit=limit, before=before))


class ShardAPI(_LifecycleAPI):
    @property
    def players(self) -> PlayerAPI:
        return PlayerAPI(self)

    async def status(self) -> ShardRuntimeStatus:
        return await self.invoke(c.Status())

    async def execute(
        self, source: str, *, timeout: float = DEFAULT_COMMAND_TIMEOUT
    ) -> str:
        return await self.invoke(c.Execute(source=source, timeout=timeout))

    async def execute_json(
        self, source: str, *, timeout: float = DEFAULT_COMMAND_TIMEOUT
    ) -> JsonValue:
        return await self.invoke(c.ExecuteJson(source=source, timeout=timeout))

    async def save(self, *, timeout: float = DEFAULT_SAVE_TIMEOUT) -> SavedEvent:
        return await self.invoke(c.Save(timeout=timeout))

    async def health(self) -> DriverHealth:
        return await self.invoke(c.Health())

    async def room(self) -> Room:
        return await self.invoke(c.Room())

    async def world(self) -> World:
        return await self.invoke(c.World())

    async def runtime(self) -> Runtime:
        return await self.invoke(c.Runtime())

    async def mods(self) -> tuple[Mod, ...]:
        return await self.invoke(c.Mods())

    async def connected_shards(self) -> tuple[ShardStatus, ...]:
        return await self.invoke(c.ConnectedShards())

    async def pause(self, paused: bool) -> bool:
        return await self.invoke(c.Pause(paused=paused))

    async def regenerate_shard(
        self, *, preserve_settings: bool = True, timeout: float = DEFAULT_RELOAD_TIMEOUT
    ) -> None:
        return await self.invoke(
            c.RegenerateShard(preserve_settings=preserve_settings, timeout=timeout)
        )


class ClusterAPI(_LifecycleAPI):
    async def status(self) -> ClusterStatus:
        return await self.invoke(c.ClusterStatusQuery())

    async def update_mods(self) -> None:
        return await self.invoke(c.UpdateMods())

    async def read_configuration(self) -> ConfigurationRead:
        return await self.invoke(c.ReadConfiguration())

    async def save_configuration(
        self, expected_revision: ULID, configuration: ClusterConfig
    ) -> ConfigurationSnapshot:
        return await self.invoke(
            c.SaveConfiguration(
                expected_revision=expected_revision, configuration=configuration
            )
        )

    async def execute_all(
        self, source: str, *, timeout: float = DEFAULT_COMMAND_TIMEOUT
    ) -> tuple[ShardResult[str], ...]:
        return await self.invoke(c.ExecuteAll(source=source, timeout=timeout))

    async def save(self, *, timeout: float = DEFAULT_SAVE_TIMEOUT) -> ClusterSaveResult:
        return await self.invoke(c.ClusterSave(timeout=timeout))

    async def pause(self, paused: bool) -> tuple[ShardResult[bool], ...]:
        return await self.invoke(c.ClusterPause(paused=paused))

    async def rollback_to_day(
        self, day: int, *, timeout: float = DEFAULT_RELOAD_TIMEOUT
    ) -> Snapshot:
        return await self.invoke(c.RollbackToDay(day=day, timeout=timeout))

    async def list_players(self) -> tuple[LocatedPlayer, ...]:
        return await self.invoke(c.LocatePlayers())

    async def get_player(self, userid: str) -> LocatedPlayer | None:
        return await self.invoke(c.LocatePlayer(userid=userid))

    async def announce(self, message: str) -> None:
        return await self.invoke(c.Announce(message=message))

    async def reset(self, *, timeout: float = DEFAULT_RELOAD_TIMEOUT) -> None:
        return await self.invoke(c.Reset(timeout=timeout))

    async def rollback(
        self, count: int = 1, *, timeout: float = DEFAULT_RELOAD_TIMEOUT
    ) -> None:
        return await self.invoke(c.Rollback(count=count, timeout=timeout))

    async def regenerate(self, *, timeout: float = DEFAULT_RELOAD_TIMEOUT) -> None:
        return await self.invoke(c.Regenerate(timeout=timeout))

    async def is_whitelisted(self, userid: str) -> bool:
        return await self.invoke(c.IsWhitelisted(userid=userid))

    async def whitelist(self, userid: str) -> bool:
        return await self.invoke(c.Whitelist(userid=userid))

    async def unwhitelist(self, userid: str) -> bool:
        return await self.invoke(c.Unwhitelist(userid=userid))


class PlayerAPI:
    def __init__(self, endpoint: EndpointAPI) -> None:
        self._endpoint = endpoint

    async def list(self) -> tuple[Player, ...]:
        return await self._endpoint.invoke(c.ListPlayers())

    async def get(self, userid: str) -> Player | None:
        return await self._endpoint.invoke(c.GetPlayer(userid=userid))

    async def inventory(self, userid: str) -> Inventory | None:
        return await self._endpoint.invoke(c.Inventory(userid=userid))

    async def kick(self, userid: str) -> None:
        return await self._endpoint.invoke(c.Kick(userid=userid))

    async def ban(self, userid: str, *, seconds: int | None = None) -> None:
        return await self._endpoint.invoke(c.Ban(userid=userid, seconds=seconds))

    async def blocklist(self) -> tuple[str, ...]:
        return await self._endpoint.invoke(c.Blocklist())

    async def is_blocked(self, userid: str) -> bool:
        return await self._endpoint.invoke(c.IsBlocked(userid=userid))

    async def unban(self, userid: str) -> bool:
        return await self._endpoint.invoke(c.Unban(userid=userid))

    async def is_admin(self, userid: str) -> bool | None:
        return await self._endpoint.invoke(c.IsAdmin(userid=userid))

    async def set_vitals(
        self,
        userid: str,
        *,
        health: float | None = None,
        hunger: float | None = None,
        sanity: float | None = None,
        temperature: float | None = None,
        moisture: float | None = None,
    ) -> bool:
        return await self._endpoint.invoke(
            c.SetVitals(
                userid=userid,
                health=health,
                hunger=hunger,
                sanity=sanity,
                temperature=temperature,
                moisture=moisture,
            )
        )

    async def kill(self, userid: str) -> bool:
        return await self._endpoint.invoke(c.KillPlayer(userid=userid))

    async def revive(self, userid: str) -> bool:
        return await self._endpoint.invoke(c.Revive(userid=userid))

    async def despawn(self, userid: str) -> bool:
        return await self._endpoint.invoke(c.Despawn(userid=userid))

    async def migrate(self, userid: str, shard_id: str, *, portal_id: int = 1) -> bool:
        return await self._endpoint.invoke(
            c.Migrate(userid=userid, shard_id=shard_id, portal_id=portal_id)
        )

    async def teleport(self, userid: str, *, x: float, y: float, z: float) -> bool:
        return await self._endpoint.invoke(c.Teleport(userid=userid, x=x, y=y, z=z))

    async def give(self, userid: str, item: str, count: int = 1) -> int:
        return await self._endpoint.invoke(
            c.Give(userid=userid, item=item, count=count)
        )

    async def remove(self, userid: str, item: str, count: int = 1) -> int:
        return await self._endpoint.invoke(
            c.Remove(userid=userid, item=item, count=count)
        )
