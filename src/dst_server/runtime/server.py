from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from time import time_ns
from typing import Self

from logbook import Logger

from dst_server.events import ObservedGameEvent
from dst_server.events import server as server_events
from dst_server.game import DriverHealth, GameClient
from dst_server.game.validation import positive_timeout
from dst_server.telemetry.recorder import Recorder
from dst_server.telemetry.stream import EventStream

from .config import ServerConfig
from .console import (
    DEFAULT_COMMAND_TIMEOUT,
    Console,
    StaleGenerationError,
    track_request,
)
from .driver import Driver
from .fds import open_pipes, open_reader, open_writer
from .lifecycle import Lifecycle, RequestState, read_line

FD_LAUNCHER = Path(__file__).with_name("fds.py")
SUBPROCESS_STREAM_LIMIT = 1024 * 1024
DEFAULT_STARTUP_TIMEOUT = 300.0
logger = Logger(__name__)
_operation_deadline = ContextVar[float | None](
    "dst_server_operation_deadline", default=None
)

type LogHandler = Callable[[str], None]


@asynccontextmanager
async def _timeout_scope(duration: float) -> AsyncIterator[float]:
    inherited = _operation_deadline.get()
    if inherited is not None:
        yield inherited
        return
    deadline = asyncio.get_running_loop().time() + duration
    token = _operation_deadline.set(deadline)
    try:
        async with asyncio.timeout_at(deadline):
            yield deadline
    finally:
        _operation_deadline.reset(token)


class Server:  # ruff:ignore[too-many-public-methods]
    def __init__(
        self,
        config: ServerConfig,
        *,
        log_handler: LogHandler | None = None,
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
        self.recorder = Recorder(
            config.telemetry_cluster or config.cluster,
            config.shard,
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
            try:
                timeout = positive_timeout(startup_timeout, "startup")
                async with _timeout_scope(timeout):
                    await self.start_process()
            except BaseException:
                self.recorder.set_process_up(False)
                raise
            span.set_attribute("process.pid", self.process.pid)
            if self.session_id is not None:
                span.set_attribute("dst.session.id", self.session_id)

    async def start_process(self) -> None:  # ruff:ignore[complex-structure]
        if self.child is not None:
            msg = "DST server process objects are single-use"
            raise RuntimeError(msg)

        parent_fds, server_fds = open_pipes()
        parent_pid = os.getpid() if self.config.monitor_parent_process else None
        command = self.config.command(monitor_parent_process=parent_pid)
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
        try:  # ruff:ignore[too-many-statements-in-try-clause]
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
        except BaseException:
            if self.process.returncode is None:
                self.process.kill()
            await self.process.wait()
            await self.finish()
            raise

    async def _pump_lifecycle(self, reader: asyncio.StreamReader) -> None:
        try:
            await self.lifecycle.pump(reader, self._session_started)
        finally:
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

    async def read_game_event(self) -> ObservedGameEvent | None:
        return await self.game_events.read()

    async def install_driver(self) -> DriverHealth:
        try:
            health = await self.game.install()
        except Exception as error:
            self._driver_error = str(error) or type(error).__name__
            raise
        self._driver_error = None
        if health.telemetry_status == "failed":
            logger.warning(
                "failed to install DST telemetry: "
                "{cluster}/{shard} ({profile}): {error}",
                cluster=self.config.cluster,
                shard=self.config.shard,
                profile=self.config.telemetry.profile,
                error=health.telemetry_error,
            )
        return health

    async def save(self, completion_timeout: float = 30) -> server_events.SavedEvent:
        return await self._save(
            self.game.world.request_save,
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

    async def stop(self, grace_period: float = 30) -> int:
        with self.recorder.operation("stop", self.session_id):
            process = self.process
            grace_period = positive_timeout(grace_period, "grace period")
            if process.returncode is not None:
                return await self._reap()

            logger.info(
                "stop DST server: {cluster}/{shard}",
                cluster=self.config.cluster,
                shard=self.config.shard,
            )
            process.terminate()
            exited = asyncio.create_task(process.wait())
            stopping = asyncio.create_task(self.lifecycle.stopping.wait())
            tasks = (exited, stopping)
            try:  # ruff:ignore[too-many-statements-in-try-clause]
                try:
                    done, _ = await asyncio.wait(
                        tasks,
                        timeout=grace_period,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.kill()
                await self._reap()
                raise
            if not done:
                msg = "DST server did not report DST_Stopping; process left running"
                raise TimeoutError(msg)
            if process.returncode is None:
                process.kill()
            return await self._reap()

    async def kill(self) -> int:
        with self.recorder.operation("kill", self.session_id):
            process = self.process
            if process.returncode is None:
                process.kill()
            return await self._reap()

    async def _reap(self) -> int:
        waiting = asyncio.create_task(self.wait())
        try:
            return await asyncio.shield(waiting)
        except asyncio.CancelledError:
            await asyncio.shield(waiting)
            raise

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

    async def pump_logs(self, reader: asyncio.StreamReader) -> None:
        handler_failed = False
        while (raw_line := await read_line(reader)) is not None:
            observed_timestamp_ns = time_ns()
            raw_line = raw_line.rstrip(b"\r\n")
            if self.game_events.accept(raw_line, observed_timestamp_ns):
                continue
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

    async def finish(self) -> None:
        async with self.finish_lock:
            if self.closed:
                return
            driver_task = self.driver.task
            self.driver.close()
            self.lifecycle.close()
            self._generation_changed.set()
            self.game_events.close()
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
            for transport in self.read_transports:
                transport.close()
            try:
                if self.console is not None:
                    await self.console.close()
                await asyncio.sleep(0)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            self.closed = True


__all__ = ["LogHandler", "Server"]
