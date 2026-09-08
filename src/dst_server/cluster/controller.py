# ruff: file-ignore[private-member-access]
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any, Protocol

from logbook import Logger
from ulid import ULID

from dst_server import commands as c
from dst_server.api import ClusterAPI, ShardAPI
from dst_server.concurrency import cancel_tasks, complete
from dst_server.configuration.models import ClusterConfig
from dst_server.configuration.store import ConfigurationStore
from dst_server.errors import (
    ConfigurationStoreError,
    ControllerOperationError,
    DisconnectedError,
    IncompleteRosterError,
    IndeterminateCommandError,
    IndeterminateError,
    InvalidConfigurationError,
    PlayerLocationConflictError,
    SubscriptionOverflowError,
    error_info,
)
from dst_server.models import Player
from dst_server.models.cluster import (
    ClusterPhase,
    ClusterSaveResult,
    ClusterStatus,
    ConfigurationRead,
    ConfigurationSnapshot,
    GameEventRecord,
    InvalidConfiguration,
    LifecycleRecord,
    LocatedPlayer,
    LogRecord,
    ShardDesired,
    ShardPhase,
    ShardResult,
    ShardRuntimeStatus,
)
from dst_server.models.snapshot import Snapshot, SnapshotCatalog
from dst_server.runtime.supervisor import MAX_ATTEMPTS, RETRY_DELAY
from dst_server.timeouts import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_RELOAD_TIMEOUT,
    DEFAULT_SAVE_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    OUTPUT_DRAIN_TIMEOUT,
    RPC_TIMEOUT_MARGIN,
    timeout_scope,
)

from . import service
from .subscriptions import Broadcast, Subscription

logger = Logger(__name__)
AGENT_CALL_TIMEOUT = DEFAULT_COMMAND_TIMEOUT + RPC_TIMEOUT_MARGIN
AGENT_STATUS_TIMEOUT = 30.0
AGENT_START_TIMEOUT = (
    MAX_ATTEMPTS * (DEFAULT_STARTUP_TIMEOUT + 2 * OUTPUT_DRAIN_TIMEOUT)
    + (MAX_ATTEMPTS - 1) * RETRY_DELAY
    + RPC_TIMEOUT_MARGIN
)
AGENT_STOP_TIMEOUT = (
    DEFAULT_STOP_TIMEOUT + 2 * OUTPUT_DRAIN_TIMEOUT + RPC_TIMEOUT_MARGIN
)
AGENT_KILL_TIMEOUT = OUTPUT_DRAIN_TIMEOUT + RPC_TIMEOUT_MARGIN
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

    async def invoke[T](self, command: c.Request[T]) -> T: ...


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
        self._blocked: dict[str, str] = {}
        self._prepared_revision: ULID | None = None
        self._phase: ClusterPhase | None = None
        self._error_id: ULID | None = None
        self._error: str | None = None
        self._ever_complete = False
        self._lock = asyncio.Lock()
        self._lock_owner: asyncio.Task[Any] | None = None
        self._close_task: asyncio.Task[None] | None = None
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

    async def invoke[T](self, command: c.Request[T]) -> T:
        operation = c.operation("cluster", command)
        handlers: dict[type[c.Request[Any]], Callable[..., Awaitable[Any]]] = {
            c.Start: self._start,
            c.Stop: self._stop,
            c.Restart: self._restart,
            c.Kill: self._kill,
            c.UpdateMods: self._update_mods,
            c.ReadConfiguration: self._read_configuration,
            c.SaveConfiguration: self._save_configuration,
            c.ExecuteAll: self._execute_all,
            c.Announce: self._announce,
            c.ClusterSave: self._save,
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
        # Save/reload own their deadline and classify missing confirmations.
        scope = (
            nullcontext()
            if isinstance(
                command,
                c.ClusterSave | c.Reset | c.Rollback | c.RollbackToDay | c.Regenerate,
            )
            else timeout_scope(command.timeout)
        )
        async with scope:
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

    async def _status(self) -> ClusterStatus:
        self._require_open()
        read = await self._configuration.read()
        revision = read.revision
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
            prepared_revision=self._prepared_revision,
            master=self.master,
            missing_shards=missing,
            shards=statuses,
            error_id=error_id,
            error=error,
        )

    async def _start(self) -> None:
        async with self._public_operation():
            self._desired = dict.fromkeys(self._names, ShardDesired.RUNNING)
            self._blocked.clear()
            self._require_complete()
            self._clear_error()
            await self._start_desired()

    async def _stop(self) -> None:
        async with self._public_operation():
            self._desired = dict.fromkeys(self._names, ShardDesired.STOPPED)
            await self._stop_registered(force=False)

    async def _restart(self) -> None:
        async with self._public_operation():
            self._desired = dict.fromkeys(self._names, ShardDesired.RUNNING)
            self._blocked.clear()
            self._require_complete()
            self._clear_error()
            await self._stop_registered(force=False)
            await self._start_desired()

    async def _kill(self) -> None:
        async with self._public_operation():
            self._desired = dict.fromkeys(self._names, ShardDesired.STOPPED)
            await self._stop_registered(force=True)

    async def _update_mods(self) -> None:
        async with self._public_operation():
            await self._prepare(force=True)

    async def _read_configuration(self) -> ConfigurationRead:
        self._require_open()
        return await self._configuration.read()

    async def _save_configuration(
        self,
        expected_revision: ULID,
        configuration: ClusterConfig,
    ) -> ConfigurationSnapshot:
        async with self._public_operation():
            saved = await self._configuration.save(
                expected_revision,
                configuration,
                all_stopped=self._complete and await self._all_stopped(),
            )
            self._prepared_revision = None
            return saved

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

    async def _announce(self, message: str) -> None:
        await self._require_ready()
        await self._agent_call(
            lambda: self.agent(self.master).invoke(c.Announce(message=message))
        )

    async def _save(
        self, completion_timeout: float = DEFAULT_SAVE_TIMEOUT
    ) -> ClusterSaveResult:
        async with self._public_operation():
            await self._require_ready()
            agents = self._ordered_agents
            mutation_completed = False
            try:
                async with timeout_scope(completion_timeout):
                    markers = await self._gather(
                        agents,
                        lambda agent: agent.invoke(c.SaveMarker()),
                    )
                    master_event = await self._agent_call(
                        lambda: self.agent(self.master).invoke(
                            c.Save(timeout=completion_timeout)
                        ),
                        limit=completion_timeout + RPC_TIMEOUT_MARGIN,
                    )
                    mutation_completed = True
                    events = await self._gather(
                        agents,
                        lambda agent: agent.invoke(
                            c.WaitSaved(
                                cursor=markers[agent.name],
                                snapshot=master_event.snapshot,
                                timeout=completion_timeout,
                            )
                        ),
                        limit=completion_timeout + RPC_TIMEOUT_MARGIN,
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

    async def _pause(self, paused: bool) -> tuple[ShardResult[bool], ...]:
        return await self._shard_results(
            lambda agent: agent.invoke(c.Pause(paused=paused))
        )

    async def _reset(self, completion_timeout: float = DEFAULT_RELOAD_TIMEOUT) -> None:
        await self._reload(
            lambda master: master.invoke(c.Reset(timeout=completion_timeout)),
            completion_timeout,
        )

    async def _rollback(
        self, count: int = 1, completion_timeout: float = DEFAULT_RELOAD_TIMEOUT
    ) -> None:
        await self._reload(
            lambda master: master.invoke(
                c.Rollback(count=count, timeout=completion_timeout)
            ),
            completion_timeout,
        )

    async def _list_snapshots(
        self, limit: int = 100, *, before: int | None = None
    ) -> SnapshotCatalog:
        return await self.shard(self.master).list_snapshots(limit=limit, before=before)

    async def _rollback_to_day(
        self, day: int, completion_timeout: float = DEFAULT_RELOAD_TIMEOUT
    ) -> Snapshot:
        sessions: dict[str, str] = {}

        async def restore(master: AgentEndpoint) -> Snapshot:
            runtimes = await self._gather(
                self._ordered_agents, lambda agent: agent.invoke(c.Runtime())
            )
            sessions.update(
                (name, state.session_id) for name, state in runtimes.items()
            )
            snapshot = await self._snapshot_for_day(day, sessions)
            await master.invoke(
                c.RollbackToSnapshot(
                    session_id=sessions[self.master],
                    snapshot_id=snapshot.snapshot_id,
                    timeout=completion_timeout,
                )
            )
            return snapshot

        async def verify(snapshot: Snapshot) -> None:
            runtimes = await self._gather(
                self._ordered_agents, lambda agent: agent.invoke(c.Runtime())
            )
            worlds = await self._gather(
                self._ordered_agents, lambda agent: agent.invoke(c.World())
            )
            if any(
                state.session_id != sessions[name]
                or state.snapshot != snapshot.snapshot_id + 1
                or worlds[name].day != day
                for name, state in runtimes.items()
            ):
                raise IndeterminateError

        return await self._reload(restore, completion_timeout, verify=verify)

    async def _snapshot_for_day(self, day: int, sessions: dict[str, str]) -> Snapshot:
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
                    or snapshot.metadata is None
                    or snapshot.metadata.day != day
                ):
                    continue
                copies = await self._gather(
                    self._ordered_agents,
                    lambda agent, snapshot=snapshot: agent.invoke(
                        c.Snapshots(limit=1, before=snapshot.snapshot_id + 1)
                    ),
                )
                if any(
                    copy.session_id != sessions[name] for name, copy in copies.items()
                ):
                    msg = "world session changed while selecting a snapshot"
                    raise ValueError(msg)
                if all(
                    copy.snapshots
                    and copy.snapshots[0].snapshot_id == snapshot.snapshot_id
                    and copy.snapshots[0].world_file is not None
                    and copy.snapshots[0].metadata is not None
                    and copy.snapshots[0].metadata.day == day
                    for copy in copies.values()
                ):
                    selected = snapshot
            if not catalog.has_more:
                if selected is not None:
                    return selected
                msg = f"no complete cluster snapshot for day {day}"
                raise KeyError(msg)
            if not catalog.snapshots or (
                before is not None and catalog.snapshots[-1].snapshot_id >= before
            ):
                msg = "snapshot catalog did not advance"
                raise ValueError(msg)
            before = catalog.snapshots[-1].snapshot_id

    async def _regenerate(
        self, completion_timeout: float = DEFAULT_RELOAD_TIMEOUT
    ) -> None:
        await self._reload(
            lambda master: master.invoke(c.Regenerate(timeout=completion_timeout)),
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
        return await self._agent_call(
            lambda: self.agent(self.master).invoke(c.Whitelist(userid=userid))
        )

    async def _unwhitelist(self, userid: str) -> bool:
        await self._require_ready()
        return await self._agent_call(
            lambda: self.agent(self.master).invoke(c.Unwhitelist(userid=userid))
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

    async def _close(self) -> None:
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
                self._agent_call(
                    lambda agent=agent: agent.invoke(
                        c.Stop(timeout=AGENT_STOP_TIMEOUT)
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
        return True, await self._kill_agents(failed)

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
                    raise InvalidConfigurationError(read.revision, read.fields)
                self._configuration.validate_deployment(read.configuration)
                if not force and read.revision == self._prepared_revision:
                    break
                if not await self._all_stopped():
                    if not force and self._prepared_revision is None:
                        # Adopt live games without rewriting their shared Mod files.
                        break
                    msg = "all game processes must be stopped"
                    raise RuntimeError(msg)
                self._prepared_revision = None
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
                    raise InvalidConfigurationError(current.revision, current.fields)
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
                lambda agent: agent.invoke(c.Activate()),
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
                lambda agent: agent.invoke(c.Start(timeout=AGENT_START_TIMEOUT)),
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
        self._prepared_revision = None
        operation: _Operation[object]
        operation = (
            (lambda agent: agent.invoke(c.Kill(timeout=AGENT_KILL_TIMEOUT)))
            if force
            else (lambda agent: agent.invoke(c.Stop(timeout=AGENT_STOP_TIMEOUT)))
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

    async def _reload[T](
        self,
        operation: _Operation[T],
        completion_timeout: float,
        *,
        verify: Callable[[T], Awaitable[None]] | None = None,
    ) -> T:
        async with self._public_operation():
            await self._require_ready()
            agents = self._ordered_agents
            mutation_completed = False
            try:  # ruff: ignore[too-many-statements-in-try-clause]
                async with timeout_scope(completion_timeout):
                    markers = await self._gather(
                        agents,
                        lambda agent: agent.invoke(c.GenerationMarker()),
                    )
                    result = await self._agent_call(
                        lambda: operation(self.agent(self.master)),
                        limit=completion_timeout + RPC_TIMEOUT_MARGIN,
                    )
                    mutation_completed = True
                    await self._gather(
                        agents,
                        lambda agent: agent.invoke(
                            c.WaitGeneration(
                                cursor=markers[agent.name], timeout=completion_timeout
                            )
                        ),
                        limit=completion_timeout + RPC_TIMEOUT_MARGIN,
                    )
                    if verify is not None:
                        await verify(result)
            except Exception as error:
                if not mutation_completed and isinstance(error, KeyError | ValueError):
                    raise
                raise self._operation_error(
                    error,
                    mutation_completed=mutation_completed,
                ) from None
            return result

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

    def _schedule_reconcile(self) -> None:
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return
        self._reconcile_task = asyncio.create_task(
            self._reconcile(),
            name=f"dst-controller-{self.epoch}",
        )
        self._reconcile_task.add_done_callback(self._consume_task_result)

    def _consume_task_result(self, task: asyncio.Task[None]) -> None:
        if self._reconcile_task is task:
            self._reconcile_task = None
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
                        del batch, item
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


class ShardController(ShardAPI):
    def __init__(self, cluster: ClusterController, name: str) -> None:
        self.cluster = cluster
        self.name = name

    async def invoke[T](self, command: c.Request[T]) -> T:
        operation = c.operation("shard", command)
        match command:
            case c.Status():
                result = await self.cluster._shard_status(self.name)
            case c.Start() | c.Stop() | c.Restart() | c.Kill():
                async with self.cluster._public_operation():
                    running = isinstance(command, c.Start | c.Restart)
                    if running:
                        self.cluster._require_complete()
                    self.cluster._desired[self.name] = (
                        ShardDesired.RUNNING if running else ShardDesired.STOPPED
                    )
                    if running:
                        await self.cluster._prepare()
                    result = await self._call(command)
            case c.RegenerateShard():
                async with self.cluster._public_operation():
                    result = await self._call(command)
            case _:
                result = await self._call(command)
        return operation.response.validate_python(result, strict=True)

    async def _call[T](self, command: c.Request[T]) -> T:
        return await self.cluster._agent_call(
            lambda: self.cluster.agent(self.name).invoke(command),
            limit=command.timeout + RPC_TIMEOUT_MARGIN,
        )

    def subscribe_logs(self) -> Subscription[LogRecord]:
        return self.cluster._subscribe(self.cluster._shard_logs[self.name])

    def subscribe_lifecycle(self) -> Subscription[LifecycleRecord]:
        return self.cluster._subscribe(self.cluster._shard_lifecycle[self.name])

    def subscribe_events(self) -> Subscription[GameEventRecord]:
        return self.cluster._subscribe(self.cluster._shard_events[self.name])
