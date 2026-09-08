import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from time import time_ns
from typing import TYPE_CHECKING, Any

from logbook import Logger
from ulid import ULID

from dst_server import commands as c
from dst_server.concurrency import cancel_tasks, complete
from dst_server.configuration.files import Shard
from dst_server.errors import IndeterminateError
from dst_server.events.server import SavedEvent, SessionEvent
from dst_server.models.cluster import (
    GameEventRecord,
    LifecycleRecord,
    LogRecord,
    ObservationCursor,
    ShardPhase,
    ShardRuntimeStatus,
)
from dst_server.models.snapshot import Snapshot, SnapshotCatalog, WorldSnapshotMetadata
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.supervisor import ShardSupervisor, ShardSupervisorStatus
from dst_server.telemetry import TelemetrySettings
from dst_server.telemetry.recorder import Recorder
from dst_server.timeouts import (
    DEFAULT_RELOAD_TIMEOUT,
    DEFAULT_SAVE_TIMEOUT,
    positive_timeout,
    timeout_scope,
)

from . import console, service
from .subscriptions import Broadcast

if TYPE_CHECKING:
    from dst_server.telemetry.otel import Pipeline

logger = Logger(__name__)
SAVED_EVENT_HISTORY = 64


class ShardAgent:
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
        self._saved_floor = 0
        self._event_changed = asyncio.Condition()
        self._attempt_tasks: tuple[asyncio.Task[None], ...] = ()
        self._fifo_task: asyncio.Task[None] | None = None
        self._pipeline: Pipeline | None = None
        self._activated = False
        self._close_task: asyncio.Task[None] | None = None
        self._fatal_error: BaseException | None = None
        self._fatal = asyncio.Event()
        self._failure_id: ULID | None = None
        self._started_at_ns: int | None = None
        self.failures: asyncio.Queue[ShardSupervisorStatus] = asyncio.Queue(maxsize=1)
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
            desired=status.desired,
            phase=status.phase,
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
            telemetry_delivery=(
                self._pipeline.status() if self._pipeline is not None else None
            ),
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

    async def invoke[T](self, command: c.Request[T]) -> T:  # ruff: ignore[complex-structure]
        operation = c.operation("agent", command)
        async with timeout_scope(command.timeout):
            match command:
                case c.Status():
                    result = await self.runtime_status()
                case c.Activate():
                    result = await self.activate()
                case c.Start() | c.Stop() | c.Restart() | c.Kill():
                    lifecycle: dict[
                        type[c.Request[Any]],
                        Callable[[], Awaitable[ShardSupervisorStatus]],
                    ] = {
                        c.Start: self.start,
                        c.Stop: self.stop,
                        c.Restart: self.restart,
                        c.Kill: self.kill,
                    }
                    await lifecycle[type(command)]()
                    result = None
                case c.Execute(source=source):
                    result = await self.server.execute(
                        source, completion_timeout=command.timeout
                    )
                case c.Save():
                    result = await self.server.save(completion_timeout=command.timeout)
                case c.Snapshots(limit=limit, before=before):
                    result = await self.list_snapshots(limit, before=before)
                case c.SaveMarker():
                    result = await self.save_marker()
                case c.WaitSaved(cursor=cursor, snapshot=snapshot):
                    result = await self.wait_saved(cursor, snapshot, command.timeout)
                case c.GenerationMarker():
                    result = await self.generation_marker()
                case c.WaitGeneration(cursor=cursor):
                    result = await self.wait_generation(cursor, command.timeout)
                case _:
                    result = await self.server.game.invoke(command)
        return operation.response.validate_python(result, strict=True)

    async def list_snapshots(
        self, limit: int = 100, before: int | None = None
    ) -> SnapshotCatalog:
        server = self.server
        catalog = await server.game.invoke(c.Snapshots(limit=limit, before=before))
        result = await asyncio.to_thread(self._read_snapshot_metadata, catalog)
        if self.server is not server or server.session_id != catalog.session_id:
            msg = "world session changed while reading snapshots"
            raise RuntimeError(msg)
        return result

    def _read_snapshot_metadata(self, catalog: SnapshotCatalog) -> SnapshotCatalog:
        snapshots: list[Snapshot] = []
        root = self.cluster_path / self.name / "save"
        for snapshot in catalog.snapshots:
            if snapshot.world_file is None:
                snapshots.append(snapshot.replace(metadata=None))
                continue
            path = PurePosixPath(snapshot.world_file)
            if ".." in path.parts or path.parts != (
                "session",
                catalog.session_id,
                f"{snapshot.snapshot_id:010d}",
            ):
                msg = "native snapshot path does not match its session and ID"
                raise ValueError(msg)
            source = root.joinpath(*path.parts)
            metadata_path = source.with_suffix(".meta")
            if any(
                part.is_symlink()
                for part in (
                    self.cluster_path,
                    root.parent,
                    root,
                    source.parent.parent,
                    source.parent,
                    source,
                    metadata_path,
                )
            ):
                msg = "snapshot paths cannot be symlinks"
                raise ValueError(msg)
            metadata = (
                WorldSnapshotMetadata.load(metadata_path)
                if source.is_file() and metadata_path.exists()
                else None
            )
            snapshots.append(snapshot.replace(metadata=metadata))
        return catalog.replace(snapshots=tuple(snapshots))

    async def save_marker(self) -> ObservationCursor:
        return ObservationCursor(
            attempt=ULID.from_str(self.server.game_events.nonce),
            sequence=self._lifecycle_sequence,
        )

    async def wait_saved(
        self,
        cursor: ObservationCursor,
        snapshot: int | None,
        completion_timeout: float = DEFAULT_SAVE_TIMEOUT,
    ) -> SavedEvent:
        if cursor.sequence > self._lifecycle_sequence:
            msg = "future save cursor"
            raise ValueError(msg)
        attempt = str(cursor.attempt)
        async with (
            timeout_scope(positive_timeout(completion_timeout)),
            self._event_changed,
        ):
            while True:
                if cursor.sequence < self._saved_floor:
                    raise IndeterminateError
                match = next(
                    (
                        event
                        for sequence, event_attempt, event in self._saved
                        if sequence > cursor.sequence
                        and event_attempt == attempt
                        and (snapshot is None or event.snapshot == snapshot)
                    ),
                    None,
                )
                if match is not None:
                    return match
                self._require_attempt(attempt)
                await self._event_changed.wait()

    async def generation_marker(self) -> ObservationCursor:
        return ObservationCursor(
            attempt=ULID.from_str(self.server.game_events.nonce),
            sequence=self._generation_sequence,
        )

    async def wait_generation(
        self,
        cursor: ObservationCursor,
        completion_timeout: float = DEFAULT_RELOAD_TIMEOUT,
    ) -> int:
        if cursor.sequence > self._generation_sequence:
            msg = "future generation cursor"
            raise ValueError(msg)
        attempt = str(cursor.attempt)
        async with timeout_scope(positive_timeout(completion_timeout)):
            async with self._event_changed:
                while self._generation_sequence <= cursor.sequence:
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
        task = self._close_task
        if task is None:
            task = self._close_task = asyncio.create_task(
                self._close(), name=f"dst-agent-close-{self.shard.name}"
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
        if self._fatal_error is not None:
            raise self._fatal_error

    async def _close(self) -> None:
        errors: list[BaseException] = []
        try:
            await self.supervisor.aclose()
        except BaseException as error:
            errors.append(error)
        fifo, self._fifo_task = self._fifo_task, None
        if fifo is not None:
            await cancel_tasks(fifo)
        tasks, self._attempt_tasks = self._attempt_tasks, ()
        await cancel_tasks(*tasks)
        self.logs.close()
        self.lifecycle.close()
        self.game_events.close()
        self.failures.shutdown(immediate=True)
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            try:
                await pipeline.shutdown()
            except BaseException as error:
                errors.append(error)
        if self._fatal_error is not None and self._fatal_error not in errors:
            errors.append(self._fatal_error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            message = "failed to close shard agent"
            raise BaseExceptionGroup(message, errors)

    def _new_server(self) -> Server:
        pipeline = self._pipeline
        recorder = Recorder(
            self.config.telemetry_cluster or self.config.cluster,
            self.name,
            meter_provider=pipeline.meter_provider if pipeline is not None else None,
            tracer_provider=pipeline.tracer_provider if pipeline is not None else None,
        )
        server = Server(self.config, recorder=recorder)
        server.log_handler = lambda line: self._log(server, line)
        self._attempt_tasks = (
            asyncio.create_task(
                self._drain_lifecycle(server),
                name=f"dst-lifecycle-relay-{self.shard.name}",
            ),
            asyncio.create_task(
                self._drain_game_events(server),
                name=f"dst-game-event-relay-{self.shard.name}",
            ),
            asyncio.create_task(
                self._drain_operational(server),
                name=f"dst-operational-relay-{self.shard.name}",
            ),
        )
        for task in self._attempt_tasks:
            task.add_done_callback(
                lambda completed: self._background_done(
                    server, completed, critical=True
                )
            )
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
        self._fifo_task = asyncio.create_task(
            console.forward(self.shard.console, server),
            name=f"dst-fifo-{self.shard.name}",
        )
        self._fifo_task.add_done_callback(
            lambda completed: self._background_done(server, completed, critical=False)
        )

    async def _stopped(self, _server: Server) -> None:
        self._started_at_ns = None
        fifo, self._fifo_task = self._fifo_task, None
        if fifo is not None:
            await cancel_tasks(fifo)
        tasks, self._attempt_tasks = self._attempt_tasks, ()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._event_changed:
            self._event_changed.notify_all()

    async def _failed(self, status: ShardSupervisorStatus) -> None:
        self._failure_id = ULID()
        # Failure reports wake reconciliation, which reads the current status.
        if self.failures.full():
            self.failures.get_nowait()
        self.failures.put_nowait(status)

    def _background_done(
        self,
        server: Server,
        task: asyncio.Task[None],
        *,
        critical: bool,
    ) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if self._fatal.is_set():
            return
        if not (critical and error is not None) and (
            self.supervisor.server is not server
            or (
                error is None
                and (
                    server.returncode is not None
                    or self.supervisor.status.phase
                    not in {ShardPhase.STARTING, ShardPhase.RUNNING}
                )
            )
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
        while (observed := await server.read_lifecycle_event()) is not None:
            event = observed.event
            self._lifecycle_sequence += 1
            sequence = self._lifecycle_sequence
            self.lifecycle.publish(
                LifecycleRecord(
                    shard=self.shard.name,
                    game_attempt=attempt,
                    sequence=sequence,
                    observed_timestamp_ns=observed.observed_timestamp_ns,
                    event=event,
                )
            )
            async with self._event_changed:
                if isinstance(event, SavedEvent):
                    if len(self._saved) == SAVED_EVENT_HISTORY:
                        self._saved_floor = self._saved[0][0]
                    self._saved.append((sequence, str(attempt), event))
                if isinstance(event, SessionEvent):
                    self._generation_sequence += 1
                self._event_changed.notify_all()
            del observed, event

    async def _drain_game_events(self, server: Server) -> None:
        attempt = ULID.from_str(server.game_events.nonce)
        while (observed := await server.read_game_event()) is not None:
            if self._pipeline is not None and self._pipeline.logs_enabled:
                await self._pipeline.emit_event(
                    observed, attributes=server.recorder.attributes()
                )
            else:
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
            del observed

    async def _drain_operational(self, server: Server) -> None:
        while (record := await server.read_operational_event()) is not None:
            if self._pipeline is not None and self._pipeline.logs_enabled:
                await self._pipeline.emit_operational(
                    event_name=record.event_name,
                    body=record.body,
                    observed_timestamp_ns=record.observed_timestamp_ns,
                    severity_text=record.severity_text,
                    attributes=server.recorder.attributes()
                    | {
                        "log.record.uid": record.uid,
                        "dst.game.attempt.id": server.game_events.nonce,
                    },
                )
            else:
                logger.info(
                    "{shard}: {event}: {body}",
                    shard=server.config.shard,
                    event=record.event_name,
                    body=record.body,
                )
            del record
