# ruff: file-ignore[private-member-access]
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from logbook import Logger
from ulid import ULID

from dst_server import commands as c
from dst_server.announcements import MOD_UPDATE_NOTICE, Countdown
from dst_server.api import ClusterAPI, ShardAPI
from dst_server.concurrency import cancel_tasks, complete
from dst_server.configuration.models import ClusterConfig
from dst_server.configuration.store import ConfigurationStore
from dst_server.errors import (
    ControllerOperationError,
    DisconnectedError,
    ErrorCode,
    IncompleteRosterError,
    IndeterminateCommandError,
    IndeterminateError,
    PlayerLocationConflictError,
    RemoteError,
    SubscriptionOverflowError,
    error_info,
)
from dst_server.events.world import ModOutdatedEvent
from dst_server.game.rpc import LuaRequestError
from dst_server.models import Player, Runtime
from dst_server.models.cluster import (
    ClusterPhase,
    ClusterStatus,
    GameEventRecord,
    LifecycleRecord,
    LocatedPlayer,
    LogRecord,
    ShardDesired,
    ShardPhase,
    ShardResult,
    ShardRuntimeStatus,
)
from dst_server.models.snapshot import Snapshot, SnapshotCatalog
from dst_server.mods import ModUpdateError
from dst_server.mods.maintenance import ModMaintenance
from dst_server.timeouts import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_LIFECYCLE_TIMEOUT,
    DEFAULT_RELOAD_TIMEOUT,
    DEFAULT_SAVE_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    OUTPUT_DRAIN_TIMEOUT,
    RPC_TIMEOUT_MARGIN,
    operation_deadline,
    timeout_scope,
)

from . import service
from .subscriptions import (
    STREAM_MODELS,
    Broadcast,
    StreamKind,
    StreamRecord,
    Subscription,
)

logger = Logger(__name__)
AGENT_CALL_TIMEOUT = DEFAULT_COMMAND_TIMEOUT + RPC_TIMEOUT_MARGIN
AGENT_STATUS_TIMEOUT = 30.0
AGENT_START_TIMEOUT = (
    DEFAULT_STARTUP_TIMEOUT + 2 * OUTPUT_DRAIN_TIMEOUT + RPC_TIMEOUT_MARGIN
)
AGENT_STOP_TIMEOUT = (
    DEFAULT_STOP_TIMEOUT + 2 * OUTPUT_DRAIN_TIMEOUT + RPC_TIMEOUT_MARGIN
)
AGENT_KILL_TIMEOUT = OUTPUT_DRAIN_TIMEOUT + RPC_TIMEOUT_MARGIN
CONTROLLER_CANCEL_TIMEOUT = 1.0
ANNOUNCE_TIMEOUT = 5.0
HEALTH_INTERVAL = 30.0
HEALTH_FAILURE_TIMEOUT = 300.0


class AgentEndpoint(Protocol):
    name: str
    master: bool
    incarnation: str
    logs: Broadcast[LogRecord]
    lifecycle: Broadcast[LifecycleRecord]
    game_events: Broadcast[GameEventRecord]

    async def runtime_status(self) -> ShardRuntimeStatus: ...

    async def activate(self) -> None: ...

    async def invoke[T](self, command: c.Request[T]) -> T: ...


type _Operation[T] = Callable[[AgentEndpoint], Awaitable[T]]


@dataclass(frozen=True, slots=True)
class _LoadExpectation:
    previous: ShardRuntimeStatus | None = None
    restart: bool = False

    def ready(self, current: ShardRuntimeStatus | None) -> bool:
        if current is None or current.phase != "running" or not current.ready:
            return False
        previous = self.previous
        if previous is None:
            return True
        if self.restart:
            return current.game_attempt is not None and (
                current.game_attempt != previous.game_attempt
            )
        return (
            current.game_attempt == previous.game_attempt
            and current.driver_health is not None
            and previous.driver_health is not None
            and current.driver_health.generation > previous.driver_health.generation
        )


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
        if isinstance(nested, IndeterminateError | IndeterminateCommandError):
            return nested
    return None


class ClusterController(ClusterAPI):
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
        self._desired: dict[str, ShardDesired] = dict.fromkeys(
            names, ShardDesired.RUNNING
        )
        self._prepared = False
        self._phase: ClusterPhase | None = None
        self._error_id: ULID | None = None
        self._error: str | None = None
        self._ever_complete = False
        self._lock = asyncio.Lock()
        self._lock_owner: asyncio.Task[Any] | None = None
        # Only the lock owner changes this; releasing the lock clears it.
        self._pending_confirmation = False
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._shutdown_complete = False
        self._initial_task: asyncio.Task[None] | None = None
        self._registered = asyncio.Event()
        self._fatal = asyncio.Event()
        self._mod_maintenance = ModMaintenance()
        self._mod_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._loading: dict[str, _LoadExpectation] = {}
        self._load_deadline = 0.0
        self._streams = {kind: Broadcast[StreamRecord]() for kind in STREAM_MODELS}
        self._shard_streams = {
            name: {kind: Broadcast[StreamRecord]() for kind in STREAM_MODELS}
            for name in names
        }
        self._relays: dict[str, tuple[asyncio.Task[None], ...]] = {}
        self._shards = {name: ShardController(self, name) for name in names}

    async def invoke[T](self, command: c.Request[T]) -> T:
        operation = c.operation("cluster", command)
        if isinstance(command, c.Stop | c.Kill):
            await self._interrupt_operation()
        elif isinstance(command, c.Start | c.Restart) or (
            isinstance(command, c.UpdateMods) and command.restart
        ):
            await self._cancel_initial_start()
        handlers: dict[type[c.Request[Any]], Callable[..., Awaitable[Any]]] = {
            c.Start: self._start,
            c.Stop: self._stop,
            c.Restart: self._restart,
            c.Kill: self._kill,
            c.UpdateMods: self._update_mods,
            c.ReadConfiguration: self._read_configuration,
            c.ExecuteAll: self._execute_all,
            c.Announce: self._announce,
            c.ClusterSave: self._save_ready,
            c.ClusterPause: self._pause,
            c.Reset: self._reset,
            c.Rollback: self._rollback,
            c.Snapshots: self._list_snapshots,
            c.RollbackToDay: self._rollback_to_day,
            c.Regenerate: self._regenerate,
            c.LocatePlayers: self._list_players,
            c.LocatePlayer: self._get_player,
            c.IsWhitelisted: self._is_whitelisted,
            c.Whitelist: self._whitelist,
            c.Unwhitelist: self._unwhitelist,
            c.ClusterStatusQuery: self._status,
        }
        arguments = command.arguments
        if isinstance(
            command,
            c.ExecuteAll
            | c.ClusterSave
            | c.Reset
            | c.Rollback
            | c.RollbackToDay
            | c.Regenerate,
        ):
            arguments["completion_timeout"] = command.timeout
        async with (
            timeout_scope(command.timeout),
            self._public_operation(stopping=isinstance(command, c.Stop | c.Kill))
            if operation.mutation
            else nullcontext(),
        ):
            result = await handlers[type(command)](**arguments)
        return operation.response.validate_python(result, strict=True)

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
            if self._ever_complete:
                msg = "room agents cannot register again after startup"
                raise RuntimeError(msg)
            status = await self._validate_endpoint(endpoint)
            self._require_open()
            if endpoint.name in self._agents:
                msg = f"shard agent is already registered: {endpoint.name}"
                raise RuntimeError(msg)
            streams = self._shard_streams[endpoint.name]
            relays = (
                self._start_relay(
                    endpoint.name,
                    endpoint.logs,
                    self._streams["logs"].publish,
                    streams["logs"].publish,
                ),
                self._start_relay(
                    endpoint.name,
                    endpoint.lifecycle,
                    self._streams["lifecycle"].publish,
                    streams["lifecycle"].publish,
                ),
                self._start_relay(
                    endpoint.name,
                    endpoint.game_events,
                    self._streams["events"].publish,
                    streams["events"].publish,
                ),
            )
            self._agents[endpoint.name] = endpoint
            self._last_status[endpoint.name] = status
            self._relays[endpoint.name] = relays
            if self._complete:
                self._ever_complete = True
                self._registered.set()
                self._initial_task = asyncio.create_task(
                    self._initialize(), name=f"dst-start-{self.epoch}"
                )
                self._monitor_task = asyncio.create_task(
                    self._watch_runtime(), name=f"dst-runtime-monitor-{self.epoch}"
                )
                if self._mod_task is None and self._mod_maintenance.enabled:
                    self._mod_task = asyncio.create_task(
                        self._watch_mods(), name=f"dst-mod-maintenance-{self.epoch}"
                    )

    async def unregister(self, endpoint: AgentEndpoint) -> bool:
        if self._closed or self._agents.get(endpoint.name) is not endpoint:
            return False
        await self._interrupt_operation()
        async with self._serialized():
            self._agents.pop(endpoint.name, None)
            await self._cancel_relays(endpoint.name)
            cleanup = await self._fail_close("shard agent disconnected")
            self._fatal.set()
            if cleanup is not None:
                raise cleanup
            return True

    async def failed(self, endpoint: AgentEndpoint) -> bool:
        if self._closed or self._agents.get(endpoint.name) is not endpoint:
            return False
        if (await self._endpoint_status(endpoint)).phase not in {
            "failed",
            "unavailable",
        }:
            return True
        if self._desired[endpoint.name] is ShardDesired.STOPPED:
            return True
        await self._interrupt_operation()
        async with self._serialized():
            if self._closed or self._agents.get(endpoint.name) is not endpoint:
                return False
            if self._desired[endpoint.name] is ShardDesired.STOPPED:
                return True
            if (await self._endpoint_status(endpoint)).phase not in {
                "failed",
                "unavailable",
            }:
                return True
            cleanup = await self._fail_close("shard game process failed")
            self._fatal.set()
            if cleanup is not None:
                raise cleanup
            return True

    async def wait_fatal(self) -> None:
        async with asyncio.timeout(DEFAULT_CONNECT_TIMEOUT):
            await self._registered.wait()
        await self._fatal.wait()
        raise ControllerOperationError(self._error_id)

    async def _status(self) -> ClusterStatus:
        self._require_open()
        missing = self._missing
        agents = tuple(
            self._agents[name] for name in self._names if name in self._agents
        )
        loading = self._loading
        statuses = tuple(
            await asyncio.gather(*(self._endpoint_status(agent) for agent in agents))
        )
        runtimes = await self._refresh_loading(
            {state.name: state for state in statuses}, loading
        )
        error_id, error = self._error_id, self._error
        phase = self._cluster_phase(statuses, missing, error_id)
        if phase == "running":
            runtimes = runtimes or await self._observe_connections()
            if runtimes is None:
                phase = "degraded"
            else:
                statuses = tuple(
                    state.replace(session_id=runtimes[state.name].session_id)
                    for state in statuses
                )
        return ClusterStatus(
            epoch=self.epoch,
            phase=phase,
            prepared=self._prepared,
            busy=self._lock.locked() or bool(self._loading),
            master=self.master,
            missing_shards=missing,
            shards=statuses,
            mod_update=self._mod_maintenance.status(asyncio.get_running_loop().time()),
            error_id=error_id,
            error=error,
        )

    async def _start(self) -> None:
        self._desired = dict.fromkeys(self._names, ShardDesired.RUNNING)
        self._require_complete()
        self._clear_error()
        await self._start_desired()

    async def _stop(self, *, notice: Countdown | None) -> None:
        await self._notify(notice)
        self._desired = dict.fromkeys(self._names, ShardDesired.STOPPED)
        await self._stop_registered(force=False)

    async def _restart(self, *, notice: Countdown | None) -> None:
        if missing := self._missing:
            raise IncompleteRosterError(missing)
        await self._notify(notice)
        self._desired = dict.fromkeys(self._names, ShardDesired.RUNNING)
        self._require_complete()
        self._clear_error()
        await self._stop_registered(force=False)
        await self._start_desired()

    async def _kill(self) -> None:
        self._desired = dict.fromkeys(self._names, ShardDesired.STOPPED)
        await self._stop_registered(force=True)

    async def _update_mods(
        self, *, restart: bool = False, notice: Countdown | None
    ) -> None:
        self._require_complete()
        running = not await self._all_stopped()
        if restart:
            self._desired = dict.fromkeys(self._names, ShardDesired.RUNNING)
            await self._notify(notice if running else None)
            await self._update_and_start(running=running)
        else:
            await self._prepare(force=True)

    async def _update_and_start(self, *, running: bool) -> None:
        if running:
            await self._stop_registered(force=False)
        await self._start_desired(force_prepare=True)

    async def _read_configuration(self) -> ClusterConfig:
        self._require_open()
        return await self._configuration.read()

    async def _execute_all(
        self,
        source: str,
        completion_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> tuple[ShardResult[str], ...]:
        return await self._shard_results(
            lambda agent: agent.invoke(
                c.Execute(source=source, timeout=completion_timeout)
            ),
            limit=completion_timeout + RPC_TIMEOUT_MARGIN,
        )

    async def _announce(self, message: str, count: int, interval: float) -> None:
        await self._require_ready()
        await self.shard(self.master)._call(
            c.Announce(message=message, count=count, interval=interval)
        )

    async def _interrupt_operation(self) -> None:
        await cancel_tasks(
            *(
                task
                for task in {self._lock_owner, self._initial_task}
                if task is not None and task is not asyncio.current_task()
            )
        )

    async def _cancel_initial_start(self) -> None:
        task = self._initial_task
        if task is not None and not task.done() and task is not self._lock_owner:
            await cancel_tasks(task)

    async def _notice_recipient(self, names: tuple[str, ...]) -> AgentEndpoint | None:
        agents = tuple(self._agents[name] for name in names if name in self._agents)
        results = await asyncio.gather(
            *(
                self._agent_call(
                    lambda agent=agent: agent.invoke(
                        c.ListPlayers(timeout=ANNOUNCE_TIMEOUT)
                    ),
                    limit=ANNOUNCE_TIMEOUT,
                )
                for agent in agents
            ),
            return_exceptions=True,
        )
        if not any(value for value in results if not isinstance(value, BaseException)):
            return None
        available = tuple(
            agent
            for agent, value in zip(agents, results, strict=True)
            if not isinstance(value, BaseException)
        )
        return next((agent for agent in available if agent.master), available[0])

    async def _notify(
        self, notice: Countdown | None, *, names: tuple[str, ...] | None = None
    ) -> None:
        self._require_open()
        if notice is None:
            return
        try:
            recipient = await self._notice_recipient(
                self._names if names is None else names
            )
        except Exception:
            logger.warning(
                "maintenance audience unavailable; continuing lifecycle operation"
            )
            recipient = None
        self._require_open()
        if recipient is None:
            return

        async def send(message: str) -> None:
            await self._agent_call(
                lambda: recipient.invoke(
                    c.Announce(message=message, timeout=ANNOUNCE_TIMEOUT)
                ),
                limit=ANNOUNCE_TIMEOUT,
            )

        try:
            await notice.run(send)
        except Exception:
            logger.warning(
                "maintenance announcement failed; continuing lifecycle operation"
            )
        self._require_open()

    async def _save_ready(
        self, completion_timeout: float = DEFAULT_SAVE_TIMEOUT
    ) -> None:
        await self._wait_ready()
        await self.shard(self.master)._call(c.Save(timeout=completion_timeout))

    async def _pause(self, paused: bool) -> tuple[ShardResult[bool], ...]:
        return await self._shard_results(
            lambda agent: agent.invoke(c.Pause(paused=paused))
        )

    async def _reset(self, completion_timeout: float = DEFAULT_RELOAD_TIMEOUT) -> None:
        await self._restore_snapshot(count=0, completion_timeout=completion_timeout)

    async def _rollback(
        self, count: int = 1, completion_timeout: float = DEFAULT_RELOAD_TIMEOUT
    ) -> None:
        await self._restore_snapshot(count=count, completion_timeout=completion_timeout)

    async def _list_snapshots(
        self, limit: int = 100, *, before: int | None = None
    ) -> SnapshotCatalog:
        return await self.shard(self.master).list_snapshots(limit=limit, before=before)

    async def _rollback_to_day(
        self, day: int, completion_timeout: float = DEFAULT_RELOAD_TIMEOUT
    ) -> Snapshot:
        return await self._restore_snapshot(
            day=day, completion_timeout=completion_timeout
        )

    async def _restore_snapshot(
        self,
        *,
        count: int = 0,
        day: int | None = None,
        completion_timeout: float,
    ) -> Snapshot:
        runtimes = await self._wait_ready()
        sessions = {name: state.session_id for name, state in runtimes.items()}
        snapshot = await self._select_snapshot(sessions, count=count, day=day)
        await self._reload(
            lambda master: master.invoke(
                c.RollbackToSnapshot(
                    session_id=sessions[self.master],
                    snapshot_id=snapshot.snapshot_id,
                    timeout=completion_timeout,
                )
            ),
            completion_timeout,
            sessions=sessions,
        )
        return snapshot

    async def _select_snapshot(  # ruff: ignore[complex-structure]
        self,
        sessions: dict[str, str],
        *,
        count: int,
        day: int | None,
    ) -> Snapshot:
        before: int | None = None
        selected: Snapshot | None = None
        while True:
            catalog = await self.list_snapshots(before=before)
            if catalog.session_id != sessions[self.master]:
                msg = "world session changed while selecting a snapshot"
                raise ValueError(msg)
            for snapshot in catalog.snapshots:
                if (
                    snapshot.snapshot_id == 0
                    or snapshot.world_file is None
                    or (
                        day is not None
                        and (snapshot.metadata is None or snapshot.metadata.day != day)
                    )
                ):
                    continue
                if day is None and count:
                    count -= 1
                    continue
                if await self._snapshot_complete(snapshot, sessions, day):
                    selected = snapshot
                    if day is None:
                        return selected
                elif day is None:
                    msg = f"snapshot {snapshot.snapshot_id} is missing from a shard"
                    raise KeyError(msg)
            if not catalog.has_more:
                if selected is not None:
                    return selected
                msg = "no complete cluster snapshot for the requested target"
                raise KeyError(msg)
            if not catalog.snapshots or (
                before is not None and catalog.snapshots[-1].snapshot_id >= before
            ):
                msg = "snapshot catalog did not advance"
                raise ValueError(msg)
            before = catalog.snapshots[-1].snapshot_id

    async def _snapshot_complete(
        self,
        snapshot: Snapshot,
        sessions: dict[str, str],
        day: int | None,
    ) -> bool:
        copies = await self._gather(
            self._ordered_agents,
            lambda agent: agent.invoke(
                c.Snapshots(limit=1, before=snapshot.snapshot_id + 1)
            ),
        )
        if any(copy.session_id != sessions[name] for name, copy in copies.items()):
            msg = "world session changed while selecting a snapshot"
            raise ValueError(msg)
        return all(
            copy.snapshots
            and copy.snapshots[0].snapshot_id == snapshot.snapshot_id
            and copy.snapshots[0].world_file is not None
            and (
                day is None
                or (
                    copy.snapshots[0].metadata is not None
                    and copy.snapshots[0].metadata.day == day
                )
            )
            for copy in copies.values()
        )

    async def _regenerate(
        self,
        expected_session_id: str | None = None,
        require_empty: bool | None = None,
        completion_timeout: float = DEFAULT_RELOAD_TIMEOUT,
    ) -> None:
        await self._reload(
            lambda master: master.invoke(
                c.Regenerate(
                    expected_session_id=expected_session_id,
                    require_empty=require_empty,
                    timeout=completion_timeout,
                )
            ),
            completion_timeout,
        )

    async def _list_players(self) -> tuple[LocatedPlayer, ...]:
        await self._require_ready()
        players = await self._gather(
            self._ordered_agents,
            lambda agent: agent.invoke(c.ListPlayers()),
        )
        grouped: dict[str, list[tuple[str, Player]]] = {}
        for name in self._names:
            for player in players[name]:
                grouped.setdefault(player.userid, []).append((name, player))
        return tuple(self._locate(grouped[userid]) for userid in sorted(grouped))

    async def _get_player(self, userid: str) -> LocatedPlayer | None:
        await self._require_ready()
        players = await self._gather(
            self._ordered_agents,
            lambda agent: agent.invoke(c.GetPlayer(userid=userid)),
        )
        found = [
            (name, player)
            for name in self._names
            if (player := players[name]) is not None
        ]
        return self._locate(found) if found else None

    async def _is_whitelisted(self, userid: str) -> bool:
        await self._require_ready()
        return await self._agent_call(
            lambda: self.agent(self.master).invoke(c.IsWhitelisted(userid=userid))
        )

    async def _whitelist(self, userid: str) -> bool:
        await self._require_ready()
        return await self.shard(self.master)._call(c.Whitelist(userid=userid))

    async def _unwhitelist(self, userid: str) -> bool:
        await self._require_ready()
        return await self.shard(self.master)._call(c.Unwhitelist(userid=userid))

    def subscribe(self, kind: StreamKind) -> Subscription[StreamRecord]:
        self._require_open()
        return self._streams[kind].subscribe()

    async def wait_idle(self) -> None:
        task = self._initial_task
        if task is not None:
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            task = self._close_task = asyncio.create_task(
                self._close(), name="dst-controller-close"
            )
        try:
            await complete(task)
        finally:
            if (
                self._close_task is task
                and task.done()
                and (task.cancelled() or task.exception() is not None)
            ):
                self._close_task = None

    async def _close(self) -> None:  # ruff: ignore[complex-structure]
        if self._shutdown_complete:
            return
        self._closed = True
        current = asyncio.current_task()
        tasks = {
            task
            for task in (
                self._initial_task,
                self._mod_task,
                self._monitor_task,
                self._lock_owner,
            )
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
        forced, failures = await self._terminate_agents(agents, draining=True)
        await cancel_tasks(*tasks)
        if forced:
            self._record_error("cluster shutdown required forced termination")
        for name in tuple(self._relays):
            await self._cancel_relays(name)
        for streams in (self._streams, *self._shard_streams.values()):
            for broadcast in streams.values():
                broadcast.close()
        if failures:
            error = BaseExceptionGroup("cluster shutdown failed", failures)
            self._record_error("cluster shutdown failed", error)
            raise error
        self._shutdown_complete = True

    @property
    def _complete(self) -> bool:
        return len(self._agents) == len(self._names)

    @property
    def _missing(self) -> tuple[str, ...]:
        return tuple(name for name in self._names if name not in self._agents)

    @property
    def _running_names(self) -> tuple[str, ...]:
        return tuple(name for name in self._names if self._desired[name] == "running")

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
        *,
        draining: bool = False,
    ) -> tuple[bool, tuple[BaseException, ...]]:
        stopped = await asyncio.gather(
            *(
                self._agent_call(
                    lambda agent=agent: agent.invoke(
                        c.Drain(timeout=AGENT_STOP_TIMEOUT)
                        if draining
                        else c.Stop(timeout=AGENT_STOP_TIMEOUT, notice=None)
                    ),
                    limit=AGENT_STOP_TIMEOUT,
                )
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
        failures = tuple(
            result for result in stopped if isinstance(result, BaseException)
        )
        killed = await self._kill_agents(failed)
        self._record_error(
            "graceful shutdown failed; forced termination requested",
            BaseExceptionGroup("shard shutdown failed", failures),
        )
        if await self._all_stopped():
            return True, ()
        return True, (*failures, *killed)

    async def _kill_agents(
        self,
        agents: tuple[AgentEndpoint, ...],
    ) -> tuple[BaseException, ...]:
        killed = await asyncio.gather(
            *(
                self._agent_call(
                    lambda agent=agent: agent.invoke(
                        c.Kill(timeout=AGENT_KILL_TIMEOUT)
                    ),
                    limit=AGENT_KILL_TIMEOUT,
                )
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
        if status.phase != "stopped" or status.pid is not None:
            msg = "agent games must be stopped before registration"
            raise RuntimeError(msg)
        return status

    def _require_open(self) -> None:
        if self._closed:
            msg = "cluster controller is closed"
            raise DisconnectedError(msg)

    @asynccontextmanager
    async def _public_operation(self, *, stopping: bool = False) -> AsyncIterator[None]:
        self._require_open()
        if self._fatal.is_set():
            msg = "room failed; restart its service"
            raise RuntimeError(msg)
        if self._loading and not stopping and not self._lock.locked():
            loading = self._loading
            await self._refresh_loading(
                await self._gather(
                    self._ordered_agents,
                    lambda agent: agent.runtime_status(),
                ),
                loading,
            )
        if self._lock.locked() or (self._loading and not stopping):
            msg = "room operation is busy"
            raise RuntimeError(msg)
        async with self._serialized():
            self._require_open()
            try:
                yield
            except asyncio.CancelledError:
                deadline = operation_deadline.get()
                if (
                    self._pending_confirmation
                    and deadline is not None
                    and asyncio.get_running_loop().time() >= deadline
                ):
                    raise IndeterminateError from None
                raise
            except Exception as error:
                if self._pending_confirmation and not isinstance(
                    error, ControllerOperationError
                ):
                    raise _indeterminate(error) or IndeterminateError() from None
                raise

    @asynccontextmanager
    async def _serialized(self) -> AsyncIterator[None]:
        async with self._lock:
            self._lock_owner = asyncio.current_task()
            try:
                yield
            finally:
                self._pending_confirmation = False
                self._lock_owner = None

    def _require_complete(self) -> None:
        self._require_open()
        if missing := self._missing:
            raise IncompleteRosterError(missing)

    async def _require_ready(self, names: tuple[str, ...] | None = None) -> None:
        self._require_complete()
        statuses = await asyncio.gather(
            *(
                self._endpoint_status(self.agent(name))
                for name in self._names
                if names is None or name in names
            )
        )
        if any(status.phase != "running" or not status.ready for status in statuses):
            msg = "all shard game processes must be ready"
            raise RuntimeError(msg)

    async def _observe_connections(
        self, names: tuple[str, ...] | None = None
    ) -> dict[str, Runtime] | None:
        budget = AGENT_STATUS_TIMEOUT
        if (deadline := operation_deadline.get()) is not None:
            budget = min(
                budget, max(0, deadline - asyncio.get_running_loop().time()) / 2
            )
        try:
            async with asyncio.timeout(budget):
                return await self._connected_runtimes(names)
        except Exception:
            return None

    async def _connected_runtimes(
        self, names: tuple[str, ...] | None = None
    ) -> dict[str, Runtime] | None:
        names = self._names if names is None else names
        runtimes = await self._gather(
            tuple(self.agent(name) for name in names),
            lambda agent: agent.invoke(c.Runtime()),
        )
        expected = {state.shard_id for state in runtimes.values()}
        if len(expected) != len(names):
            msg = "game shards must have unique IDs"
            raise RuntimeError(msg)
        # An intentionally stopped master cannot provide shard connectivity.
        if self.master not in names or len(names) == 1:
            return runtimes
        connected = await self._agent_call(
            lambda: self.agent(self.master).invoke(c.ConnectedShards())
        )
        if expected <= {shard.id for shard in connected if shard.ready}:
            return runtimes
        return None

    async def _wait_ready(
        self, names: tuple[str, ...] | None = None
    ) -> dict[str, Runtime]:
        self._require_complete()
        names = self._names if names is None else names
        if any(self._desired[name] != "running" for name in names):
            msg = "all shards must be enabled for a cluster operation"
            raise RuntimeError(msg)
        while True:
            statuses = await self._gather(
                tuple(self.agent(name) for name in names),
                lambda agent: agent.runtime_status(),
            )
            if any(
                state.phase in {"stopped", "failed", "unavailable"}
                for state in statuses.values()
            ):
                msg = "all shard game processes must be running"
                raise RuntimeError(msg)
            if self._ready(statuses, names):
                loading = self._loading
                runtimes = await self._connected_runtimes(names)
                if runtimes is not None:
                    if self._loading is loading:
                        self._loading = {}
                    return runtimes
            await asyncio.sleep(0.1)

    def _ready(
        self, statuses: dict[str, ShardRuntimeStatus], names: tuple[str, ...]
    ) -> bool:
        return all(
            self._loading.get(name, _LoadExpectation()).ready(statuses.get(name))
            for name in names
        )

    async def _shard_status(self, name: str) -> ShardRuntimeStatus:
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
                phase=ShardPhase.UNAVAILABLE,
                ready=False,
                pid=None,
                game_attempt=None,
                outdated_mods=(),
                session_id=None,
                returncode=None,
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
        if self._loading:
            return "starting"
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
            if force or not self._prepared:
                if await self._all_stopped():
                    self._prepared = False
                    try:
                        await service.prepare_shared(
                            self.install_path,
                            self.cluster_path,
                            update_mods=force or self._mod_maintenance.enabled,
                        )
                    except ModUpdateError:
                        self._mod_maintenance.begin()
                        self._mod_maintenance.finish(
                            asyncio.get_running_loop().time(), failed=True
                        )
                        raise
                    self._require_open()
                    self._prepared = True
                    if force or self._mod_maintenance.enabled:
                        self._mod_maintenance.updated(asyncio.get_running_loop().time())
                elif force:
                    msg = "all game processes must be stopped"
                    raise RuntimeError(msg)
            self._require_open()
            await self._gather(
                self._ordered_agents,
                lambda agent: agent.invoke(c.Activate()),
            )
        finally:
            self._phase = None

    async def _start_desired(self, *, force_prepare: bool = False) -> None:
        self._require_complete()
        names = self._running_names
        if not names:
            return
        await self._prepare(force=force_prepare)
        self._expect_ready(
            dict.fromkeys(names, _LoadExpectation()), DEFAULT_STARTUP_TIMEOUT
        )
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            self._pending_confirmation = True
            async with asyncio.timeout_at(self._load_deadline):
                await self._lifecycle_operation(
                    "starting",
                    lambda agent: agent.invoke(c.Start(timeout=AGENT_START_TIMEOUT)),
                    names,
                    limit=AGENT_START_TIMEOUT,
                )
                self._phase = "starting"
                await self._wait_ready(names)
            self._pending_confirmation = False
            self._clear_error()
        except Exception as error:
            if self._closed:
                raise
            cleanup = await self._fail_close("cluster start failed", error)
            self._fatal.set()
            if cleanup is not None:
                error = BaseExceptionGroup(
                    "cluster start and cleanup failed",
                    (error, cleanup),
                )
            raise self._operation_error(error, self._error_id) from None
        finally:
            self._phase = None

    async def _stop_registered(self, *, force: bool) -> None:
        self._require_open()
        operation: _Operation[object]
        operation = (
            (lambda agent: agent.invoke(c.Kill(timeout=AGENT_KILL_TIMEOUT)))
            if force
            else (
                lambda agent: agent.invoke(
                    c.Stop(timeout=AGENT_STOP_TIMEOUT, notice=None)
                )
            )
        )
        limit = AGENT_KILL_TIMEOUT if force else AGENT_STOP_TIMEOUT
        try:
            self._pending_confirmation = True
            await self._lifecycle_operation(
                "stopping",
                operation,
                tuple(self._agents),
                limit=limit,
            )
            self._pending_confirmation = False
        except Exception as error:
            if self._closed:
                raise
            failures = await self._kill_agents(tuple(self._agents.values()))
            if failures:
                error = BaseExceptionGroup(
                    "cluster stop and forced termination failed",
                    (error, *failures),
                )
            self._record_error("cluster stop required forced termination", error)
            if await self._all_stopped():
                self._pending_confirmation = False
                self._loading = {}
                return
            raise self._operation_error(error, self._error_id) from None
        self._loading = {}
        self._clear_error()

    async def _lifecycle_operation(
        self,
        phase: ClusterPhase,
        operation: _Operation[object],
        names: tuple[str, ...],
        *,
        limit: float,
    ) -> None:
        self._phase = phase
        try:
            results = await asyncio.gather(
                *(
                    self._agent_call(
                        lambda name=name: operation(self._agents[name]),
                        limit=limit,
                    )
                    for name in names
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
    ) -> BaseExceptionGroup | None:
        self._record_error(reason, cause)
        self._phase = "stopping"
        try:
            _, failures = await self._terminate_agents(tuple(self._agents.values()))
            if failures:
                return BaseExceptionGroup("cluster fail-close failed", failures)
            return None
        finally:
            self._phase = None

    async def _reload(
        self,
        operation: _Operation[object],
        completion_timeout: float,
        *,
        sessions: dict[str, str] | None = None,
        names: tuple[str, ...] | None = None,
    ) -> None:
        if names is None:
            await self._wait_ready()
        else:
            await self._require_ready(names)
        markers = await self._gather(
            tuple(self.agent(name) for name in (names or self._names)),
            lambda agent: agent.runtime_status(),
        )
        if sessions is not None and any(
            state.session_id != sessions[name] for name, state in markers.items()
        ):
            msg = "world session changed while selecting a snapshot"
            raise ValueError(msg)
        self._expect_ready(
            {name: _LoadExpectation(state) for name, state in markers.items()},
            DEFAULT_RELOAD_TIMEOUT,
        )
        self._pending_confirmation = True
        try:
            async with asyncio.timeout_at(self._load_deadline):
                await self._agent_call(
                    lambda: operation(self.agent((names or (self.master,))[0])),
                    limit=completion_timeout + RPC_TIMEOUT_MARGIN,
                )
        except (LuaRequestError, RemoteError) as error:
            rejected = (
                error.code
                in {"invalid_request", "not_ready", "stale_generation", "rejected"}
                if isinstance(error, LuaRequestError)
                else error.error.code
                in {
                    ErrorCode.INVALID_ARGUMENT,
                    ErrorCode.INVALID_STATE,
                    ErrorCode.NOT_FOUND,
                    ErrorCode.CONFLICT,
                }
            )
            if rejected:
                self._loading.clear()
            self._pending_confirmation = False
            raise
        self._pending_confirmation = False

    def _expect_ready(
        self, expectations: dict[str, _LoadExpectation], timeout: float
    ) -> None:
        self._loading = expectations
        self._load_deadline = asyncio.get_running_loop().time() + timeout

    async def _refresh_loading(
        self,
        statuses: dict[str, ShardRuntimeStatus],
        loading: dict[str, _LoadExpectation],
    ) -> dict[str, Runtime] | None:
        if self._loading is not loading:
            return None
        stopped = {
            name
            for name, status in statuses.items()
            if name in self._loading
            and (
                (previous := loading[name].previous) is None
                or self._loading[name].restart
                or status.game_attempt == previous.game_attempt
            )
            and self._desired[name] is ShardDesired.STOPPED
            and status.phase in {"stopped", "failed"}
            and status.pid is None
        }
        if stopped:
            self._loading = {
                name: state
                for name, state in self._loading.items()
                if name not in stopped
            }
        loading = self._loading
        names = self._running_names
        if (
            not loading
            or not self._ready(statuses, tuple(loading))
            or not self._ready(statuses, names)
        ):
            return None
        runtimes = await self._observe_connections(names)
        # A concurrent stop or reload may replace the operation being observed.
        if runtimes is not None and self._loading is loading:
            self._loading = {}
        return runtimes

    async def _watch_runtime(self) -> None:
        # One room-owned observer; caller deadlines never own failure recovery.
        operation_deadline.set(None)
        unready_since: float | None = None
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            while not self._closed and not self._fatal.is_set():
                await asyncio.sleep(HEALTH_INTERVAL)
                ready = await self._runtime_ready()
                if ready is True:
                    unready_since = None
                elif ready is False:
                    now = asyncio.get_running_loop().time()
                    unready_since = now if unready_since is None else unready_since
                    if now - unready_since >= HEALTH_FAILURE_TIMEOUT:
                        self._record_error(
                            "room was not ready or connected for 5 minutes"
                        )
                        self._fatal.set()
        except Exception as error:
            self._record_error("room runtime failed", error)
            self._fatal.set()

    async def _runtime_ready(self) -> bool | None:  # ruff: ignore[complex-structure]
        if self._lock.locked():
            return None  # The running operation owns its existing deadline.
        names = self._running_names
        loading = self._loading
        if not names and not loading:
            return True
        try:
            statuses = await self._gather(
                tuple(
                    self.agent(name)
                    for name in self._names
                    if name in names or name in loading
                ),
                lambda agent: agent.runtime_status(),
                limit=AGENT_STATUS_TIMEOUT,
            )
        except Exception:
            statuses = {}
        if (
            self._lock.locked()
            or names != self._running_names
            or self._loading is not loading
        ):
            return None
        if self._loading:
            await self._refresh_loading(statuses, loading)
            if (
                self._loading
                and names
                and self._loading is loading
                and not self._lock.locked()
                and asyncio.get_running_loop().time() >= self._load_deadline
            ):
                msg = "world loading timed out"
                raise TimeoutError(msg)
            return True
        if (
            self._mod_maintenance.enabled
            and self._mod_maintenance.pending
            and statuses
            and all(
                status.phase == "stopped" and status.pid is None
                for status in statuses.values()
            )
        ):
            return True
        if not self._ready(statuses, names):
            return False
        try:
            async with asyncio.timeout(HEALTH_INTERVAL):
                runtimes = await self._connected_runtimes(names)
        except Exception:
            runtimes = None
        if (
            self._lock.locked()
            or self._loading is not loading
            or names != self._running_names
        ):
            return None
        return runtimes is not None

    async def _shard_results[T](
        self,
        operation: _Operation[T],
        *,
        limit: float = AGENT_CALL_TIMEOUT,
    ) -> tuple[ShardResult[T], ...]:
        await self._require_ready()
        agents = self._ordered_agents
        self._pending_confirmation = True
        values = await asyncio.gather(
            *(
                self._agent_call(lambda agent=agent: operation(agent), limit=limit)
                for agent in agents
            ),
            return_exceptions=True,
        )
        self._pending_confirmation = False
        return tuple(
            ShardResult(name, error=error_info(value))
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
        async with asyncio.TaskGroup() as group:
            pending = {
                agent.name: group.create_task(
                    self._agent_call(lambda agent=agent: operation(agent), limit=limit)
                )
                for agent in agents
            }
        return {name: task.result() for name, task in pending.items()}

    @staticmethod
    def _locate(players: list[tuple[str, Player]]) -> LocatedPlayer:
        active = [
            (name, player) for name, player in players if player.state is not None
        ]
        if len(active) > 1:
            raise PlayerLocationConflictError
        name, player = active[0] if active else players[0]
        return LocatedPlayer(shard=name if active else None, player=player)

    async def _initialize(self) -> None:
        operation_deadline.set(None)
        try:
            async with self._serialized(), timeout_scope(DEFAULT_LIFECYCLE_TIMEOUT):
                await self._start_desired()
        except asyncio.CancelledError:
            raise
        except ModUpdateError as error:
            self._record_error(
                "initial MOD download failed; retrying in 5 minutes", error
            )
        except Exception as error:
            self._record_error("cluster initialization failed", error)
            self._fatal.set()

    async def _watch_mods(self) -> None:
        # This room-owned task must not inherit a registration RPC deadline.
        operation_deadline.set(None)
        while not self._closed:
            await self._mod_maintenance.wait()
            try:
                await asyncio.create_task(self._maintain_mods(), name="dst-mod-update")
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
            except Exception as error:
                self._record_error("MOD state observation failed", error)

    async def _maintain_mods(self) -> None:
        if self._lock.locked() or self._loading:
            return
        async with self._public_operation():
            maintenance = self._mod_maintenance
            if self._closed or not maintenance.enabled or not self._complete:
                return
            statuses = tuple(
                await asyncio.gather(
                    *(self._endpoint_status(agent) for agent in self._ordered_agents)
                )
            )
            now = asyncio.get_running_loop().time()
            maintenance.observe(statuses)
            if (
                not maintenance.pending
                or now < maintenance.retry_at
                or any(value != "running" for value in self._desired.values())
            ):
                return
            running = all(
                status.phase == "running" and status.ready for status in statuses
            )
            stopped = all(
                status.phase in {"stopped", "failed"} and status.pid is None
                for status in statuses
            )
            if not running and not stopped:
                return
            await self._notify(MOD_UPDATE_NOTICE if running else None)
            maintenance.begin()
            failed = True
            try:
                async with timeout_scope(DEFAULT_LIFECYCLE_TIMEOUT):
                    await self._update_and_start(running=running)
                failed = False
            except Exception as error:
                self._record_error("automatic MOD maintenance failed", error)
            finally:
                maintenance.finish(asyncio.get_running_loop().time(), failed=failed)

    def _start_relay[T](
        self,
        name: str,
        source: Broadcast[T],
        *publish: Callable[[T], None],
    ) -> asyncio.Task[None]:
        async def relay() -> None:
            subscription = source.subscribe()
            try:
                while not self._closed:
                    try:
                        batch = await subscription.next(256)
                    except SubscriptionOverflowError:
                        self._mod_maintenance.wake()
                        logger.warning(
                            "internal shard relay dropped records: {shard}", shard=name
                        )
                        continue
                    if not batch:
                        return
                    for item in batch:
                        if isinstance(item, GameEventRecord) and isinstance(
                            item.event, ModOutdatedEvent
                        ):
                            self._mod_maintenance.wake()
                        for emit in publish:
                            emit(item)
                    del batch, item
            finally:
                subscription.close()

        return asyncio.create_task(relay(), name="dst-controller-relay")

    async def _cancel_relays(self, name: str) -> None:
        tasks = self._relays.pop(name, ())
        await cancel_tasks(*tasks)

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
    ) -> ControllerOperationError | IndeterminateError | IndeterminateCommandError:
        if indeterminate := _indeterminate(error):
            return indeterminate
        result = ControllerOperationError(error_id=error_id)
        logger.error(
            "cluster operation failed: {error_id}: {kind}",
            error_id=result.error_id,
            kind=type(error).__name__,
        )
        return result


class ShardController(ShardAPI):
    def __init__(self, cluster: ClusterController, name: str) -> None:
        self.cluster = cluster
        self.name = name

    async def invoke[T](self, command: c.Request[T]) -> T:
        operation = c.operation("shard", command)
        if isinstance(command, c.Stop | c.Kill):
            await self.cluster._interrupt_operation()
        async with (
            timeout_scope(command.timeout),
            self.cluster._public_operation(
                stopping=isinstance(command, c.Stop | c.Kill)
            )
            if operation.mutation
            else nullcontext(),
        ):
            if isinstance(command, c.Status):
                result = await self.cluster._shard_status(self.name)
            elif isinstance(command, c.Start | c.Stop | c.Restart | c.Kill):
                result = await self._lifecycle(command)
            elif operation.game == "reload":
                result = await self.cluster._reload(
                    lambda agent: agent.invoke(command),
                    command.timeout,
                    names=(self.name,),
                )
            else:
                result = await self._call(command)
        return operation.response.validate_python(result, strict=True)

    async def _lifecycle(self, command: c.Start | c.Stop | c.Restart | c.Kill) -> None:
        running = isinstance(command, c.Start | c.Restart)
        if running:
            self.cluster._require_complete()
            if not self.cluster._prepared:
                msg = "room resources are not prepared; start the room first"
                raise RuntimeError(msg)
        if isinstance(command, c.Stop | c.Restart):
            notice = command.notice
            if notice is not None and notice.parameters.get("subject") == "本房间":
                notice = notice.replace(
                    parameters={**notice.parameters, "subject": f"分片「{self.name}」"}
                )
            await self.cluster._notify(notice, names=(self.name,))
        self.cluster._desired[self.name] = (
            ShardDesired.RUNNING if running else ShardDesired.STOPPED
        )
        if running:
            await self._call(c.Activate())
            previous = (
                await self.cluster._shard_status(self.name)
                if isinstance(command, c.Restart)
                else None
            )
            self.cluster._expect_ready(
                {self.name: _LoadExpectation(previous, restart=True)},
                DEFAULT_STARTUP_TIMEOUT,
            )
        internal = (
            command.replace(notice=None)
            if isinstance(command, c.Stop | c.Restart)
            else command
        )
        if running:
            async with asyncio.timeout_at(self.cluster._load_deadline):
                await self._call(internal)
                self.cluster._pending_confirmation = True
                await self.cluster._wait_ready(self.cluster._running_names)
                self.cluster._pending_confirmation = False
        else:
            await self._call(internal)
            self.cluster._loading = {
                name: state
                for name, state in self.cluster._loading.items()
                if name != self.name
            }

    async def _call[T](self, command: c.Request[T]) -> T:
        mutation = c.operation("agent", command).mutation
        if mutation:
            self.cluster._pending_confirmation = True
        try:
            result = await self.cluster._agent_call(
                lambda: self.cluster.agent(self.name).invoke(command),
                limit=command.timeout + RPC_TIMEOUT_MARGIN,
            )
        except RemoteError, LuaRequestError:
            # A complete reply already carries the command's outcome.
            if mutation:
                self.cluster._pending_confirmation = False
            raise
        if mutation:
            self.cluster._pending_confirmation = False
        return result

    def subscribe(self, kind: StreamKind) -> Subscription[StreamRecord]:
        self.cluster._require_open()
        return self.cluster._shard_streams[self.name][kind].subscribe()
