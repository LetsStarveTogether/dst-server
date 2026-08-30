import asyncio
from collections import deque
from pathlib import Path
from time import time_ns
from typing import TYPE_CHECKING

from logbook import Logger
from pydantic import JsonValue
from ulid import ULID

from dst_server.events.server import SavedEvent, SessionEvent
from dst_server.game import DriverHealth
from dst_server.models import Inventory, Mod, Player, Room, Runtime, ShardStatus, World
from dst_server.rpc.models import (
    GameEventRecord,
    LifecycleRecord,
    LogRecord,
    ShardRuntimeStatus,
)
from dst_server.rpc.models import ShardPhase as RpcShardPhase
from dst_server.runtime import Server, ServerConfig
from dst_server.telemetry import TelemetrySettings

from . import console, service
from .layout import Shard
from .subscriptions import Broadcast
from .supervisor import ShardPhase, ShardSupervisor, ShardSupervisorStatus

if TYPE_CHECKING:
    from dst_server.telemetry.otel import Pipeline

logger = Logger(__name__)
SAVED_EVENT_HISTORY = 64
_RPC_PHASE: dict[ShardPhase, RpcShardPhase] = {
    ShardPhase.UNAVAILABLE: "unavailable",
    ShardPhase.STOPPED: "stopped",
    ShardPhase.STARTING: "starting",
    ShardPhase.RUNNING: "running",
    ShardPhase.STOPPING: "stopping",
    ShardPhase.RETRY_WAIT: "retryWait",
    ShardPhase.FAILED: "failed",
}


class ShardAgent:  # ruff:ignore[too-many-public-methods]
    def __init__(
        self,
        shard: Shard,
        *,
        install_path: Path = service.DEFAULT_INSTALL_PATH,
        cluster_path: Path = service.DEFAULT_CLUSTER_PATH,
        external_port: int | None = None,
        telemetry: TelemetrySettings | None = None,
    ) -> None:
        self.shard = shard
        self.install_path = install_path
        self.cluster_path = cluster_path
        self.external_port = external_port
        self.incarnation = str(ULID())
        self.config: ServerConfig = service.create_server_config(
            install_path,
            cluster_path,
            shard,
            external_port=external_port,
            telemetry=telemetry,
        )
        self.logs = Broadcast[LogRecord]()
        self.lifecycle = Broadcast[LifecycleRecord]()
        self.game_events = Broadcast[GameEventRecord]()
        self._log_sequence = 0
        self._lifecycle_sequence = 0
        self._game_sequence = 0
        self._generation_sequence = 0
        self._saved: deque[tuple[int, str, SavedEvent]] = deque(
            maxlen=SAVED_EVENT_HISTORY,
        )
        self._save_markers: deque[tuple[int, str]] = deque(
            maxlen=SAVED_EVENT_HISTORY,
        )
        self._generation_markers: deque[tuple[int, str]] = deque(
            maxlen=SAVED_EVENT_HISTORY,
        )
        self._event_changed = asyncio.Condition()
        self._attempt_tasks: tuple[asyncio.Task[None], ...] = ()
        self._pipeline: Pipeline | None = None
        self._activated = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._fatal_error: BaseException | None = None
        self._fatal = asyncio.Event()
        self._failure_id: ULID | None = None
        self._started_at_ns: int | None = None
        self.failures: asyncio.Queue[ShardSupervisorStatus] = asyncio.Queue()
        self.supervisor = ShardSupervisor(
            shard.name,
            self._new_server,
            on_started=self._started,
            on_stopped=self._stopped,
            on_failed=self._failed,
        )

    @property
    def name(self) -> str:
        return self.shard.name

    @property
    def master(self) -> bool:
        return self.shard.master

    @property
    def server(self) -> Server:
        server = self.supervisor.server
        if (
            server is None
            or server.returncode is not None
            or self.supervisor.status.phase is not ShardPhase.RUNNING
        ):
            msg = f"DST shard is not running: {self.shard.name}"
            raise RuntimeError(msg)
        return server

    async def runtime_status(self) -> ShardRuntimeStatus:
        status = self.supervisor.status
        server = self.supervisor.server
        process = server.child if server is not None else None
        live = process is not None and process.returncode is None
        try:
            driver_health = server.driver_health if server is not None else None
        except RuntimeError:
            driver_health = None
        return ShardRuntimeStatus(
            name=self.shard.name,
            is_master=self.shard.master,
            desired=status.desired.value,
            phase=_RPC_PHASE[status.phase],
            agent_incarnation=ULID.from_str(self.incarnation),
            game_attempt=(
                ULID.from_str(server.game_events.nonce) if server is not None else None
            ),
            pid=process.pid if live else None,
            session_id=server.session_id if server is not None else None,
            ready=bool(server is not None and live and server.lifecycle.ready),
            returncode=status.returncode,
            retry_attempt=status.attempts,
            stable_since_ns=self._started_at_ns,
            driver_health=driver_health,
            driver_error=server.driver_error if server is not None else None,
            telemetry_profile=self.config.telemetry.profile,
            telemetry_invalid=server.telemetry_invalid if server is not None else 0,
            telemetry_dropped=server.telemetry_dropped if server is not None else 0,
            external_port=self.external_port,
            error_id=self._failure_id,
            error="DST shard failed" if self._failure_id is not None else None,
        )

    async def activate(self) -> None:
        service.activate_shard(
            self.install_path,
            self.cluster_path,
            self.shard,
        )
        if not self._activated:
            self._pipeline = service.configure_otel(
                self.config,
                instance_id=self.incarnation,
            )
        self._activated = True

    async def start(self) -> ShardSupervisorStatus:
        self._require_activated()
        self._failure_id = None
        return await self.supervisor.start()

    async def stop(self) -> ShardSupervisorStatus:
        return await self.supervisor.stop()

    async def restart(self) -> ShardSupervisorStatus:
        self._require_activated()
        self._failure_id = None
        return await self.supervisor.restart()

    async def kill(self) -> ShardSupervisorStatus:
        return await self.supervisor.kill()

    async def execute(self, command: str, completion_timeout: float = 30) -> str:
        return await self.server.execute(
            command,
            completion_timeout=completion_timeout,
        )

    async def execute_json(self, source: str) -> JsonValue:
        return await self.server.game.world.execute(source)

    async def health(self) -> DriverHealth:
        return await self.server.game.get_health()

    async def room(self) -> Room:
        return await self.server.game.world.room()

    async def world(self) -> World:
        return await self.server.game.world.state()

    async def runtime(self) -> Runtime:
        return await self.server.game.world.runtime()

    async def mods(self) -> tuple[Mod, ...]:
        return await self.server.game.world.mods()

    async def connected_shards(self) -> tuple[ShardStatus, ...]:
        return await self.server.game.world.shards()

    async def pause(self, paused: bool) -> bool:
        return await self.server.game.world.pause(paused)

    async def regenerate_shard(
        self,
        *,
        preserve_settings: bool = True,
        completion_timeout: float = 30,
    ) -> None:
        await self.server.game.world.regenerate_shard(
            preserve_settings=preserve_settings,
            completion_timeout=completion_timeout,
        )

    async def list_players(self) -> tuple[Player, ...]:
        return await self.server.game.players.list()

    async def get_player(self, userid: str) -> Player | None:
        return await self.server.game.players.get(userid)

    async def inventory(self, userid: str) -> Inventory | None:
        return await self.server.game.players.inventory(userid)

    async def kick(self, userid: str) -> None:
        await self.server.game.players.kick(userid)

    async def ban(self, userid: str, *, seconds: int | None = None) -> None:
        await self.server.game.players.ban(userid, seconds=seconds)

    async def blocklist(self) -> tuple[str, ...]:
        return await self.server.game.players.blocklist()

    async def is_blocked(self, userid: str) -> bool:
        return await self.server.game.players.is_blocked(userid)

    async def unban(self, userid: str) -> bool:
        return await self.server.game.players.unban(userid)

    async def is_admin(self, userid: str) -> bool | None:
        return await self.server.game.players.is_admin(userid)

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
        return await self.server.game.players.set_vitals(
            userid,
            health=health,
            hunger=hunger,
            sanity=sanity,
            temperature=temperature,
            moisture=moisture,
        )

    async def kill_player(self, userid: str) -> bool:
        return await self.server.game.players.kill(userid)

    async def revive(self, userid: str) -> bool:
        return await self.server.game.players.revive(userid)

    async def despawn(self, userid: str) -> bool:
        return await self.server.game.players.despawn(userid)

    async def migrate(
        self,
        userid: str,
        shard_id: str,
        portal_id: int = 1,
    ) -> bool:
        return await self.server.game.players.migrate(userid, shard_id, portal_id)

    async def teleport(
        self,
        userid: str,
        x: float,
        y: float,
        z: float,
    ) -> bool:
        return await self.server.game.players.teleport(userid, x, y, z)

    async def give(self, userid: str, item: str, count: int = 1) -> int:
        return await self.server.game.players.give(userid, item, count)

    async def remove(self, userid: str, item: str, count: int = 1) -> int:
        return await self.server.game.players.remove(userid, item, count)

    async def is_whitelisted(self, userid: str) -> bool:
        return await self.server.game.players.is_whitelisted(userid)

    async def whitelist(self, userid: str) -> bool:
        return await self.server.game.players.whitelist(userid)

    async def unwhitelist(self, userid: str) -> bool:
        return await self.server.game.players.unwhitelist(userid)

    async def announce(self, message: str) -> None:
        await self.server.game.world.announce(message)

    async def reset(self, completion_timeout: float = 30) -> None:
        await self.server.game.world.reset(completion_timeout=completion_timeout)

    async def rollback(self, count: int = 1, completion_timeout: float = 30) -> None:
        await self.server.game.world.rollback(
            count,
            completion_timeout=completion_timeout,
        )

    async def regenerate(self, completion_timeout: float = 30) -> None:
        await self.server.game.world.regenerate(
            completion_timeout=completion_timeout,
        )

    async def save_marker(self) -> int:
        marker = self._lifecycle_sequence
        self._save_markers.append((marker, self.server.game_events.nonce))
        return marker

    async def save(self, completion_timeout: float = 30) -> SavedEvent:
        return await self.server.save(completion_timeout=completion_timeout)

    async def wait_saved(
        self,
        after_sequence: int,
        snapshot: int | None,
        completion_timeout: float = 30,
    ) -> SavedEvent:
        attempt = next(
            (
                value
                for marker, value in reversed(self._save_markers)
                if marker == after_sequence
            ),
            None,
        )
        if attempt is None:
            msg = "unknown save marker"
            raise ValueError(msg)
        async with asyncio.timeout(completion_timeout), self._event_changed:
            while True:
                match = next(
                    (
                        event
                        for sequence, event_attempt, event in self._saved
                        if sequence > after_sequence
                        and event_attempt == attempt
                        and (snapshot is None or event.snapshot == snapshot)
                    ),
                    None,
                )
                if match is not None:
                    return match
                self._require_attempt(attempt)
                await self._event_changed.wait()

    async def generation_marker(self) -> int:
        marker = self._generation_sequence
        self._generation_markers.append((marker, self.server.game_events.nonce))
        return marker

    async def wait_generation(
        self,
        after_sequence: int,
        completion_timeout: float = 30,
    ) -> int:
        attempt = next(
            (
                value
                for marker, value in reversed(self._generation_markers)
                if marker == after_sequence
            ),
            None,
        )
        if attempt is None:
            msg = "unknown generation marker"
            raise ValueError(msg)
        async with asyncio.timeout(completion_timeout):
            async with self._event_changed:
                while self._generation_sequence <= after_sequence:
                    self._require_attempt(attempt)
                    await self._event_changed.wait()
            server = self._require_attempt(attempt)
            await server.driver.wait_ready()
        return self._generation_sequence

    async def wait_fatal(self) -> None:
        await self._fatal.wait()
        if self._fatal_error is not None:
            raise self._fatal_error

    async def next_failure(self) -> ShardSupervisorStatus:
        return await self.failures.get()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            try:
                await self.supervisor.aclose()
            finally:
                tasks, self._attempt_tasks = self._attempt_tasks, ()
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                self.logs.close()
                self.lifecycle.close()
                self.game_events.close()
                self.failures.shutdown(immediate=True)
                pipeline, self._pipeline = self._pipeline, None
                if pipeline is not None:
                    await pipeline.shutdown()
            self._closed = True

    def _new_server(self) -> Server:
        server = Server(self.config)
        server.log_handler = lambda line: self._log(server, line)
        return server

    def _require_activated(self) -> None:
        if not self._activated:
            msg = f"DST shard is not prepared: {self.shard.name}"
            raise RuntimeError(msg)

    def _require_attempt(self, attempt: str) -> Server:
        server = self.supervisor.server
        if (
            server is None
            or server.returncode is not None
            or server.game_events.nonce != attempt
        ):
            msg = f"DST shard attempt changed: {self.shard.name}"
            raise RuntimeError(msg)
        return server

    def _log(self, server: Server, line: str) -> None:
        logger.info("{shard}: {line}", shard=self.shard.name, line=line)
        self._log_sequence += 1
        attempt = ULID.from_str(server.game_events.nonce)
        self.logs.publish(
            LogRecord(
                shard=self.shard.name,
                game_attempt=attempt,
                sequence=self._log_sequence,
                observed_timestamp_ns=time_ns(),
                line=line,
            )
        )

    async def _started(self, server: Server) -> None:
        self._failure_id = None
        self._started_at_ns = time_ns()
        tasks = (
            asyncio.create_task(
                console.forward(self.shard.console, server),
                name=f"dst-fifo-{self.shard.name}",
            ),
            asyncio.create_task(
                self._drain_lifecycle(server),
                name=f"dst-lifecycle-relay-{self.shard.name}",
            ),
            asyncio.create_task(
                self._drain_game_events(server),
                name=f"dst-game-event-relay-{self.shard.name}",
            ),
        )
        self._attempt_tasks = tasks
        for index, task in enumerate(tasks):
            task.add_done_callback(
                lambda completed, server=server, critical=index == 1: (
                    self._background_done(
                        server,
                        completed,
                        critical=critical,
                    )
                )
            )

    async def _stopped(self, _server: Server) -> None:
        self._started_at_ns = None
        tasks, self._attempt_tasks = self._attempt_tasks, ()
        if tasks and not tasks[0].done():
            tasks[0].cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._event_changed:
            self._event_changed.notify_all()

    async def _failed(self, status: ShardSupervisorStatus) -> None:
        self._failure_id = ULID()
        self.failures.put_nowait(status)

    def _background_done(
        self,
        server: Server,
        task: asyncio.Task[None],
        *,
        critical: bool,
    ) -> None:
        if task.cancelled() or self._fatal.is_set():
            return
        error = task.exception()
        if (
            self.supervisor.server is not server
            or server.returncode is not None
            or self.supervisor.status.phase
            not in {ShardPhase.STARTING, ShardPhase.RUNNING}
        ):
            return
        if not critical:
            logger.error(
                "non-critical shard background task stopped: {shard}: {task}: {kind}",
                shard=self.shard.name,
                task=task.get_name(),
                kind=type(error).__name__ if error is not None else "unexpected exit",
            )
            return
        self._fatal_error = RuntimeError(
            f"shard background task failed: {self.shard.name}"
        )
        self._fatal.set()

    async def _drain_lifecycle(self, server: Server) -> None:
        attempt = ULID.from_str(server.game_events.nonce)
        while (event := await server.read_event()) is not None:
            self._lifecycle_sequence += 1
            sequence = self._lifecycle_sequence
            self.lifecycle.publish(
                LifecycleRecord(
                    shard=self.shard.name,
                    game_attempt=attempt,
                    sequence=sequence,
                    observed_timestamp_ns=time_ns(),
                    event=event,
                )
            )
            async with self._event_changed:
                if isinstance(event, SavedEvent):
                    self._saved.append((sequence, str(attempt), event))
                if isinstance(event, SessionEvent):
                    self._generation_sequence += 1
                self._event_changed.notify_all()

    async def _drain_game_events(self, server: Server) -> None:
        attempt = ULID.from_str(server.game_events.nonce)
        event_logger = self._pipeline.logger if self._pipeline is not None else None
        while (observed := await server.read_game_event()) is not None:
            if event_logger is not None:
                from dst_server.telemetry.otel import emit

                try:
                    emit(
                        event_logger, observed, attributes=server.recorder.attributes()
                    )
                except Exception:
                    logger.exception(
                        "{shard}: OpenTelemetry event export failed; "
                        "using local event logging",
                        shard=server.config.shard,
                    )
                    event_logger = None
            if event_logger is None:
                logger.info(
                    "{shard}: DST_EVENT|{event}",
                    shard=server.config.shard,
                    event=observed.record.model_dump_json(),
                )
            self._game_sequence += 1
            self.game_events.publish(
                GameEventRecord(
                    shard=self.shard.name,
                    game_attempt=attempt,
                    sequence=self._game_sequence,
                    observed_timestamp_ns=observed.observed_timestamp_ns,
                    event=observed.record,
                )
            )
