import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from logbook import Logger
from ulid import ULID

from dst_server.concurrency import cancel_tasks, complete
from dst_server.errors import DisconnectedError
from dst_server.models.cluster import ShardDesired, ShardPhase
from dst_server.timeouts import operation_deadline

from .server import Server

logger = Logger(__name__)

type ServerFactory = Callable[[], Server]
type ServerHook = Callable[[Server], Awaitable[None]]


class _Action(StrEnum):
    STOP = "stop"
    RESTART = "restart"
    KILL = "kill"


@dataclass(frozen=True, slots=True)
class ShardSupervisorStatus:
    shard: str
    desired: ShardDesired
    phase: ShardPhase
    returncode: int | None
    error_id: ULID | None = None


type FailureHook = Callable[[ShardSupervisorStatus], Awaitable[None]]


class ShardSupervisor:
    def __init__(
        self,
        shard: str,
        factory: ServerFactory,
        *,
        on_stopped: ServerHook | None = None,
        on_failed: FailureHook | None = None,
    ) -> None:
        if not shard:
            msg = "shard must not be empty"
            raise ValueError(msg)
        self.shard = shard
        self._factory = factory
        self._on_stopped = on_stopped
        self._on_failed = on_failed
        self._condition = asyncio.Condition()
        self._wake = asyncio.Event()
        self._desired = ShardDesired.STOPPED
        self._phase = ShardPhase.STOPPED
        self._returncode: int | None = None
        self._error_id: ULID | None = None
        self._action: _Action | None = None
        self._server: Server | None = None
        self._runner: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._failure_reported = False
        self._closed = False

    @property
    def status(self) -> ShardSupervisorStatus:
        return ShardSupervisorStatus(
            shard=self.shard,
            desired=self._desired,
            phase=self._phase,
            returncode=self._returncode,
            error_id=self._error_id,
        )

    @property
    def server(self) -> Server | None:
        return self._server

    async def start(self) -> ShardSupervisorStatus:
        async with self._condition:
            self._require_available()
            if self._phase is ShardPhase.FAILED and self._server is None:
                self._phase = ShardPhase.STOPPED
            elif self._phase is ShardPhase.FAILED:
                self._phase = ShardPhase.STOPPING
                self._action = _Action.RESTART
            self._desired = ShardDesired.RUNNING
            self._ensure_runner()
            self._wake.set()
            self._condition.notify_all()
            await self._condition.wait_for(
                lambda: (
                    self._phase
                    in {ShardPhase.RUNNING, ShardPhase.FAILED, ShardPhase.UNAVAILABLE}
                    or (
                        self._desired is ShardDesired.STOPPED
                        and self._phase is ShardPhase.STOPPED
                    )
                )
            )
            self._raise_if_live_failure()
            return self.status

    async def stop(self) -> ShardSupervisorStatus:
        return await self._stop(_Action.STOP)

    async def kill(self) -> ShardSupervisorStatus:
        return await self._stop(_Action.KILL)

    async def restart(self) -> ShardSupervisorStatus:
        async with self._condition:
            self._require_available()
            previous = self._server
            self._desired = ShardDesired.RUNNING
            if self._phase is ShardPhase.FAILED and self._server is not None:
                self._phase = ShardPhase.STOPPING
                self._action = _Action.RESTART
            elif self._phase in {ShardPhase.STOPPED, ShardPhase.FAILED}:
                self._phase = ShardPhase.STOPPED
            else:
                self._action = _Action.RESTART
            self._ensure_runner()
            self._wake.set()
            self._condition.notify_all()
            await self._condition.wait_for(
                lambda: (
                    self._phase
                    in {
                        ShardPhase.FAILED,
                        ShardPhase.UNAVAILABLE,
                    }
                    or (
                        self._phase is ShardPhase.RUNNING
                        and self._server is not previous
                    )
                    or (
                        self._desired is ShardDesired.STOPPED
                        and self._phase is ShardPhase.STOPPED
                    )
                )
            )
            self._raise_if_live_failure()
            return self.status

    async def aclose(self) -> None:
        task = self._close_task
        if task is None:
            task = self._close_task = asyncio.create_task(
                self._close(),
                name=f"dst-close-{self.shard}",
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
        async with self._condition:
            if self._closed:
                return
            self._desired = ShardDesired.STOPPED
            if self._server is None and self._phase in {
                ShardPhase.STOPPED,
                ShardPhase.FAILED,
            }:
                self._phase = ShardPhase.STOPPED
            else:
                self._action = _Action.KILL
                if self._phase is ShardPhase.FAILED:
                    self._phase = ShardPhase.STOPPING
                self._wake.set()
                await self._condition.wait_for(
                    lambda: self._phase in {ShardPhase.STOPPED, ShardPhase.FAILED}
                )
                self._raise_if_live_failure()
            self._closed = True
            self._wake.set()
            runner = self._runner
            self._condition.notify_all()
        if runner is not None:
            await cancel_tasks(runner)
        async with self._condition:
            self._phase = ShardPhase.UNAVAILABLE
            self._condition.notify_all()

    async def _stop(self, action: _Action) -> ShardSupervisorStatus:
        async with self._condition:
            self._require_available()
            self._desired = ShardDesired.STOPPED
            if self._server is None and self._phase in {
                ShardPhase.STOPPED,
                ShardPhase.FAILED,
            }:
                self._phase = ShardPhase.STOPPED
                self._condition.notify_all()
                return self.status
            self._action = action
            if self._phase is ShardPhase.FAILED:
                self._phase = ShardPhase.STOPPING
            self._wake.set()
            self._condition.notify_all()
            await self._condition.wait_for(
                lambda: (
                    self._phase
                    in {
                        ShardPhase.STOPPED,
                        ShardPhase.FAILED,
                        ShardPhase.UNAVAILABLE,
                    }
                    or self._desired is not ShardDesired.STOPPED
                )
            )
            self._raise_if_live_failure()
            return self.status

    def _require_available(self) -> None:
        if self._close_task is not None or self._closed:
            msg = f"shard supervisor is unavailable: {self.shard}"
            raise DisconnectedError(msg)

    def _ensure_runner(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(
                self._run(),
                name=f"dst-supervisor-{self.shard}",
            )

    def _take_action(self) -> _Action | None:
        action = self._action
        self._action = None
        return action

    async def _run(self) -> None:
        # Process supervision outlives the request that first started it.
        operation_deadline.set(None)
        while not self._closed:
            try:
                await self._step()
            except Exception as error:
                self._record_error("run", error)
                try:
                    await self._recover()
                except Exception as error:
                    self._record_error("recover", error)
                    await self._failed(self._returncode, terminal=True)

    async def _step(self) -> None:
        if (
            self._server is not None
            and self._phase is ShardPhase.STOPPING
            and self._action is not None
        ):
            await self._stop_attempt(self._server, self._action)
            return
        if self._desired is ShardDesired.RUNNING and (
            self._phase is not ShardPhase.FAILED
        ):
            await self._attempt()
            return
        self._wake.clear()
        await self._wake.wait()

    async def _recover(self) -> None:
        server = self._server
        returncode = None if server is None else server.returncode
        if server is not None:
            try:
                returncode = await self._terminate_now(server, force=True)
            except Exception as error:
                self._record_error("recover", error)
                if self._is_live(server):
                    self._action = None
                    await self._failed(terminal=True)
                    return
                returncode = server.returncode
            if self._is_live(server):
                self._action = None
                await self._failed(terminal=True)
                return
            self._action = None
            await self._finish_attempt(server)
        await self._failed(returncode)

    async def _attempt(self) -> None:
        self._failure_reported = False
        self._returncode = None
        self._error_id = None
        await self._set_phase(ShardPhase.STARTING)

        try:
            server = self._factory()
        except Exception as error:
            self._record_error("create", error)
            await self._failed()
            return

        self._server = server
        await self._notify()
        start_task = asyncio.create_task(
            server.start(),
            name=f"dst-start-{self.shard}",
        )
        try:
            started = await self._await_or_action(start_task)
        finally:
            await cancel_tasks(start_task)
        if not started:
            action = self._action
            if action is None:
                raise RuntimeError
            await self._stop_attempt(server, action)
            return
        try:
            start_task.result()
        except Exception as error:
            self._record_error("start", error)
            returncode = await self._terminate(server, force=True)
            await self._finish_attempt(server)
            await self._failed(returncode)
            return

        await self._set_phase(ShardPhase.RUNNING)
        await self._running(server)

    async def _running(self, server: Server) -> None:
        exit_task = asyncio.create_task(
            server.process.wait(),
            name=f"dst-exit-{self.shard}",
        )
        try:
            if not await self._await_or_action(exit_task):
                action = self._action
                if action is None:
                    msg = f"shard stopped without a control action: {self.shard}"
                    raise RuntimeError(msg)
                await self._stop_attempt(server, action)
                return
            returncode = await server.wait()
            await self._finish_attempt(server)
            await self._failed(returncode)
        finally:
            await cancel_tasks(exit_task)

    async def _stop_attempt(self, server: Server, action: _Action) -> None:
        await self._set_phase(ShardPhase.STOPPING)
        returncode = await self._terminate(
            server,
            force=action is _Action.KILL,
        )
        if self._is_live(server):
            msg = f"failed to stop live shard process: {self.shard}"
            raise RuntimeError(msg)
        await self._finish_attempt(server)
        action = self._take_action() or action
        await self._after_action(action, returncode)

    async def _await_or_action[T](self, task: asyncio.Task[T]) -> bool:
        while True:
            if self._closed or self._action is not None:
                return False
            if task.done():
                return True
            self._wake.clear()
            wake_task = asyncio.create_task(self._wake.wait())
            try:
                await asyncio.wait(
                    (task, wake_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                await cancel_tasks(wake_task)

    async def _after_action(
        self,
        action: _Action | None,
        returncode: int | None = None,
    ) -> None:
        self._returncode = returncode
        self._action = None
        if action is _Action.RESTART or self._desired is ShardDesired.RUNNING:
            self._desired = ShardDesired.RUNNING
        else:
            self._desired = ShardDesired.STOPPED
        await self._set_phase(ShardPhase.STOPPED)

    async def _failed(
        self,
        returncode: int | None = None,
        *,
        terminal: bool = False,
    ) -> None:
        action = self._take_action()
        if action is not None and not terminal:
            await self._after_action(action, returncode)
            return
        self._returncode = returncode
        if not terminal and self._desired is ShardDesired.STOPPED:
            await self._set_phase(ShardPhase.STOPPED)
            return
        await self._set_phase(ShardPhase.FAILED)
        if self._failure_reported:
            return
        self._failure_reported = True
        await self._report_failure()

    async def _report_failure(self) -> None:
        self._error_id = self._error_id or ULID()
        logger.error(
            "shard failed: {shard}: {error_id}: returncode={returncode}",
            shard=self.shard,
            error_id=self._error_id,
            returncode=self._returncode,
        )
        if self._on_failed is None:
            return
        try:
            await self._on_failed(self.status)
        except Exception:
            logger.exception(
                "shard failure hook failed: {shard}",
                shard=self.shard,
            )

    def _record_error(self, stage: str, error: Exception) -> None:
        self._error_id = self._error_id or ULID()
        logger.error(
            "shard failed: {shard}: {error_id}: {stage}: game_attempt={game_attempt}",
            shard=self.shard,
            error_id=self._error_id,
            stage=stage,
            game_attempt=(
                self._server.game_events.nonce if self._server is not None else None
            ),
            exc_info=error,
        )

    async def _finish_attempt(self, server: Server) -> None:
        if self._is_live(server):
            msg = f"cannot finish live shard process: {self.shard}"
            raise RuntimeError(msg)
        if self._on_stopped is not None:
            try:
                await self._on_stopped(server)
            except Exception:
                logger.exception("shard stop hook failed: {shard}", shard=self.shard)
        self._server = None
        await self._notify()

    async def _terminate(self, server: Server, *, force: bool) -> int | None:
        if force:
            return await self._terminate_now(server, force=True)

        stopping = asyncio.create_task(
            self._terminate_now(server, force=False),
            name=f"dst-terminate-{self.shard}",
        )
        try:
            while not stopping.done():
                if self._action is _Action.KILL:
                    await cancel_tasks(stopping)
                    return await self._terminate_now(server, force=True)
                self._wake.clear()
                wake = asyncio.create_task(self._wake.wait())
                try:
                    await asyncio.wait(
                        (stopping, wake),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    await cancel_tasks(wake)
            return stopping.result()
        finally:
            await cancel_tasks(stopping)

    @staticmethod
    async def _terminate_now(server: Server, *, force: bool) -> int | None:
        if server.child is None:
            return None
        if server.returncode is not None:
            if server.closed:
                return server.returncode
            return await server.wait()
        try:
            if not force:
                try:
                    return await server.stop()
                except TimeoutError:
                    pass
            return await server.kill()
        except ProcessLookupError:
            return await server.wait()

    @staticmethod
    def _is_live(server: Server) -> bool:
        return server.child is not None and server.returncode is None

    def _raise_if_live_failure(self) -> None:
        if (
            self._phase is ShardPhase.FAILED
            and self._server is not None
            and self._is_live(self._server)
        ):
            msg = f"failed to stop live shard process: {self.shard}"
            raise RuntimeError(msg)

    async def _set_phase(self, phase: ShardPhase) -> None:
        async with self._condition:
            self._phase = phase
            self._condition.notify_all()

    async def _notify(self) -> None:
        async with self._condition:
            self._condition.notify_all()
