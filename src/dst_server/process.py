from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from time import time_ns
from types import TracebackType
from typing import Self

from logbook import Logger

from .arguments import ServerArgs
from .console import Console
from .driver import DriverManager
from .events import DriverHealth, ObservedGameEvent, ServerEvent, ServerSavedEvent
from .game import GameClient
from .game_events import GameEventStream
from .instrumentation import Instrumentation
from .protocol import open_pipe_reader, open_pipe_writer, open_protocol_pipes
from .server_events import ServerEventStream
from .validation import number

FD_WRAPPER = Path(__file__).with_name("fd_wrapper.py")
SUBPROCESS_STREAM_LIMIT = 1024 * 1024
logger = Logger(__name__)

type LogHandler = Callable[[str], None]


class Server:  # ruff:ignore[too-many-public-methods]
    def __init__(
        self,
        args: ServerArgs,
        *,
        log_handler: LogHandler | None = None,
    ) -> None:
        self.args = args
        self.log_handler = log_handler
        self.child: asyncio.subprocess.Process | None = None
        self.console: Console | None = None
        self.read_transports: tuple[asyncio.ReadTransport, ...] = ()
        self.finish_lock = asyncio.Lock()
        self.server_events = ServerEventStream()
        self.event_task: asyncio.Task[None] | None = None
        self.log_task: asyncio.Task[None] | None = None
        self.closed = False
        self.instrumentation = Instrumentation(args.cluster, args.shard)
        self.game_events = GameEventStream(self.instrumentation)
        self.game = GameClient(
            args,
            self.execute,
            self.instrumentation,
            lambda: self.session_id,
            self.game_events.nonce,
        )
        self.driver = DriverManager(self.install_driver, args.cluster, args.shard)

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
        return self.server_events.session_id

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
        with self.instrumentation.operation("start", self.session_id) as span:
            try:
                await self.start_process()
            except BaseException:
                self.instrumentation.set_process_up(False)
                raise
            span.set_attribute("process.pid", self.process.pid)
            if self.session_id is not None:
                span.set_attribute("dst.session.id", self.session_id)

    async def start_process(self) -> None:
        if self.child is not None:
            msg = "DST server process objects are single-use"
            raise RuntimeError(msg)

        parent_fds, server_fds = open_protocol_pipes()
        parent_pid = os.getpid() if self.args.monitor_parent_process else None
        command = self.args.command(monitor_parent_process=parent_pid)
        transports: list[asyncio.BaseTransport] = []
        try:  # ruff:ignore[too-many-statements-in-try-clause]
            command_writer = await open_pipe_writer(parent_fds.pop(0))
            transports.append(command_writer.transport)
            result_reader, result_transport = await open_pipe_reader(parent_fds.pop(0))
            transports.append(result_transport)
            event_reader, event_transport = await open_pipe_reader(parent_fds.pop(0))
            transports.append(event_transport)
            logger.info(
                "start DST server: {cluster}/{shard}",
                cluster=self.args.cluster,
                shard=self.args.shard,
            )
            self.child = await asyncio.create_subprocess_exec(
                sys.executable,
                str(FD_WRAPPER),
                *(str(descriptor) for descriptor in server_fds),
                *command,
                cwd=self.args.executable.parent,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=SUBPROCESS_STREAM_LIMIT,
                pass_fds=server_fds,
            )
            self.instrumentation.set_process_up(True)
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
        self.event_task = asyncio.create_task(
            self.server_events.pump(event_reader, self.driver.session_started),
            name=f"dst-events-{self.args.shard}",
        )
        stdout = self.process.stdout
        if stdout is None:
            msg = "DST server stdout pipe is unavailable"
            raise RuntimeError(msg)
        self.log_task = asyncio.create_task(
            self.pump_logs(stdout),
            name=f"dst-logs-{self.args.shard}",
        )
        try:
            await self.wait_ready()
            await self.driver.install(self.server_events.session_generation)
        except BaseException:
            if self.process.returncode is None:
                self.process.kill()
            await self.process.wait()
            self.cancel_tasks()
            await self.finish()
            raise

    async def execute(self, command: str) -> str:
        with self.instrumentation.operation("console.execute", self.session_id):
            await self.wait_ready()
            if self.process.returncode is not None:
                msg = f"DST server exited with status {self.process.returncode}"
                raise RuntimeError(msg)
            if self.console is None:
                msg = "DST console is unavailable"
                raise RuntimeError(msg)
            logger.info(
                "execute DST console command: {cluster}/{shard}",
                cluster=self.args.cluster,
                shard=self.args.shard,
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
        await self.server_events.wait_ready()

    async def read_event(self) -> ServerEvent | None:
        return await self.server_events.read()

    async def read_game_event(self) -> ObservedGameEvent | None:
        return await self.game_events.read()

    async def install_driver(self) -> DriverHealth:
        return await self.game.install()

    async def save(self, completion_timeout: float = 30) -> ServerSavedEvent:
        with self.instrumentation.operation("save", self.session_id) as span:
            completion_timeout = number("completion timeout", completion_timeout)
            if completion_timeout <= 0:
                msg = "completion timeout must be positive"
                raise ValueError(msg)
            event = await self.server_events.wait_for_save(
                self.game.world.request_save,
                completion_timeout,
            )
            if event.snapshot is not None:
                span.set_attribute("dst.snapshot", event.snapshot)
            return event

    async def stop(self, grace_period: float = 30) -> int:
        with self.instrumentation.operation("stop", self.session_id):
            process = self.process
            if process.returncode is not None:
                return await self.wait()

            logger.info(
                "stop DST server: {cluster}/{shard}",
                cluster=self.args.cluster,
                shard=self.args.shard,
            )
            process.terminate()
            exited = asyncio.create_task(process.wait())
            stopping = asyncio.create_task(self.server_events.stopping.wait())
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
        with self.instrumentation.operation("kill", self.session_id):
            process = self.process
            if process.returncode is None:
                process.kill()
            return await self.wait()

    async def wait(self) -> int:
        with self.instrumentation.operation("wait", self.session_id) as span:
            process = self.process
            returncode = await process.wait()
            await self.finish()
            self.instrumentation.set_process_up(False)
            span.set_attribute("process.exit.code", returncode)
            logger.info(
                "DST server exited: {cluster}/{shard} ({returncode})",
                cluster=self.args.cluster,
                shard=self.args.shard,
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
                    self.event_task,
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
            self.event_task,
            self.log_task,
            driver_task,
        ):
            if task is not None:
                task.cancel()


__all__ = ["LogHandler", "Server"]
