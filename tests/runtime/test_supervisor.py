import asyncio
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import cast

import logbook
import pytest
from ulid import ULID

from dst_server.models.cluster import ShardDesired, ShardPhase
from dst_server.runtime import Server
from dst_server.runtime.supervisor import (
    ShardSupervisor,
    ShardSupervisorStatus,
)
from dst_server.timeouts import operation_deadline, timeout_scope
from tests.helpers import wait_for_event


class ProcessStub:
    def __init__(
        self,
        *,
        start_gate: asyncio.Event | None = None,
        stop_gate: asyncio.Event | None = None,
        kill_gate: asyncio.Event | None = None,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        kill_error: Exception | None = None,
        wait_error: Exception | None = None,
    ) -> None:
        self.game_events = SimpleNamespace(nonce=str(ULID()))
        self.child = cast("asyncio.subprocess.Process", self)
        self.closed = False
        self.returncode: int | None = None
        self.exited = asyncio.Event()
        self.started = asyncio.Event()
        self.stop_started = asyncio.Event()
        self.kill_started = asyncio.Event()
        self.start_gate = start_gate
        self.stop_gate = stop_gate
        self.kill_gate = kill_gate
        self.start_error = start_error
        self.stop_error = stop_error
        self.kill_error = kill_error
        self.wait_error = wait_error
        self.stop_calls = 0
        self.kill_calls = 0

    @property
    def process(self) -> asyncio.subprocess.Process:
        return cast("asyncio.subprocess.Process", self)

    def exit(self, code: int) -> None:
        self.returncode = code
        self.exited.set()

    async def start(self, startup_timeout: float = 300) -> None:
        del startup_timeout
        self.started.set()
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.start_error is not None:
            self.exit(-signal.SIGKILL)
            self.closed = True
            raise self.start_error

    async def wait(self) -> int:
        await self.exited.wait()
        if self.wait_error is not None:
            raise self.wait_error
        self.closed = True
        assert self.returncode is not None
        return self.returncode

    async def stop(self, grace_period: float = 30) -> int:
        del grace_period
        self.stop_calls += 1
        self.stop_started.set()
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if isinstance(self.stop_error, TimeoutError):
            raise self.stop_error
        self.exit(0)
        if self.stop_error is not None:
            raise self.stop_error
        return await self.wait()

    async def kill(self) -> int:
        self.kill_calls += 1
        self.kill_started.set()
        if self.kill_gate is not None:
            await self.kill_gate.wait()
        if self.kill_error is not None:
            raise self.kill_error
        self.exit(-signal.SIGKILL)
        return await self.wait()


class Factory:
    def __init__(self, *servers: ProcessStub, error: Exception | None = None) -> None:
        self.servers = deque(servers)
        self.error = error
        self.calls = 0

    def __call__(self) -> Server:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return cast(Server, self.servers.popleft())


@pytest.fixture
async def managed_supervisor() -> AsyncIterator[Callable[..., ShardSupervisor]]:
    supervisors: list[ShardSupervisor] = []

    def create(*args: object, **kwargs: object) -> ShardSupervisor:
        supervisor = ShardSupervisor(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        supervisors.append(supervisor)
        return supervisor

    yield create
    for supervisor in supervisors:
        server = cast(ProcessStub | None, supervisor.server)
        if server is not None:
            for gate in (server.start_gate, server.stop_gate, server.kill_gate):
                if gate is not None:
                    gate.set()
            server.kill_error = None
        await asyncio.gather(supervisor.aclose(), return_exceptions=True)


async def wait_phase(
    supervisor: ShardSupervisor,
    phase: ShardPhase,
) -> ShardSupervisorStatus:
    async with asyncio.timeout(1):
        while supervisor.status.phase is not ShardPhase.UNAVAILABLE and (  # ruff: ignore[async-busy-wait]
            supervisor.status.phase is not phase
        ):
            await asyncio.sleep(0)
    assert supervisor.status.phase is phase
    return supervisor.status


async def _append(values: list[object], value: object) -> None:
    values.append(value)
    await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("action", "phase", "desired", "calls", "factory_calls"),
    [
        ("stop", ShardPhase.STOPPED, ShardDesired.STOPPED, (1, 0), 1),
        ("kill", ShardPhase.STOPPED, ShardDesired.STOPPED, (0, 1), 1),
        ("restart", ShardPhase.RUNNING, ShardDesired.RUNNING, (1, 0), 2),
    ],
)
async def test_public_action_matrix(
    managed_supervisor: Callable[..., ShardSupervisor],
    action: str,
    phase: ShardPhase,
    desired: ShardDesired,
    calls: tuple[int, int],
    factory_calls: int,
) -> None:
    first, second = ProcessStub(), ProcessStub()
    factory = Factory(first, second)
    stopped: list[object] = []
    supervisor = managed_supervisor(
        "Forest",
        factory,
        on_stopped=lambda server: _append(stopped, server),
    )

    running = await supervisor.start()
    assert running.phase is ShardPhase.RUNNING
    assert supervisor.server is cast(Server, first)
    assert await supervisor.start() == running
    result = await getattr(supervisor, action)()

    assert (result.phase, result.desired) == (phase, desired)
    assert (first.stop_calls, first.kill_calls) == calls
    assert factory.calls == factory_calls
    assert stopped[0] is first


@pytest.mark.parametrize("stage", ["factory", "start", "exit-zero", "exit-error"])
async def test_first_failure_is_reported_without_restarting(
    managed_supervisor: Callable[..., ShardSupervisor], stage: str
) -> None:
    server = ProcessStub(
        start_error=RuntimeError("start failed") if stage == "start" else None
    )
    factory = Factory(
        server,
        ProcessStub(),
        error=RuntimeError("factory failed") if stage == "factory" else None,
    )
    failed: list[object] = []
    supervisor = managed_supervisor(
        "Caves", factory, on_failed=lambda status: _append(failed, status)
    )
    await supervisor.start()
    if stage.startswith("exit"):
        server.exit(0 if stage == "exit-zero" else 23)
    status = await wait_phase(supervisor, ShardPhase.FAILED)
    await asyncio.sleep(0)
    assert failed == [status]
    assert factory.calls == 1
    assert supervisor.server is None
    if stage.startswith("exit"):
        assert status.returncode == (0 if stage == "exit-zero" else 23)


async def test_restarts_do_not_inherit_the_initial_request_deadline(
    managed_supervisor: Callable[..., ShardSupervisor],
) -> None:
    deadlines: list[float] = []

    class TimedProcess(ProcessStub):
        async def start(self, startup_timeout: float = 1) -> None:
            async with timeout_scope(startup_timeout) as deadline:
                deadlines.append(deadline)
                await super().start()

    first, second, third = TimedProcess(), TimedProcess(), TimedProcess()
    supervisor = managed_supervisor("Forest", Factory(first, second, third))
    async with timeout_scope(60) as request_deadline:
        await supervisor.start()
        assert operation_deadline.get() == request_deadline
    await supervisor.restart()
    second.exit(1)
    await wait_phase(supervisor, ShardPhase.FAILED)
    await supervisor.start()

    assert len(deadlines) == 3
    assert all(deadline < request_deadline for deadline in deadlines)


async def test_stop_during_restart_cleanup_prevents_another_start(
    managed_supervisor: Callable[..., ShardSupervisor],
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def stopped(_: Server) -> None:
        entered.set()
        await release.wait()

    factory = Factory(ProcessStub(), ProcessStub())
    supervisor = managed_supervisor("Forest", factory, on_stopped=stopped)
    await supervisor.start()
    restarting = asyncio.create_task(supervisor.restart())
    stopping: asyncio.Task[ShardSupervisorStatus] | None = None
    try:
        await wait_for_event(entered, restarting)
        stopping = asyncio.create_task(supervisor.stop())
        async with supervisor._condition:
            await supervisor._condition.wait_for(
                lambda: supervisor.status.desired is ShardDesired.STOPPED
            )
        release.set()
        async with asyncio.timeout(1):
            results = await asyncio.gather(restarting, stopping)
        assert all(result.phase is ShardPhase.STOPPED for result in results)
        assert factory.calls == 1
    finally:
        release.set()
        tasks = (restarting,) if stopping is None else (restarting, stopping)
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("stage", ["start", "exit"])
async def test_restart_during_failure_cleanup_starts_next_process(
    managed_supervisor: Callable[..., ShardSupervisor],
    stage: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def stopped(_: Server) -> None:
        entered.set()
        await release.wait()

    first = ProcessStub(
        start_error=RuntimeError("start failed") if stage == "start" else None
    )
    second = ProcessStub()
    factory = Factory(first, second)
    supervisor = managed_supervisor("Forest", factory, on_stopped=stopped)
    starting = asyncio.create_task(supervisor.start())
    restarting: asyncio.Task[ShardSupervisorStatus] | None = None
    try:
        if stage == "exit":
            await starting
            first.exit(23)
        await wait_for_event(entered)
        restarting = asyncio.create_task(supervisor.restart())
        async with supervisor._condition:
            await supervisor._condition.wait_for(lambda: supervisor._action is not None)
        release.set()
        async with asyncio.timeout(1):
            result = await restarting
        assert result.phase is ShardPhase.RUNNING
        assert supervisor.server is cast(Server, second)
        assert factory.calls == 2
    finally:
        release.set()
        tasks = (starting,) if restarting is None else (starting, restarting)
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("stage", ["create", "start", "recover"])
async def test_failure_id_links_root_cause_logs_to_terminal_status(
    managed_supervisor: Callable[..., ShardSupervisor],
    stage: str,
) -> None:
    error = OSError("native operation failed")
    server = ProcessStub(
        start_error=error if stage == "start" else None,
        kill_error=error if stage == "recover" else None,
    )
    supervisor = managed_supervisor(
        "Forest",
        Factory(server, error=error if stage == "create" else None),
    )
    with logbook.TestHandler() as output:
        if stage == "recover":
            await supervisor.start()
            with pytest.raises(RuntimeError, match="failed to stop live"):
                await supervisor.kill()
            server.kill_error = None
        else:
            await supervisor.start()

    status = supervisor.status
    assert status.phase is ShardPhase.FAILED
    assert status.error_id is not None
    causes = [record for record in output.records if record.exc_info]
    assert causes
    assert all(record.exc_info[1] is error for record in causes)
    assert all(str(status.error_id) in record.message for record in output.records)
    assert any(f": {stage}:" in record.message for record in causes)


@pytest.mark.parametrize("action", ["stop", "kill", "restart", "aclose"])
async def test_action_interrupts_startup(
    managed_supervisor: Callable[..., ShardSupervisor],
    action: str,
) -> None:
    server = ProcessStub(start_gate=asyncio.Event())
    supervisor = managed_supervisor("Forest", Factory(server, ProcessStub()))
    starting = asyncio.create_task(supervisor.start())
    acting: asyncio.Task[object] | None = None
    try:
        await wait_for_event(server.started, starting)
        await wait_phase(supervisor, ShardPhase.STARTING)

        acting = asyncio.create_task(getattr(supervisor, action)())
        await asyncio.wait_for(asyncio.shield(acting), timeout=5)
        result = await asyncio.wait_for(asyncio.shield(starting), timeout=5)

        if action == "restart":
            assert result.desired is ShardDesired.RUNNING
            assert supervisor.status.phase is ShardPhase.RUNNING
        elif action == "aclose":
            assert supervisor.status.phase is ShardPhase.UNAVAILABLE
        else:
            assert result.phase is ShardPhase.STOPPED
    finally:
        async with asyncio.timeout(5):
            if server.start_gate is not None:
                server.start_gate.set()
            pending = (starting,) if acting is None else (starting, acting)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.parametrize(
    ("action", "stop_error", "kill_error", "expected"),
    [
        ("stop", TimeoutError(), None, -signal.SIGKILL),
        ("stop", ProcessLookupError(), None, 0),
        ("kill", None, ProcessLookupError(), -signal.SIGKILL),
    ],
)
async def test_termination_failure_matrix(
    managed_supervisor: Callable[..., ShardSupervisor],
    action: str,
    stop_error: Exception | None,
    kill_error: Exception | None,
    expected: int,
) -> None:
    server = ProcessStub(stop_error=stop_error, kill_error=kill_error)
    supervisor = managed_supervisor("Forest", Factory(server))
    await supervisor.start()
    if isinstance(stop_error, ProcessLookupError):
        server.exit(0)
    if isinstance(kill_error, ProcessLookupError):
        server.exit(-signal.SIGKILL)

    status = await getattr(supervisor, action)()

    assert status.phase is ShardPhase.STOPPED
    assert status.returncode == expected
    assert supervisor.server is None


@pytest.mark.parametrize("action", ["kill", "aclose"])
async def test_force_action_upgrades_in_progress_stop(
    managed_supervisor: Callable[..., ShardSupervisor],
    action: str,
) -> None:
    server = ProcessStub(stop_gate=asyncio.Event())
    supervisor = managed_supervisor("Forest", Factory(server))
    await supervisor.start()
    stopping = asyncio.create_task(supervisor.stop())
    acting: asyncio.Task[object] | None = None
    try:
        await wait_for_event(server.stop_started, stopping)

        acting = asyncio.create_task(getattr(supervisor, action)())
        await asyncio.wait_for(asyncio.shield(acting), timeout=5)
        await asyncio.wait_for(asyncio.shield(stopping), timeout=5)

        assert (server.stop_calls, server.kill_calls) == (1, 1)
        assert server.returncode == -signal.SIGKILL
    finally:
        async with asyncio.timeout(5):
            if server.stop_gate is not None:
                server.stop_gate.set()
            pending = (stopping,) if acting is None else (stopping, acting)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


async def test_live_process_remains_retryable_after_kill_failure(
    managed_supervisor: Callable[..., ShardSupervisor],
) -> None:
    server = ProcessStub(kill_error=PermissionError())
    failed: list[object] = []
    supervisor = managed_supervisor(
        "Forest",
        Factory(server),
        on_failed=lambda status: _append(failed, status),
    )
    await supervisor.start()

    with pytest.raises(RuntimeError, match="failed to stop live shard process"):
        await supervisor.kill()
    assert supervisor.server is cast(Server, server)
    assert len(failed) == 1

    server.kill_error = None
    await supervisor.aclose()
    assert supervisor.server is None


@pytest.mark.parametrize("hook", ["stopped", "failed"])
async def test_hook_failures_do_not_strand_the_supervisor(
    managed_supervisor: Callable[..., ShardSupervisor], hook: str
) -> None:
    async def fail(_: object) -> None:
        await asyncio.sleep(0)
        message = "hook failed"
        raise RuntimeError(message)

    server = ProcessStub()
    kwargs = {f"on_{hook}": fail}
    supervisor = managed_supervisor("Forest", Factory(server), **kwargs)
    await supervisor.start()
    if hook == "stopped":
        assert (await supervisor.stop()).phase is ShardPhase.STOPPED
    else:
        server.exit(1)
        await wait_phase(supervisor, ShardPhase.FAILED)
        assert (await supervisor.stop()).phase is ShardPhase.STOPPED


@pytest.mark.parametrize("cancel_count", [1, 3])
@pytest.mark.parametrize("phase", ["starting", "running", "stopping"])
async def test_close_finishes_before_propagating_repeated_cancellation(
    managed_supervisor: Callable[..., ShardSupervisor],
    cancel_count: int,
    phase: str,
) -> None:
    existing_tasks = asyncio.all_tasks()
    gate = asyncio.Event()
    server = ProcessStub(
        start_gate=asyncio.Event() if phase == "starting" else None,
        stop_gate=asyncio.Event() if phase == "stopping" else None,
        kill_gate=gate,
    )
    supervisor = managed_supervisor("Forest", Factory(server))
    starting = asyncio.create_task(supervisor.start())
    await server.started.wait()
    stopping: asyncio.Task[ShardSupervisorStatus] | None = None
    if phase != "starting":
        await starting
    if phase == "stopping":
        stopping = asyncio.create_task(supervisor.stop())
        await server.stop_started.wait()
    closing = asyncio.create_task(supervisor.aclose())
    await server.kill_started.wait()

    for _ in range(cancel_count):
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
    with pytest.raises(RuntimeError, match="unavailable"):
        await supervisor.start()

    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await starting
    if stopping is not None:
        await stopping
    await supervisor.aclose()
    assert supervisor.status.phase is ShardPhase.UNAVAILABLE
    assert server.closed
    assert supervisor.server is None
    assert asyncio.all_tasks() <= existing_tasks


@pytest.mark.parametrize("operation", ["wait_for_action", "terminate"])
async def test_cancelled_operation_reclaims_temporary_waiters(
    operation: str,
) -> None:
    server = ProcessStub(stop_gate=asyncio.Event())
    supervisor = ShardSupervisor("Forest", Factory(server))
    external = asyncio.create_task(asyncio.Event().wait())
    existing_tasks = asyncio.all_tasks()
    if operation == "wait_for_action":
        pending = asyncio.create_task(supervisor._await_or_action(external))
    else:
        pending = asyncio.create_task(
            supervisor._terminate(cast(Server, server), force=False)
        )
    await asyncio.sleep(0)
    if operation == "terminate":
        await server.stop_started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    remaining_tasks = asyncio.all_tasks() - existing_tasks
    external.cancel()
    for task in remaining_tasks:
        task.cancel()
    await asyncio.gather(external, *remaining_tasks, return_exceptions=True)
    assert not remaining_tasks


def test_supervisor_rejects_empty_shard() -> None:
    with pytest.raises(ValueError, match="shard must not be empty"):
        ShardSupervisor("", Factory(ProcessStub()))
