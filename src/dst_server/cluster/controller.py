# ruff: file-ignore[private-member-access, too-many-public-methods]
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from logbook import Logger
from pydantic import JsonValue
from ulid import ULID

from dst_server.events.server import SavedEvent
from dst_server.game.rpc import DriverHealth
from dst_server.game.validation import item_count, positive_timeout
from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World
from dst_server.rpc.errors import DisconnectedError, IndeterminateError
from dst_server.rpc.models import (
    ClusterPhase,
    ClusterStatus,
    GameEventRecord,
    LifecycleRecord,
    LocatedPlayer,
    LogRecord,
    ShardRuntimeStatus,
)
from dst_server.rpc.models import ShardDesired as RpcShardDesired
from dst_server.runtime import IndeterminateCommandError

from . import service
from .config import ClusterConfig
from .configuration import (
    ConfigurationRead,
    ConfigurationSnapshot,
    ConfigurationStore,
    ConfigurationStoreError,
    InvalidConfiguration,
    InvalidConfigurationError,
)
from .subscriptions import Broadcast, Subscription, SubscriptionOverflowError

logger = Logger(__name__)
AGENT_CALL_TIMEOUT = 35.0
AGENT_STATUS_TIMEOUT = 10.0
AGENT_START_TIMEOUT = 1515.0
AGENT_STOP_TIMEOUT = 32.0
AGENT_KILL_TIMEOUT = 5.0
AGENT_RESTART_TIMEOUT = AGENT_START_TIMEOUT + AGENT_STOP_TIMEOUT
CONTROLLER_CANCEL_TIMEOUT = 1.0
PREPARE_ATTEMPTS = 3


class AgentEndpoint(Protocol):
    name: str
    master: bool
    incarnation: str
    logs: Broadcast[LogRecord]
    lifecycle: Broadcast[LifecycleRecord]
    game_events: Broadcast[GameEventRecord]

    async def runtime_status(self) -> ShardRuntimeStatus: ...

    async def activate(self) -> None: ...

    async def start(self) -> object: ...

    async def stop(self) -> object: ...

    async def restart(self) -> object: ...

    async def kill(self) -> object: ...

    async def execute(self, source: str, completion_timeout: float) -> str: ...

    async def execute_json(self, source: str) -> JsonValue: ...

    async def health(self) -> DriverHealth: ...

    async def room(self) -> Room: ...

    async def world(self) -> World: ...

    async def runtime(self) -> Runtime: ...

    async def mods(self) -> tuple[Mod, ...]: ...

    async def connected_shards(self) -> tuple[ShardStatus, ...]: ...

    async def pause(self, paused: bool) -> bool: ...

    async def regenerate_shard(
        self,
        *,
        preserve_settings: bool,
        completion_timeout: float,
    ) -> None: ...

    async def list_players(self) -> tuple[Player, ...]: ...

    async def get_player(self, userid: str) -> Player | None: ...

    async def inventory(self, userid: str) -> Inventory | None: ...

    async def kick(self, userid: str) -> None: ...

    async def ban(self, userid: str, *, seconds: int | None) -> None: ...

    async def blocklist(self) -> tuple[str, ...]: ...

    async def is_blocked(self, userid: str) -> bool: ...

    async def unban(self, userid: str) -> bool: ...

    async def is_admin(self, userid: str) -> bool | None: ...

    async def set_vitals(
        self,
        userid: str,
        *,
        health: float | None,
        hunger: float | None,
        sanity: float | None,
        temperature: float | None,
        moisture: float | None,
    ) -> bool: ...

    async def kill_player(self, userid: str) -> bool: ...

    async def revive(self, userid: str) -> bool: ...

    async def despawn(self, userid: str) -> bool: ...

    async def migrate(
        self,
        userid: str,
        shard_id: str,
        portal_id: int,
    ) -> bool: ...

    async def teleport(
        self,
        userid: str,
        x: float,
        y: float,
        z: float,
    ) -> bool: ...

    async def give(self, userid: str, item: str, count: int) -> int: ...

    async def remove(self, userid: str, item: str, count: int) -> int: ...

    async def save(self, completion_timeout: float) -> SavedEvent: ...

    async def save_marker(self) -> int: ...

    async def wait_saved(
        self,
        after_sequence: int,
        snapshot: int | None,
        completion_timeout: float,
    ) -> SavedEvent: ...

    async def generation_marker(self) -> int: ...

    async def wait_generation(
        self,
        after_sequence: int,
        completion_timeout: float,
    ) -> int: ...

    async def announce(self, message: str) -> None: ...

    async def reset(self, completion_timeout: float) -> None: ...

    async def rollback(self, count: int, completion_timeout: float) -> None: ...

    async def regenerate(self, completion_timeout: float) -> None: ...

    async def is_whitelisted(self, userid: str) -> bool: ...

    async def whitelist(self, userid: str) -> bool: ...

    async def unwhitelist(self, userid: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ShardResult[T]:
    shard: str
    value: T | None = None
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class ClusterSaveResult:
    snapshot: int | None
    shards: tuple[tuple[str, SavedEvent], ...]


class ControllerOperationError(RuntimeError):
    def __init__(
        self,
        error_id: ULID | None = None,
    ) -> None:
        self.error_id = error_id or ULID()
        super().__init__("cluster operation failed")


class IncompleteRosterError(RuntimeError):
    def __init__(self, missing: tuple[str, ...]) -> None:
        super().__init__(f"missing shard agents: {', '.join(missing)}")


class PlayerLocationConflictError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("player is active on more than one shard")


type _Operation[T] = Callable[[AgentEndpoint], Awaitable[T]]


def _leaf_errors(error: BaseException) -> Iterator[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            yield from _leaf_errors(nested)
    else:
        yield error


def _indeterminate(
    error: BaseException,
) -> IndeterminateError | IndeterminateCommandError | None:
    for nested in _leaf_errors(error):
        if isinstance(nested, TimeoutError):
            return IndeterminateError()
        if isinstance(nested, IndeterminateError | IndeterminateCommandError):
            return nested
    return None


def _configuration_error(error: BaseException) -> ConfigurationStoreError | None:
    return next(
        (
            nested
            for nested in _leaf_errors(error)
            if isinstance(nested, ConfigurationStoreError)
        ),
        None,
    )


class ClusterController:
    def __init__(
        self,
        configuration: ConfigurationStore,
        *,
        install_path: Path = service.DEFAULT_INSTALL_PATH,
    ) -> None:
        shards = configuration.shards
        names = tuple(shard.name for shard in shards)
        masters = tuple(shard.name for shard in shards if shard.master)
        if not names or len(names) != len(set(names)) or len(masters) != 1:
            msg = "controller requires unique shards and exactly one master"
            raise ValueError(msg)
        self.epoch = ULID()
        self.install_path = install_path
        self.cluster_path = configuration.directory
        self._layout = {shard.name: shard for shard in shards}
        self._names = names
        self.master = masters[0]
        self._configuration = configuration
        self._agents: dict[str, AgentEndpoint] = {}
        self._last_status: dict[str, ShardRuntimeStatus] = {}
        self._desired: dict[str, RpcShardDesired] = dict.fromkeys(names, "running")
        self._blocked: dict[str, str] = {}
        self._prepared_revision: str | None = None
        self._phase: ClusterPhase | None = None
        self._error_id: ULID | None = None
        self._error: str | None = None
        self._ever_complete = False
        self._lock = asyncio.Lock()
        self._lock_owner: asyncio.Task[Any] | None = None
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._shutdown_complete = False
        self._reconcile_task: asyncio.Task[None] | None = None
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[LifecycleRecord]()
        self.game_events = Broadcast[GameEventRecord]()
        self._shard_logs = {name: Broadcast[LogRecord]() for name in names}
        self._shard_lifecycle = {name: Broadcast[LifecycleRecord]() for name in names}
        self._shard_events = {name: Broadcast[GameEventRecord]() for name in names}
        self._relays: dict[str, tuple[asyncio.Task[None], ...]] = {}
        self._shards = {name: ShardController(self, name) for name in names}

    def shard(self, name: str) -> ShardController:
        self._require_open()
        try:
            return self._shards[name]
        except KeyError:
            message = f"unknown shard: {name}"
            raise KeyError(message) from None

    def agent(self, name: str) -> AgentEndpoint:
        self._require_open()
        if name not in self._layout:
            message = f"unknown shard: {name}"
            raise KeyError(message)
        try:
            return self._agents[name]
        except KeyError:
            message = f"shard agent is unavailable: {name}"
            raise DisconnectedError(message) from None

    async def register(self, endpoint: AgentEndpoint) -> None:
        async with self._serialized():
            self._require_open()
            status = await self._validate_endpoint(endpoint)
            self._require_open()
            if endpoint.name in self._agents:
                msg = f"shard agent is already registered: {endpoint.name}"
                raise RuntimeError(msg)
            relays = tuple(
                self._start_relay(endpoint.name, source, aggregate, shard)
                for source, aggregate, shard in (
                    (endpoint.logs, self.logs, self._shard_logs[endpoint.name]),
                    (
                        endpoint.lifecycle,
                        self.lifecycle,
                        self._shard_lifecycle[endpoint.name],
                    ),
                    (
                        endpoint.game_events,
                        self.game_events,
                        self._shard_events[endpoint.name],
                    ),
                )
            )
            self._agents[endpoint.name] = endpoint
            self._last_status[endpoint.name] = status
            self._relays[endpoint.name] = relays
            previous = self._blocked.get(endpoint.name)
            if previous is not None and previous != endpoint.incarnation:
                self._blocked.pop(endpoint.name)
            if self._complete:
                self._ever_complete = True
                if not self._blocked:
                    self._clear_error()
                    self._schedule_reconcile()

    async def unregister(self, endpoint: AgentEndpoint) -> bool:
        async with self._serialized():
            if self._closed:
                return False
            if self._agents.get(endpoint.name) is not endpoint:
                return False
            self._agents.pop(endpoint.name)
            await self._cancel_relays(endpoint.name)
            peers = tuple(self._agents.values())
            cleanup = await self._fail_close(
                "shard agent disconnected",
                agents=peers,
            )
            if cleanup is not None:
                self._blocked.update((agent.name, agent.incarnation) for agent in peers)
                raise cleanup
            return True

    async def failed(self, endpoint: AgentEndpoint) -> bool:
        async with self._serialized():
            if self._closed:
                return False
            if self._agents.get(endpoint.name) is not endpoint:
                return False
            if endpoint.name not in self._blocked and (
                await self._endpoint_status(endpoint)
            ).phase not in {"failed", "unavailable"}:
                return True
            self._blocked[endpoint.name] = endpoint.incarnation
            cleanup = await self._fail_close("shard retry budget exhausted")
            if cleanup is not None:
                raise cleanup
            return True

    async def status(self) -> ClusterStatus:
        self._require_open()
        read = await self._configuration.read()
        revision = ULID.from_str(read.revision)
        missing = self._missing
        agents = tuple(
            self._agents[name] for name in self._names if name in self._agents
        )
        statuses = tuple(
            await asyncio.gather(*(self._endpoint_status(agent) for agent in agents))
        )
        error_id, error = self._error_id, self._error
        if isinstance(read, InvalidConfiguration):
            error_id = error_id or ULID()
            error = error or "cluster configuration is invalid"
        return ClusterStatus(
            epoch=self.epoch,
            phase=self._cluster_phase(statuses, missing, error_id),
            revision=revision,
            prepared_revision=(
                ULID.from_str(self._prepared_revision)
                if self._prepared_revision is not None
                else None
            ),
            master=self.master,
            missing_shards=missing,
            shards=statuses,
            error_id=error_id,
            error=error,
        )

    async def start(self) -> None:
        async with self._public_operation():
            self._desired = dict.fromkeys(self._names, "running")
            self._blocked.clear()
            self._require_complete()
            self._clear_error()
            await self._start_desired()

    async def stop(self) -> None:
        async with self._public_operation():
            self._desired = dict.fromkeys(self._names, "stopped")
            await self._stop_registered(force=False)

    async def restart(self) -> None:
        async with self._public_operation():
            self._desired = dict.fromkeys(self._names, "running")
            self._blocked.clear()
            self._require_complete()
            self._clear_error()
            try:
                await self._prepare()
                await self._lifecycle_operation(
                    "starting",
                    lambda agent: agent.restart(),
                    limit=AGENT_RESTART_TIMEOUT,
                )
            except Exception as error:
                if self._closed:
                    raise
                cleanup = await self._fail_close("cluster restart failed", error)
                if cleanup is not None:
                    error = BaseExceptionGroup(
                        "cluster restart and cleanup failed",
                        (error, cleanup),
                    )
                raise self._operation_error(error, self._error_id) from None

    async def kill(self) -> None:
        async with self._public_operation():
            self._desired = dict.fromkeys(self._names, "stopped")
            await self._stop_registered(force=True)

    async def update_mods(self) -> None:
        async with self._public_operation():
            self._require_complete()
            if not await self._all_stopped():
                msg = "all game processes must be stopped"
                raise RuntimeError(msg)
            await self._prepare(force=True)

    async def read_configuration(self) -> ConfigurationRead:
        self._require_open()
        return await self._configuration.read()

    async def save_configuration(
        self,
        expected_revision: str | ULID,
        desired: ClusterConfig,
    ) -> ConfigurationSnapshot:
        async with self._public_operation():
            saved = await self._configuration.save(
                str(expected_revision),
                desired,
                all_stopped=self._complete and await self._all_stopped(),
            )
            self._prepared_revision = None
            return saved

    async def execute_all(
        self,
        source: str,
        completion_timeout: float = 30,
    ) -> tuple[ShardResult[str], ...]:
        completion_timeout = positive_timeout(completion_timeout)
        return await self._shard_results(
            lambda agent: agent.execute(source, completion_timeout),
            limit=completion_timeout + 5,
        )

    async def announce(self, message: str) -> None:
        await self._require_ready()
        await self._agent_call(lambda: self.agent(self.master).announce(message))

    async def save(self, completion_timeout: float = 30) -> ClusterSaveResult:
        completion_timeout = positive_timeout(completion_timeout)
        async with self._public_operation():
            await self._require_ready()
            agents = self._ordered_agents
            mutation_completed = False
            try:
                markers = await self._gather(
                    agents,
                    lambda agent: agent.save_marker(),
                )
                master_event = await self._agent_call(
                    lambda: self.agent(self.master).save(completion_timeout),
                    limit=completion_timeout + 5,
                )
                mutation_completed = True
                events = await self._gather(
                    agents,
                    lambda agent: agent.wait_saved(
                        markers[agent.name],
                        master_event.snapshot,
                        completion_timeout,
                    ),
                    limit=completion_timeout + 5,
                )
            except Exception as error:
                raise self._operation_error(
                    error,
                    mutation_completed=mutation_completed,
                ) from None
            return ClusterSaveResult(
                master_event.snapshot,
                tuple((name, events[name]) for name in self._names),
            )

    async def pause(self, paused: bool) -> tuple[ShardResult[bool], ...]:
        return await self._shard_results(lambda agent: agent.pause(paused))

    async def reset(self, completion_timeout: float = 30) -> None:
        completion_timeout = positive_timeout(completion_timeout)
        await self._reload(
            lambda master: master.reset(completion_timeout),
            completion_timeout,
        )

    async def rollback(self, count: int = 1, completion_timeout: float = 30) -> None:
        count = item_count(count, allow_zero=True)
        completion_timeout = positive_timeout(completion_timeout)
        await self._reload(
            lambda master: master.rollback(count, completion_timeout),
            completion_timeout,
        )

    async def regenerate(self, completion_timeout: float = 30) -> None:
        completion_timeout = positive_timeout(completion_timeout)
        await self._reload(
            lambda master: master.regenerate(completion_timeout),
            completion_timeout,
        )

    async def list_players(self) -> tuple[LocatedPlayer, ...]:
        await self._require_ready()
        players = await self._gather(
            self._ordered_agents,
            lambda agent: agent.list_players(),
        )
        grouped: dict[str, list[tuple[str, Player]]] = {}
        for name in self._names:
            for player in players[name]:
                grouped.setdefault(player.userid, []).append((name, player))
        return tuple(self._locate(grouped[userid]) for userid in sorted(grouped))

    async def get_player(self, userid: str) -> LocatedPlayer | None:
        await self._require_ready()
        players = await self._gather(
            self._ordered_agents,
            lambda agent: agent.get_player(userid),
        )
        found = [
            (name, player)
            for name in self._names
            if (player := players[name]) is not None
        ]
        return self._locate(found) if found else None

    async def is_whitelisted(self, userid: str) -> bool:
        await self._require_ready()
        return await self._agent_call(
            lambda: self.agent(self.master).is_whitelisted(userid)
        )

    async def whitelist(self, userid: str) -> bool:
        await self._require_ready()
        return await self._agent_call(lambda: self.agent(self.master).whitelist(userid))

    async def unwhitelist(self, userid: str) -> bool:
        await self._require_ready()
        return await self._agent_call(
            lambda: self.agent(self.master).unwhitelist(userid)
        )

    def subscribe_logs(self) -> Subscription[LogRecord]:
        return self._subscribe(self.logs)

    def subscribe_lifecycle(self) -> Subscription[LifecycleRecord]:
        return self._subscribe(self.lifecycle)

    def subscribe_events(self) -> Subscription[GameEventRecord]:
        return self._subscribe(self.game_events)

    def _subscribe[T](self, source: Broadcast[T]) -> Subscription[T]:
        self._require_open()
        return source.subscribe()

    async def wait_idle(self) -> None:
        task = self._reconcile_task
        if task is not None:
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._shutdown_complete:
                return
            self._closed = True
            current = asyncio.current_task()
            tasks = {
                task
                for task in (self._reconcile_task, self._lock_owner)
                if task is not None and task is not current and not task.done()
            }
            for task in tasks:
                task.cancel()
            if tasks:
                done, _ = await asyncio.wait(
                    tasks,
                    timeout=CONTROLLER_CANCEL_TIMEOUT,
                )
                await asyncio.gather(*done, return_exceptions=True)
            agents = tuple(self._agents.values())
            forced, failures = await self._terminate_agents(agents)
            if forced:
                self._record_error("cluster shutdown required forced termination")
            if failures:
                error = BaseExceptionGroup("cluster shutdown failed", failures)
                self._record_error("cluster shutdown failed", error)
                raise error
            for name in tuple(self._relays):
                await self._cancel_relays(name)
            broadcasts = (
                self.logs,
                self.lifecycle,
                self.game_events,
                *self._shard_logs.values(),
                *self._shard_lifecycle.values(),
                *self._shard_events.values(),
            )
            for broadcast in broadcasts:
                broadcast.close()
            self._shutdown_complete = True

    @property
    def _complete(self) -> bool:
        return len(self._agents) == len(self._names)

    @property
    def _missing(self) -> tuple[str, ...]:
        return tuple(name for name in self._names if name not in self._agents)

    @property
    def _ordered_agents(self) -> tuple[AgentEndpoint, ...]:
        self._require_complete()
        return tuple(self._agents[name] for name in self._names)

    @staticmethod
    async def _agent_call[T](
        operation: Callable[[], Awaitable[T]],
        *,
        limit: float = AGENT_CALL_TIMEOUT,
    ) -> T:
        async with asyncio.timeout(limit):
            return await operation()

    async def _terminate_agents(
        self,
        agents: tuple[AgentEndpoint, ...],
    ) -> tuple[bool, tuple[BaseException, ...]]:
        stopped = await asyncio.gather(
            *(
                self._agent_call(agent.stop, limit=AGENT_STOP_TIMEOUT)
                for agent in agents
            ),
            return_exceptions=True,
        )
        failed = tuple(
            agent
            for agent, result in zip(agents, stopped, strict=True)
            if isinstance(result, BaseException)
        )
        if not failed:
            return False, ()
        return True, await self._kill_agents(failed)

    async def _kill_agents(
        self,
        agents: tuple[AgentEndpoint, ...],
    ) -> tuple[BaseException, ...]:
        killed = await asyncio.gather(
            *(
                self._agent_call(agent.kill, limit=AGENT_KILL_TIMEOUT)
                for agent in agents
            ),
            return_exceptions=True,
        )
        return tuple(result for result in killed if isinstance(result, BaseException))

    async def _all_stopped(self) -> bool:
        statuses = await asyncio.gather(
            *(self._endpoint_status(agent) for agent in self._agents.values())
        )
        return all(
            status.phase in {"stopped", "failed"} and status.pid is None
            for status in statuses
        )

    async def _validate_endpoint(
        self,
        endpoint: AgentEndpoint,
    ) -> ShardRuntimeStatus:
        expected = self._layout.get(endpoint.name)
        if expected is None:
            message = f"unknown shard: {endpoint.name}"
            raise KeyError(message)
        try:
            incarnation = str(ULID.from_str(endpoint.incarnation))
        except TypeError, ValueError:
            msg = "agent incarnation must be a ULID"
            raise ValueError(msg) from None
        status = await self._agent_call(
            endpoint.runtime_status,
            limit=AGENT_STATUS_TIMEOUT,
        )
        if (
            endpoint.master != expected.master
            or incarnation != endpoint.incarnation
            or status.name != endpoint.name
            or status.is_master != endpoint.master
            or status.agent_incarnation != ULID.from_str(endpoint.incarnation)
        ):
            msg = f"agent identity does not match shard topology: {endpoint.name}"
            raise ValueError(msg)
        return status

    def _require_open(self) -> None:
        if self._closed:
            msg = "cluster controller is closed"
            raise RuntimeError(msg)

    @asynccontextmanager
    async def _public_operation(self) -> AsyncIterator[None]:
        self._require_open()
        async with self._serialized():
            self._require_open()
            yield

    @asynccontextmanager
    async def _serialized(self) -> AsyncIterator[None]:
        async with self._lock:
            owner = asyncio.current_task()
            self._lock_owner = owner
            try:
                yield
            finally:
                if self._lock_owner is owner:
                    self._lock_owner = None

    def _require_complete(self) -> None:
        self._require_open()
        if missing := self._missing:
            raise IncompleteRosterError(missing)
        if self._blocked:
            msg = "cluster has a failed shard agent"
            raise RuntimeError(msg)

    async def _require_ready(self) -> None:
        self._require_complete()
        statuses = await asyncio.gather(
            *(self._endpoint_status(agent) for agent in self._ordered_agents)
        )
        if any(status.phase != "running" or not status.ready for status in statuses):
            msg = "all shard game processes must be ready"
            raise RuntimeError(msg)

    async def _status(self, name: str) -> ShardRuntimeStatus:
        return await self._endpoint_status(self.agent(name))

    async def _endpoint_status(
        self,
        endpoint: AgentEndpoint,
    ) -> ShardRuntimeStatus:
        name = endpoint.name
        try:
            status = await self._agent_call(
                endpoint.runtime_status,
                limit=AGENT_STATUS_TIMEOUT,
            )
        except Exception:
            status = self._last_status[name].replace(
                phase="unavailable",
                ready=False,
                pid=None,
                game_attempt=None,
                session_id=None,
                returncode=None,
                stable_since_ns=None,
                driver_health=None,
                driver_error=None,
                error_id=ULID(),
                error="shard agent is unavailable",
            )
        else:
            self._last_status[name] = status
        return status.replace(desired=self._desired[name])

    def _cluster_phase(
        self,
        statuses: tuple[ShardRuntimeStatus, ...],
        missing: tuple[str, ...],
        error_id: ULID | None,
    ) -> ClusterPhase:
        if self._phase is not None:
            return self._phase
        if missing:
            return "degraded" if self._ever_complete else "waitingAgents"
        if error_id is not None or any(status.phase == "failed" for status in statuses):
            return "failed"
        if any(status.phase == "unavailable" for status in statuses):
            return "degraded"
        desired = tuple(self._desired.values())
        if (
            all(value == "running" for value in desired)
            and statuses
            and all(status.phase == "running" and status.ready for status in statuses)
        ):
            return "running"
        if (
            all(value == "stopped" for value in desired)
            and statuses
            and all(status.phase == "stopped" for status in statuses)
        ):
            return "stopped"
        if any(status.phase == "stopping" for status in statuses):
            return "stopping"
        if all(value == "running" for value in desired):
            return "starting"
        return "degraded"

    async def _prepare(
        self,
        *,
        force: bool = False,
    ) -> None:
        self._require_complete()
        self._phase = "preparing"
        try:
            for _ in range(PREPARE_ATTEMPTS):
                read = await self._configuration.read()
                if isinstance(read, InvalidConfiguration):
                    raise InvalidConfigurationError(read.revision, read.paths)
                self._configuration.validate_deployment(read.configuration)
                if not force and read.revision == self._prepared_revision:
                    break
                actual = await service.prepare_shared(
                    self.install_path,
                    self.cluster_path,
                    update_mods=True,
                )
                self._require_open()
                if {(item.name, item.master) for item in actual} != {
                    (item.name, item.master) for item in self._layout.values()
                }:
                    msg = "prepared shard topology does not match controller"
                    raise RuntimeError(msg)
                current = await self._configuration.read()
                if isinstance(current, InvalidConfiguration):
                    raise InvalidConfigurationError(current.revision, current.paths)
                self._configuration.validate_deployment(current.configuration)
                if current.revision == read.revision:
                    self._prepared_revision = current.revision
                    break
                force = True
            else:
                msg = "cluster configuration changed repeatedly during preparation"
                raise RuntimeError(msg)
            self._require_open()
            await self._gather(
                self._ordered_agents,
                lambda agent: agent.activate(),
            )
        finally:
            self._phase = None

    async def _start_desired(self) -> None:
        self._require_complete()
        try:
            await self._prepare()
            names = tuple(
                name for name in self._names if self._desired[name] == "running"
            )
            await self._lifecycle_operation(
                "starting",
                lambda agent: agent.start(),
                names,
                limit=AGENT_START_TIMEOUT,
            )
            self._clear_error()
        except Exception as error:
            if self._closed:
                raise
            cleanup = await self._fail_close("cluster start failed", error)
            if cleanup is not None:
                error = BaseExceptionGroup(
                    "cluster start and cleanup failed",
                    (error, cleanup),
                )
            raise self._operation_error(error, self._error_id) from None

    async def _stop_registered(self, *, force: bool) -> None:
        self._require_open()
        operation: _Operation[object]
        operation = (
            (lambda agent: agent.kill()) if force else (lambda agent: agent.stop())
        )
        limit = AGENT_KILL_TIMEOUT if force else AGENT_STOP_TIMEOUT
        try:
            await self._lifecycle_operation(
                "stopping",
                operation,
                tuple(self._agents),
                limit=limit,
            )
        except Exception as error:
            if self._closed:
                raise
            failures = await self._kill_agents(tuple(self._agents.values()))
            if failures:
                error = BaseExceptionGroup(
                    "cluster stop and forced termination failed",
                    (error, *failures),
                )
            self._record_error("cluster stop failed", error)
            raise self._operation_error(error, self._error_id) from None
        self._blocked.clear()
        self._clear_error()

    async def _lifecycle_operation(
        self,
        phase: ClusterPhase,
        operation: _Operation[object],
        names: tuple[str, ...] | None = None,
        *,
        limit: float,
    ) -> None:
        self._phase = phase
        selected = self._names if names is None else names
        try:
            results = await asyncio.gather(
                *(
                    self._agent_call(
                        lambda name=name: operation(self._agents[name]),
                        limit=limit,
                    )
                    for name in selected
                ),
                return_exceptions=True,
            )
        finally:
            self._phase = None
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            message = "shard lifecycle operation failed"
            raise BaseExceptionGroup(message, errors)

    async def _fail_close(
        self,
        reason: str,
        cause: BaseException | None = None,
        *,
        agents: tuple[AgentEndpoint, ...] | None = None,
    ) -> BaseExceptionGroup | None:
        self._record_error(reason, cause)
        self._phase = "stopping"
        try:
            _, failures = await self._terminate_agents(
                tuple(self._agents.values()) if agents is None else agents
            )
            if failures:
                return BaseExceptionGroup("cluster fail-close failed", failures)
            return None
        finally:
            self._phase = None

    async def _reload(
        self,
        operation: Callable[[AgentEndpoint], Awaitable[None]],
        completion_timeout: float,
    ) -> None:
        async with self._public_operation():
            await self._require_ready()
            agents = self._ordered_agents
            mutation_completed = False
            try:
                markers = await self._gather(
                    agents,
                    lambda agent: agent.generation_marker(),
                )
                await self._agent_call(
                    lambda: operation(self.agent(self.master)),
                    limit=completion_timeout + 5,
                )
                mutation_completed = True
                await self._gather(
                    agents,
                    lambda agent: agent.wait_generation(
                        markers[agent.name], completion_timeout
                    ),
                    limit=completion_timeout + 5,
                )
            except Exception as error:
                raise self._operation_error(
                    error,
                    mutation_completed=mutation_completed,
                ) from None

    async def _shard_results[T](
        self,
        operation: _Operation[T],
        *,
        limit: float = AGENT_CALL_TIMEOUT,
    ) -> tuple[ShardResult[T], ...]:
        await self._require_ready()
        agents = self._ordered_agents
        values = await asyncio.gather(
            *(
                self._agent_call(lambda agent=agent: operation(agent), limit=limit)
                for agent in agents
            ),
            return_exceptions=True,
        )
        return tuple(
            ShardResult(name, error=value)
            if isinstance(value, BaseException)
            else ShardResult(name, value=value)
            for name, value in zip(self._names, values, strict=True)
        )

    async def _gather[T](
        self,
        agents: tuple[AgentEndpoint, ...],
        operation: _Operation[T],
        *,
        limit: float = AGENT_CALL_TIMEOUT,
    ) -> dict[str, T]:
        values = await asyncio.gather(
            *(
                self._agent_call(
                    lambda agent=agent: operation(agent),
                    limit=limit,
                )
                for agent in agents
            )
        )
        return {agent.name: value for agent, value in zip(agents, values, strict=True)}

    @staticmethod
    def _locate(players: list[tuple[str, Player]]) -> LocatedPlayer:
        active = [
            (name, player) for name, player in players if player.state is not None
        ]
        if len(active) > 1:
            raise PlayerLocationConflictError
        name, player = active[0] if active else players[0]
        return LocatedPlayer(shard=name if active else None, player=player)

    def _schedule_reconcile(self) -> None:
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return
        self._reconcile_task = asyncio.create_task(
            self._reconcile(),
            name=f"dst-controller-{self.epoch}",
        )
        self._reconcile_task.add_done_callback(self._consume_task_result)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    async def _reconcile(self) -> None:
        async with self._serialized():
            if self._complete and not self._blocked and not self._closed:
                if any(value == "running" for value in self._desired.values()):
                    await self._start_desired()
                elif not await self._all_stopped():
                    await self._stop_registered(force=False)

    def _start_relay[T](  # ruff: ignore[complex-structure]
        self,
        name: str,
        source: Broadcast[T],
        *targets: Broadcast[T],
    ) -> asyncio.Task[None]:
        async def relay() -> None:
            while not self._closed:
                try:
                    subscription = source.subscribe()
                except RuntimeError:
                    logger.warning(
                        "internal shard relay source is closed: {shard}",
                        shard=name,
                    )
                    return
                try:
                    while batch := await subscription.next(256):
                        for item in batch:
                            for target in targets:
                                target.publish(item)
                except SubscriptionOverflowError:
                    logger.warning(
                        "internal shard relay overflowed; resubscribing: {shard}",
                        shard=name,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "internal shard relay ended: {shard}",
                        shard=name,
                    )
                    return
                else:
                    return
                finally:
                    subscription.close()

        return asyncio.create_task(relay(), name="dst-controller-relay")

    async def _cancel_relays(self, name: str) -> None:
        tasks = self._relays.pop(name, ())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _record_error(
        self,
        message: str,
        cause: BaseException | None = None,
    ) -> None:
        self._error_id = ULID()
        self._error = message
        if cause is not None:
            logger.error(
                "{message}: {error_id}: {kind}",
                message=message,
                error_id=self._error_id,
                kind=type(cause).__name__,
            )

    def _clear_error(self) -> None:
        self._error_id = None
        self._error = None

    @staticmethod
    def _operation_error(
        error: BaseException,
        error_id: ULID | None = None,
        *,
        mutation_completed: bool = False,
    ) -> (
        ControllerOperationError
        | IndeterminateError
        | IndeterminateCommandError
        | ConfigurationStoreError
    ):
        if indeterminate := _indeterminate(error):
            return indeterminate
        if mutation_completed:
            return IndeterminateError()
        if configuration_error := _configuration_error(error):
            return configuration_error
        result = ControllerOperationError(error_id=error_id)
        logger.error(
            "cluster operation failed: {error_id}: {kind}",
            error_id=result.error_id,
            kind=type(error).__name__,
        )
        return result


class ShardController:
    def __init__(self, cluster: ClusterController, name: str) -> None:
        self.cluster = cluster
        self.name = name

    async def status(self) -> ShardRuntimeStatus:
        return await self.cluster._status(self.name)

    async def _call[T](
        self,
        operation: _Operation[T],
        *,
        limit: float = AGENT_CALL_TIMEOUT,
    ) -> T:
        return await self.cluster._agent_call(
            lambda: operation(self.cluster.agent(self.name)),
            limit=limit,
        )

    async def start(self) -> None:
        async with self.cluster._public_operation():
            self.cluster._require_complete()
            self.cluster._desired[self.name] = "running"
            await self.cluster._prepare()
            await self._call(lambda agent: agent.start(), limit=AGENT_START_TIMEOUT)

    async def stop(self) -> None:
        async with self.cluster._public_operation():
            self.cluster._desired[self.name] = "stopped"
            await self._call(lambda agent: agent.stop(), limit=AGENT_STOP_TIMEOUT)

    async def restart(self) -> None:
        async with self.cluster._public_operation():
            self.cluster._require_complete()
            self.cluster._desired[self.name] = "running"
            await self.cluster._prepare()
            await self._call(lambda agent: agent.restart(), limit=AGENT_RESTART_TIMEOUT)

    async def kill(self) -> None:
        async with self.cluster._public_operation():
            self.cluster._desired[self.name] = "stopped"
            await self._call(lambda agent: agent.kill(), limit=AGENT_KILL_TIMEOUT)

    async def execute(
        self,
        source: str,
        completion_timeout: float = 30,
    ) -> str:
        completion_timeout = positive_timeout(completion_timeout)
        return await self._call(
            lambda agent: agent.execute(source, completion_timeout),
            limit=completion_timeout + 5,
        )

    async def execute_json(self, source: str) -> JsonValue:
        return await self._call(lambda agent: agent.execute_json(source))

    async def health(self) -> DriverHealth:
        return await self._call(lambda agent: agent.health())

    async def room(self) -> Room:
        return await self._call(lambda agent: agent.room())

    async def world(self) -> World:
        return await self._call(lambda agent: agent.world())

    async def runtime(self) -> Runtime:
        return await self._call(lambda agent: agent.runtime())

    async def mods(self) -> tuple[Mod, ...]:
        return await self._call(lambda agent: agent.mods())

    async def connected_shards(self) -> tuple[ShardStatus, ...]:
        return await self._call(lambda agent: agent.connected_shards())

    async def save(self, completion_timeout: float = 30) -> SavedEvent:
        completion_timeout = positive_timeout(completion_timeout)
        return await self._call(
            lambda agent: agent.save(completion_timeout),
            limit=completion_timeout + 5,
        )

    async def pause(self, paused: bool) -> bool:
        return await self._call(lambda agent: agent.pause(paused))

    async def regenerate_shard(
        self,
        *,
        preserve_settings: bool = True,
        completion_timeout: float = 30,
    ) -> None:
        completion_timeout = positive_timeout(completion_timeout)
        async with self.cluster._public_operation():
            await self._call(
                lambda agent: agent.regenerate_shard(
                    preserve_settings=preserve_settings,
                    completion_timeout=completion_timeout,
                ),
                limit=completion_timeout + 5,
            )

    async def list_players(self) -> tuple[Player, ...]:
        return await self._call(lambda agent: agent.list_players())

    async def get_player(self, userid: str) -> Player | None:
        return await self._call(lambda agent: agent.get_player(userid))

    async def inventory(self, userid: str) -> Inventory | None:
        return await self._call(lambda agent: agent.inventory(userid))

    async def kick(self, userid: str) -> None:
        await self._call(lambda agent: agent.kick(userid))

    async def ban(self, userid: str, *, seconds: int | None = None) -> None:
        await self._call(lambda agent: agent.ban(userid, seconds=seconds))

    async def blocklist(self) -> tuple[str, ...]:
        return await self._call(lambda agent: agent.blocklist())

    async def is_blocked(self, userid: str) -> bool:
        return await self._call(lambda agent: agent.is_blocked(userid))

    async def unban(self, userid: str) -> bool:
        return await self._call(lambda agent: agent.unban(userid))

    async def is_admin(self, userid: str) -> bool | None:
        return await self._call(lambda agent: agent.is_admin(userid))

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
        return await self._call(
            lambda agent: agent.set_vitals(
                userid,
                health=health,
                hunger=hunger,
                sanity=sanity,
                temperature=temperature,
                moisture=moisture,
            )
        )

    async def kill_player(self, userid: str) -> bool:
        return await self._call(lambda agent: agent.kill_player(userid))

    async def revive(self, userid: str) -> bool:
        return await self._call(lambda agent: agent.revive(userid))

    async def despawn(self, userid: str) -> bool:
        return await self._call(lambda agent: agent.despawn(userid))

    async def migrate(
        self,
        userid: str,
        shard_id: str,
        portal_id: int = 1,
    ) -> bool:
        return await self._call(
            lambda agent: agent.migrate(userid, shard_id, portal_id)
        )

    async def teleport(
        self,
        userid: str,
        x: float,
        y: float,
        z: float,
    ) -> bool:
        return await self._call(lambda agent: agent.teleport(userid, x, y, z))

    async def give(self, userid: str, item: str, count: int = 1) -> int:
        return await self._call(lambda agent: agent.give(userid, item, count))

    async def remove(self, userid: str, item: str, count: int = 1) -> int:
        return await self._call(lambda agent: agent.remove(userid, item, count))

    def subscribe_logs(self) -> Subscription[LogRecord]:
        return self.cluster._subscribe(self.cluster._shard_logs[self.name])

    def subscribe_lifecycle(self) -> Subscription[LifecycleRecord]:
        return self.cluster._subscribe(self.cluster._shard_lifecycle[self.name])

    def subscribe_events(self) -> Subscription[GameEventRecord]:
        return self.cluster._subscribe(self.cluster._shard_events[self.name])
