from __future__ import annotations

import asyncio
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path
from time import time_ns
from types import TracebackType
from typing import Self

from logbook import Logger

from dst_server.events import ObservedGameEvent
from dst_server.events import server as server_events
from dst_server.game import DriverHealth, GameClient
from dst_server.telemetry.recorder import Recorder
from dst_server.telemetry.stream import EventStream

from .config import ServerConfig
from .console import Console
from .driver import Driver
from .fds import open_pipes, open_reader, open_writer
from .lifecycle import Lifecycle

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
        self.recorder = Recorder(config.cluster, config.shard)
        self.game_events = EventStream(self.recorder)
        self.game = GameClient(
            shard=config.shard,
            lua_directory=config.lua_directory,
            telemetry=config.telemetry,
            execute=self.execute,
            recorder=self.recorder,
            session_id=lambda: self.session_id,
            nonce=self.game_events.nonce,
        )
        self.driver = Driver(self.install_driver, config.cluster, config.shard)

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
        return self.game.driver_health

    @property
    def telemetry_invalid(self) -> int:
        return self.game_events.invalid

    @property
    def telemetry_dropped(self) -> int:
        return self.game_events.dropped

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, exc_tb
        await self.stop()

    async def start(self) -> None:
        with self.recorder.operation("start", self.session_id) as span:
            try:
                await self.start_process()
            except BaseException:
                self.recorder.set_process_up(False)
                raise
            span.set_attribute("process.pid", self.process.pid)
            if self.session_id is not None:
                span.set_attribute("dst.session.id", self.session_id)

    async def start_process(self) -> None:
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
            self.lifecycle.pump(event_reader, self.driver.session_started),
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
        try:
            await self.wait_ready()
            await self.driver.install(self.lifecycle.session_generation)
        except BaseException:
            if self.process.returncode is None:
                self.process.kill()
            await self.process.wait()
            self.cancel_tasks()
            await self.finish()
            raise

    async def execute(self, command: str) -> str:
        with self.recorder.operation("console.execute", self.session_id):
            await self.wait_ready()
            if self.process.returncode is not None:
                msg = f"DST server exited with status {self.process.returncode}"
                raise RuntimeError(msg)
            if self.console is None:
                msg = "DST console is unavailable"
                raise RuntimeError(msg)
            logger.info(
                "execute DST console command: {cluster}/{shard}",
                cluster=self.config.cluster,
                shard=self.config.shard,
            )
            if __debug__:
                logger.debug("DST console command : {command}", command=command)
            result = await self.console.execute(command)
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
        health = await self.game.install()
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
        with self.recorder.operation("save", self.session_id) as span:
            if (
                isinstance(completion_timeout, bool)
                or not isinstance(completion_timeout, (int, float))
                or not math.isfinite(completion_timeout)
                or completion_timeout <= 0
            ):
                msg = "completion timeout must be positive"
                raise ValueError(msg)
            event = await self.lifecycle.wait_for_save(
                self.game.world.request_save,
                float(completion_timeout),
            )
            if event.snapshot is not None:
                span.set_attribute("dst.snapshot", event.snapshot)
            return event

    async def stop(self, grace_period: float = 30) -> int:
        with self.recorder.operation("stop", self.session_id):
            process = self.process
            if process.returncode is not None:
                return await self.wait()

            logger.info(
                "stop DST server: {cluster}/{shard}",
                cluster=self.config.cluster,
                shard=self.config.shard,
            )
            process.terminate()
            exited = asyncio.create_task(process.wait())
            stopping = asyncio.create_task(self.lifecycle.stopping.wait())
            done, pending = await asyncio.wait(
                (exited, stopping),
                timeout=grace_period,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if not done:
                msg = "DST server did not report DST_Stopping; process left running"
                raise TimeoutError(msg)
            if process.returncode is None:
                process.kill()
            return await self.wait()

    async def kill(self) -> int:
        with self.recorder.operation("kill", self.session_id):
            process = self.process
            if process.returncode is None:
                process.kill()
            return await self.wait()

    async def wait(self) -> int:
        with self.recorder.operation("wait", self.session_id) as span:
            process = self.process
            returncode = await process.wait()
            await self.finish()
            self.recorder.set_process_up(False)
            span.set_attribute("process.exit.code", returncode)
            logger.info(
                "DST server exited: {cluster}/{shard} ({returncode})",
                cluster=self.config.cluster,
                shard=self.config.shard,
                returncode=returncode,
            )
            return returncode

    async def pump_logs(self, reader: asyncio.StreamReader) -> None:
        try:
            while raw_line := await reader.readline():
                observed_timestamp_ns = time_ns()
                line = raw_line.decode(errors="replace").rstrip("\r\n")
                if self.game_events.accept(line, observed_timestamp_ns):
                    continue
                if self.log_handler is not None:
                    self.log_handler(line)
                elif __debug__:
                    logger.debug("DST server log : {line}", line=line)
        finally:
            self.game_events.close()

    async def finish(self) -> None:
        async with self.finish_lock:
            if self.closed:
                return
            self.closed = True
            driver_task = self.driver.task
            self.driver.close()
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
            if self.console is not None:
                await self.console.close()
            await asyncio.sleep(0)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def cancel_tasks(self) -> None:
        driver_task = self.driver.task
        self.driver.close()
        for task in (
            self.console.pending_result if self.console is not None else None,
            self.lifecycle_task,
            self.log_task,
            driver_task,
        ):
            if task is not None:
                task.cancel()


__all__ = ["LogHandler", "Server"]
