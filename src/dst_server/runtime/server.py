import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import time_ns
from typing import Self

from logbook import Logger
from pydantic import JsonValue
from ulid import ULID

from dst_server.concurrency import cancel_tasks, complete
from dst_server.events import ObservedGameEvent
from dst_server.events import server as server_events
from dst_server.game import GameClient
from dst_server.models.driver import DriverHealth
from dst_server.telemetry.recorder import Recorder
from dst_server.telemetry.stream import EventStream
from dst_server.timeouts import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_SAVE_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    OUTPUT_DRAIN_TIMEOUT,
    positive_timeout,
)
from dst_server.timeouts import operation_deadline as _operation_deadline
from dst_server.timeouts import timeout_scope as _timeout_scope

from .config import ServerConfig
from .console import (
    Console,
    StaleGenerationError,
)
from .driver import Driver
from .fds import open_pipes, open_reader, open_writer, read_line
from .lifecycle import Lifecycle, ObservedLifecycleEvent
from .operational import (
    NATIVE_TIMESTAMP,
    OperationalRecord,
    classify_log,
    lifecycle_body,
)
from .request import RequestState, track_request

FD_LAUNCHER = Path(__file__).with_name("fds.py")
SUBPROCESS_STREAM_LIMIT = 1024 * 1024
logger = Logger(__name__)

type LogHandler = Callable[[str], None]


class Server:  # ruff:ignore[too-many-public-methods]
    def __init__(
        self,
        config: ServerConfig,
        *,
        log_handler: LogHandler | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self.config = config
        self.log_handler = log_handler
        self.child: asyncio.subprocess.Process | None = None
        self.console: Console | None = None
        self.read_transports: tuple[asyncio.ReadTransport, ...] = ()
        self.finish_lock = asyncio.Lock()
        self.lifecycle = Lifecycle()
        self.lifecycle_task: asyncio.Task[None] | None = None
        self.log_task: asyncio.Task[None] | None = None
        self.closed = False
        self.operational: asyncio.Queue[OperationalRecord] = asyncio.Queue(maxsize=1024)
        self._termination_requested = False
        self._exit_observed = False
        self._input_error: BaseException | None = None
        self.recorder = (
            recorder
            if recorder is not None
            else Recorder(
                config.telemetry_cluster or config.cluster,
                config.shard,
            )
        )
        self.game_events = EventStream(self.recorder)
        self.driver = Driver(self.install_driver, config.cluster, config.shard)
        self._driver_error: str | None = None
        self._generation_changed = asyncio.Event()
        self.game = GameClient(
            shard=config.shard,
            lua_directory=config.lua_directory,
            telemetry=config.telemetry,
            execute=self.execute,
            execute_ready=self._execute_ready,
            execute_reload=self._execute_reload,
            wait_reload=self._wait_reload,
            recorder=self.recorder,
            session_id=lambda: self.session_id,
            nonce=self.game_events.nonce,
            observe_health=self.driver.observe_health,
        )

    @property
    def process(self) -> asyncio.subprocess.Process:
        if self.child is None:
            msg = "DST server has not been started"
            raise RuntimeError(msg)
        return self.child

    @property
    def returncode(self) -> int | None:
        return None if self.child is None else self.child.returncode

    @property
    def session_id(self) -> str | None:
        return self.lifecycle.session_id

    @property
    def driver_health(self) -> DriverHealth:
        return self.driver.health

    @property
    def driver_error(self) -> str | None:
        return self._driver_error

    @property
    def telemetry_invalid(self) -> int:
        return self.game_events.invalid

    @property
    def telemetry_dropped(self) -> int:
        return self.game_events.dropped

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        try:
            await self.stop()
        except TimeoutError:
            await self.kill()
            raise

    async def start(self, startup_timeout: float = DEFAULT_STARTUP_TIMEOUT) -> None:
        with self.recorder.operation("start", self.session_id) as span:
            if self.child is not None or self.closed:
                msg = "DST server process objects are single-use"
                raise RuntimeError(msg)
            try:
                timeout = positive_timeout(startup_timeout)
                async with _timeout_scope(timeout):
                    await self._start_process()
            except BaseException:
                self.recorder.set_process_up(False)
                if self.child is None:
                    await self.finish()
                else:
                    await self.kill()
                raise
            span.set_attribute("process.pid", self.process.pid)
            if self.session_id is not None:
                span.set_attribute("dst.session.id", self.session_id)

    async def _start_process(self) -> None:
        parent_pid = os.getpid() if self.config.monitor_parent_process else None
        command = self.config.command(monitor_parent_process=parent_pid)
        parent_fds, server_fds = open_pipes()
        transports: list[asyncio.BaseTransport] = []
        try:  # ruff:ignore[too-many-statements-in-try-clause]
            command_writer = await open_writer(parent_fds.pop(0))
            transports.append(command_writer.transport)
            result_reader, result_transport = await open_reader(parent_fds.pop(0))
            transports.append(result_transport)
            event_reader, event_transport = await open_reader(parent_fds.pop(0))
            transports.append(event_transport)
            logger.info(
                "start DST server: {cluster}/{shard}",
                cluster=self.config.cluster,
                shard=self.config.shard,
            )
            self.child = await asyncio.create_subprocess_exec(
                sys.executable,
                str(FD_LAUNCHER),
                *(str(descriptor) for descriptor in server_fds),
                *command,
                cwd=self.config.executable.parent,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=SUBPROCESS_STREAM_LIMIT,
                pass_fds=server_fds,
            )
            self.recorder.set_process_up(True)
            await self._observe_operational(
                "dst.server.process_started", {"pid": self.child.pid}
            )
        except BaseException:
            for transport in transports:
                transport.close()
            for descriptor in parent_fds:
                os.close(descriptor)
            raise
        finally:
            for descriptor in server_fds:
                os.close(descriptor)

        self.console = Console(command_writer, result_reader, self.game_events)
        self.read_transports = (result_transport, event_transport)
        self.lifecycle_task = asyncio.create_task(
            self._pump_lifecycle(event_reader),
            name=f"dst-events-{self.config.shard}",
        )
        stdout = self.process.stdout
        if stdout is None:
            msg = "DST server stdout pipe is unavailable"
            raise RuntimeError(msg)
        self.log_task = asyncio.create_task(
            self.pump_logs(stdout),
            name=f"dst-logs-{self.config.shard}",
        )
        for task in (self.lifecycle_task, self.log_task):
            task.add_done_callback(self._input_finished)
        await self.wait_ready()
        try:
            await self.driver.install(self.lifecycle.session_generation)
        except Exception:
            if self.process.returncode is not None or self.lifecycle.eof:
                raise
            logger.exception(
                "failed to install DST Lua driver; game remains running: "
                "{cluster}/{shard}",
                cluster=self.config.cluster,
                shard=self.config.shard,
            )

    async def _pump_lifecycle(self, reader: asyncio.StreamReader) -> None:
        try:
            await self.lifecycle.pump(
                reader, self._session_started, self._observe_lifecycle
            )
        finally:
            self._generation_changed.set()

    def _input_finished(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task.cancelled() or (error := task.exception()) is None:
            return
        if self._input_error is None:
            self._input_error = error
        self.lifecycle.close()
        self._generation_changed.set()

    def _session_started(self, generation: int) -> None:
        token = _operation_deadline.set(None)
        try:
            self.driver.session_started(generation)
        finally:
            _operation_deadline.reset(token)
        changed = self._generation_changed
        self._generation_changed = asyncio.Event()
        changed.set()

    async def execute(
        self,
        command: str,
        completion_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        timeout = positive_timeout(completion_timeout)
        async with _timeout_scope(timeout):
            return await self._execute(command)

    async def _execute_ready(self, command: str) -> str:
        async with _timeout_scope(DEFAULT_COMMAND_TIMEOUT):
            while True:
                generation = await self.driver.wait_ready()
                try:
                    return await self._execute(
                        command,
                        lambda generation=generation: self.driver.is_ready(generation),
                    )
                except StaleGenerationError:
                    continue

    async def _execute_reload(
        self,
        command: str,
        completion_timeout: float,
    ) -> tuple[str, int, float]:
        timeout = positive_timeout(completion_timeout)
        async with _timeout_scope(timeout) as deadline:
            while True:
                generation = await self.driver.wait_ready()
                with track_request() as request_state:
                    try:
                        result = await self._execute(
                            command,
                            lambda generation=generation, request_state=request_state: (
                                request_state.sent or self.driver.is_ready(generation)
                            ),
                        )
                    except StaleGenerationError:
                        if request_state.sent:
                            raise
                        continue
                return result, generation, deadline

    async def _wait_reload(self, generation: int, deadline: float) -> None:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        async with asyncio.timeout_at(deadline):
            while self.driver.generation <= generation:
                if self.lifecycle.eof:
                    msg = "DST event stream closed before reload completed"
                    raise EOFError(msg)
                changed = self._generation_changed
                if self.driver.generation <= generation:
                    await changed.wait()
            await self.driver.wait_ready()

    async def _execute(
        self,
        command: str,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> str:
        with self.recorder.operation("console.execute", self.session_id):
            await self.wait_ready()
            if self.process.returncode is not None:
                msg = f"DST server exited with status {self.process.returncode}"
                raise RuntimeError(msg)
            if self.console is None:
                msg = "DST console is unavailable"
                raise RuntimeError(msg)
            if __debug__:
                logger.debug("DST console command : {command}", command=command)
            result = await self.console.execute(
                command,
                generation_is_current,
                completion_deadline=_operation_deadline.get(),
            )
            if __debug__:
                logger.debug("DST console result : {result}", result=result)
            return result

    async def wait_ready(self) -> None:
        if self.child is None:
            msg = "DST server has not been started"
            raise RuntimeError(msg)
        await self.lifecycle.wait_ready()

    async def read_event(self) -> server_events.Event | None:
        return await self.lifecycle.read()

    async def read_lifecycle_event(self) -> ObservedLifecycleEvent | None:
        return await self.lifecycle.read_observed()

    async def read_game_event(self) -> ObservedGameEvent | None:
        observed = await self.game_events.read()
        if observed is not None:
            self.driver.observe_event(observed.record)
        return observed

    async def read_operational_event(self) -> OperationalRecord | None:
        try:
            return await self.operational.get()
        except asyncio.QueueShutDown:
            return None

    async def _observe_operational(
        self,
        event_name: str,
        body: dict[str, JsonValue],
        severity_text: str = "INFO",
        observed_timestamp_ns: int | None = None,
    ) -> None:
        await self.operational.put(
            OperationalRecord(
                uid=str(ULID()),
                event_name=event_name,
                body=body,
                severity_text=severity_text,
                observed_timestamp_ns=(
                    time_ns()
                    if observed_timestamp_ns is None
                    else observed_timestamp_ns
                ),
            )
        )

    async def _observe_lifecycle(
        self, event: server_events.Event, observed_timestamp_ns: int
    ) -> None:
        body = lifecycle_body(event)
        if body is not None:
            await self._observe_operational(
                f"dst.server.{event.event}",
                body,
                observed_timestamp_ns=observed_timestamp_ns,
            )

    async def install_driver(self, generation: int) -> DriverHealth:
        try:
            async with _timeout_scope(DEFAULT_STARTUP_TIMEOUT):
                health = await self.game.install(generation)
        except Exception as error:
            self._driver_error = str(error) or type(error).__name__
            await self._observe_operational(
                "dst.runtime.diagnostic",
                {"kind": "driver_install_failed", "generation": generation},
                "ERROR",
            )
            raise
        self._driver_error = None
        if health.telemetry_status == "failed":
            logger.warning(
                "failed to install DST telemetry: "
                "{cluster}/{shard} ({profile}): {error}",
                cluster=self.config.cluster,
                shard=self.config.shard,
                profile=self.config.telemetry.profile,
                error=health.last_error,
            )
            await self._observe_operational(
                "dst.runtime.diagnostic",
                {"kind": "telemetry_install_failed", "generation": generation},
                "ERROR",
            )
        return health

    async def save(
        self, completion_timeout: float = DEFAULT_SAVE_TIMEOUT
    ) -> server_events.SavedEvent:
        return await self._save(
            self.game.request_save,
            completion_timeout,
            RequestState(),
        )

    async def _save(
        self,
        request: Callable[[], Awaitable[None]],
        completion_timeout: float,
        request_state: RequestState,
    ) -> server_events.SavedEvent:
        with self.recorder.operation("save", self.session_id) as span:
            timeout = positive_timeout(completion_timeout)
            async with _timeout_scope(timeout):
                with track_request(request_state):
                    event = await self.lifecycle.wait_for_save(
                        request,
                        timeout,
                        request_state,
                    )
            if event.snapshot is not None:
                span.set_attribute("dst.snapshot", event.snapshot)
            return event

    async def stop(self, grace_period: float = DEFAULT_STOP_TIMEOUT) -> int:
        with self.recorder.operation("stop", self.session_id):
            process = self.process
            grace_period = positive_timeout(grace_period)
            if process.returncode is not None:
                return await complete(self.wait())

            logger.info(
                "stop DST server: {cluster}/{shard}",
                cluster=self.config.cluster,
                shard=self.config.shard,
            )
            self._termination_requested = True
            process.terminate()
            exited = asyncio.create_task(process.wait())
            stopping = asyncio.create_task(self.lifecycle.stopping.wait())
            tasks = (exited, stopping)
            try:
                try:
                    done, _ = await asyncio.wait(
                        tasks,
                        timeout=grace_period,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    await cancel_tasks(*tasks)
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.kill()
                await complete(self.wait())
                raise
            if not done:
                msg = "DST server did not report DST_Stopping; process left running"
                raise TimeoutError(msg)
            if process.returncode is None:
                process.kill()
            return await complete(self.wait())

    async def kill(self) -> int:
        with self.recorder.operation("kill", self.session_id):
            process = self.process
            if process.returncode is None:
                self._termination_requested = True
                process.kill()
            return await complete(self.wait())

    async def wait(self) -> int:
        with self.recorder.operation("wait", self.session_id) as span:
            process = self.process
            returncode = await process.wait()
            await self.finish()
            span.set_attribute("process.exit.code", returncode)
            logger.info(
                "DST server exited: {cluster}/{shard} ({returncode})",
                cluster=self.config.cluster,
                shard=self.config.shard,
                returncode=returncode,
            )
            return returncode

    async def pump_logs(  # ruff:ignore[complex-structure]
        self, reader: asyncio.StreamReader
    ) -> None:
        handler_failed = False
        while True:
            raw_line, oversized = await read_line(reader)
            if raw_line is None:
                break
            if oversized:
                continue
            observed_timestamp_ns = time_ns()
            raw_line = raw_line.rstrip(b"\r\n")
            message = NATIVE_TIMESTAMP.sub(b"", raw_line, count=1)
            if message.startswith(b"DST_Stats|"):
                continue
            if await self.game_events.accept(raw_line, observed_timestamp_ns):
                continue
            diagnostic = classify_log(message.decode(errors="replace"))
            if diagnostic is not None:
                await self._observe_operational(
                    *diagnostic, observed_timestamp_ns=observed_timestamp_ns
                )
            line = raw_line.decode(errors="replace")
            if self.log_handler is not None:
                try:
                    self.log_handler(line)
                except Exception:
                    if not handler_failed:
                        logger.exception(
                            "DST log handler failed: {cluster}/{shard}",
                            cluster=self.config.cluster,
                            shard=self.config.shard,
                        )
                        handler_failed = True
            elif __debug__:
                logger.debug("DST server log : {line}", line=line)

    async def _drain_output(self) -> None:
        if self.child is None or self.child.returncode is None:
            return
        async with asyncio.timeout(OUTPUT_DRAIN_TIMEOUT):
            pumps = [
                task
                for task in (
                    self.lifecycle_task,
                    self.log_task,
                    self.console.pending_result if self.console is not None else None,
                )
                if task is not None
            ]
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            if not self._exit_observed:
                returncode = self.child.returncode
                await self._observe_operational(
                    "dst.server.process_exited",
                    {
                        "returncode": returncode,
                        "requested": self._termination_requested,
                    },
                    "ERROR"
                    if returncode and not self._termination_requested
                    else "INFO",
                )
                self._exit_observed = True

    async def finish(self) -> None:
        await complete(self._finish())

    async def _finish(self) -> None:
        async with self.finish_lock:
            if self.closed:
                return
            driver_task = self.driver.task
            self.driver.close()
            self._generation_changed.set()
            tasks = [
                task
                for task in (
                    self.console.pending_result if self.console is not None else None,
                    self.lifecycle_task,
                    self.log_task,
                    driver_task,
                )
                if task is not None
            ]
            drain_error: TimeoutError | None = None
            try:
                try:
                    await self._drain_output()
                except TimeoutError as error:
                    drain_error = error
                if self.console is not None:
                    await self.console.close()
            finally:
                await cancel_tasks(*tasks)
                self._input_finished(self.lifecycle_task)
                self._input_finished(self.log_task)
                for transport in self.read_transports:
                    transport.close()
                self.lifecycle.close()
                self.game_events.close()
                self.operational.shutdown()
            self.closed = True
            if self._input_error is not None:
                raise self._input_error
            if drain_error is not None:
                message = "DST output did not drain before the shutdown deadline"
                raise TimeoutError(message) from drain_error
