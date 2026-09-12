import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from time import time_ns
from typing import Self

import orjson
from logbook import Logger
from pydantic import JsonValue
from ulid import ULID

from dst_server.concurrency import cancel_tasks, complete
from dst_server.configuration.files import (
    atomic_write,
    configuration_file_exists,
    validate_directory,
)
from dst_server.events import GameEvent, ObservedGameEvent
from dst_server.events import server as server_events
from dst_server.events.connection import PresenceEvent
from dst_server.game import GameClient
from dst_server.game.rpc import response_adapter
from dst_server.models.console import ConsoleResult
from dst_server.models.driver import (
    DriverFailed,
    DriverHealth,
    DriverReady,
    DriverRecord,
    DriverStarting,
)
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
    classify_log,
    lifecycle_body,
)
from .request import track_request

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
        self.save_lock = asyncio.Lock()
        self.lifecycle = Lifecycle()
        self.lifecycle_task: asyncio.Task[None] | None = None
        self.log_task: asyncio.Task[None] | None = None
        self.closed = False
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
        self.driver = Driver()
        self.game_events = EventStream(
            self.recorder,
            self._observe_driver,
            observe_event=self._observe_game_event,
            profile=self.config.telemetry.profile,
        )
        self._driver_error: str | None = None
        self.game = GameClient(
            shard=config.shard,
            execute_ready=self._execute_ready,
            execute_reload=self._execute_reload,
            wait_reload=self._wait_reload,
            recorder=self.recorder,
            session_id=lambda: self.session_id,
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
    def input_error(self) -> BaseException | None:
        return self._input_error

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

    @property
    def outdated_mods(self) -> tuple[str, ...]:
        return tuple(sorted(self.game_events.outdated_mods))

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
        directory = self.config.directory
        directory.mkdir(parents=True, exist_ok=True)
        validate_directory(directory)
        options_path = directory / "dst_server_driver.json"
        configuration_file_exists(options_path)
        options = self.config.telemetry.model_dump(mode="json") | {
            "nonce": self.game_events.nonce,
        }
        atomic_write(options_path, orjson.dumps(options).decode() + "\n", 0o600)
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

        self.console = Console(
            command_writer, result_reader, self.game_events.nonce, self.recorder
        )
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
        for task in (self.lifecycle_task, self.log_task, self.console.reader_task):
            task.add_done_callback(self._input_finished)
        await self.wait_ready()
        async with _timeout_scope(DEFAULT_COMMAND_TIMEOUT):
            await self.driver.wait_ready()

    async def _pump_lifecycle(self, reader: asyncio.StreamReader) -> None:
        try:
            await self.lifecycle.pump(reader, self._observe_lifecycle)
        finally:
            self.driver.close()

    def _input_finished(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task.cancelled() or (error := task.exception()) is None:
            return
        if self._input_error is None:
            self._input_error = error
        self.lifecycle.close()
        self.driver.close()

    async def execute(
        self,
        command: str,
        completion_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> str:
        timeout = positive_timeout(completion_timeout)
        async with _timeout_scope(timeout):
            result = await self.game.request(
                "evaluate", {"source": command}, response_adapter(ConsoleResult)
            )
            return "\n".join(
                part
                for part in (
                    result.output,
                    result.error.message if result.error else "",
                )
                if part
            )

    async def _execute_ready(
        self, method: str, arguments: dict[str, JsonValue]
    ) -> bytes:
        async with _timeout_scope(DEFAULT_COMMAND_TIMEOUT):
            while True:
                generation = await self.driver.wait_ready()
                try:
                    return await self._execute(
                        method,
                        arguments,
                        generation,
                        lambda generation=generation: self.driver.is_ready(generation),
                    )
                except StaleGenerationError:
                    continue

    async def _execute_reload(
        self,
        method: str,
        arguments: dict[str, JsonValue],
        completion_timeout: float,
    ) -> tuple[bytes, int, float]:
        timeout = positive_timeout(completion_timeout)
        async with _timeout_scope(timeout) as deadline:
            while True:
                generation = await self.driver.wait_ready()
                with track_request() as request_state:
                    try:
                        result = await self._execute(
                            method,
                            arguments,
                            generation,
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
                if self.lifecycle.eof or self.driver.closed:
                    msg = "DST event stream closed before reload completed"
                    raise EOFError(msg)
                changed = self.driver.changed
                if self.driver.generation <= generation:
                    await changed.wait()
            await self.driver.wait_ready()

    async def _execute(
        self,
        method: str,
        arguments: dict[str, JsonValue],
        generation: int,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> bytes:
        with self.recorder.operation("console.execute", self.session_id):
            await self.wait_ready()
            if self.process.returncode is not None:
                msg = f"DST server exited with status {self.process.returncode}"
                raise RuntimeError(msg)
            if self.console is None:
                msg = "DST console is unavailable"
                raise RuntimeError(msg)
            if __debug__:
                logger.debug("DST RPC method: {method}", method=method)
            return await self.console.execute(
                method,
                arguments,
                generation,
                generation_is_current,
                completion_deadline=_operation_deadline.get(),
            )

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
        return await self.game_events.read()

    def _observe_game_event(self, event: GameEvent) -> None:
        self.driver.observe_event(event)
        if isinstance(event, PresenceEvent):
            self.driver.observe_health(event.generation, event.data.health)

    async def _observe_operational(
        self,
        event_name: str,
        body: dict[str, JsonValue],
        severity_text: str = "INFO",
        observed_timestamp_ns: int | None = None,
    ) -> None:
        self.recorder.observe_log(
            event_name=event_name,
            body=body,
            severity_text=severity_text,
            observed_timestamp_ns=(
                time_ns() if observed_timestamp_ns is None else observed_timestamp_ns
            ),
            attributes=self.recorder.attributes(self.session_id)
            | {
                "log.record.uid": str(ULID()),
                "dst.game.attempt.id": self.game_events.nonce,
                "dst.runtime.generation": self.driver.generation,
            },
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

    async def _observe_driver(self, record: DriverRecord) -> None:
        if isinstance(record, DriverFailed):
            if record.nonce is None:
                # Configuration failure cannot authenticate itself or alter readiness.
                await self._observe_operational(
                    "dst.runtime.diagnostic",
                    {"kind": "driver_bootstrap_unverified", "reason": record.error},
                    "ERROR",
                )
                return
            if record.generation is None or record.generation < self.driver.generation:
                return
            self.driver.starting(record.generation)
            self._driver_error = record.error
            self.driver.failed(record.error)
            await self._observe_operational(
                "dst.runtime.diagnostic",
                {
                    "kind": "driver_install_failed",
                    "reason": record.error,
                    "generation": record.generation,
                },
                "ERROR",
            )
            return
        if isinstance(record, DriverStarting):
            self.game_events.start_generation(record.generation)
            self.driver.starting(record.generation)
        elif isinstance(record, DriverReady):
            self.game_events.start_generation(record.health.generation)
            self.driver.ready(record.health)
            if record.health.telemetry_status == "failed":
                await self._observe_operational(
                    "dst.runtime.diagnostic",
                    {
                        "kind": "telemetry_install_failed",
                        "generation": record.health.generation,
                    },
                    "ERROR",
                )
        self._driver_error = self.driver.error

    async def save(
        self, completion_timeout: float = DEFAULT_SAVE_TIMEOUT
    ) -> server_events.SavedEvent:
        with self.recorder.operation("save", self.session_id) as span:
            timeout = positive_timeout(completion_timeout)
            async with _timeout_scope(timeout), self.save_lock:
                event = await self.game.request_save()
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
                self.recorder.record_event("invalid", reason="physical_line_oversized")
                self.recorder.diagnostic(
                    "physical_line_oversized",
                    "stdout",
                    time_ns(),
                    body={"limit_bytes": SUBPROCESS_STREAM_LIMIT},
                    attributes={"dst.game.attempt.id": self.game_events.nonce},
                )
                del raw_line
                continue
            observed_timestamp_ns = time_ns()
            raw_line = raw_line.rstrip(b"\r\n")
            message = NATIVE_TIMESTAMP.sub(b"", raw_line, count=1)
            if message.startswith(b"DST_Stats|"):
                del raw_line, message
                continue
            if await self.game_events.accept(raw_line, observed_timestamp_ns):
                del raw_line, message
                continue
            diagnostic = classify_log(message.decode(errors="replace"))
            del message
            if diagnostic is not None:
                await self._observe_operational(
                    *diagnostic, observed_timestamp_ns=observed_timestamp_ns
                )
            line = raw_line.decode(errors="replace")
            del raw_line
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
            del line

    async def _drain_output(self) -> None:
        if self.child is None or self.child.returncode is None:
            return
        async with asyncio.timeout(OUTPUT_DRAIN_TIMEOUT):
            pumps = [
                task
                for task in (
                    self.lifecycle_task,
                    self.log_task,
                    self.console.reader_task if self.console is not None else None,
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
            self.driver.close()
            tasks = [
                task
                for task in (
                    self.console.reader_task if self.console is not None else None,
                    self.lifecycle_task,
                    self.log_task,
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
            self.closed = True
            if self._input_error is not None:
                raise self._input_error
            if drain_error is not None:
                message = "DST output did not drain before the shutdown deadline"
                raise TimeoutError(message) from drain_error
