import asyncio
import signal
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import cast

import pytest
from ulid import ULID

from dst_server.cluster.supervisor import (
    MAX_ATTEMPTS,
    RETRY_DELAY,
    STABLE_WINDOW,
    ShardDesired,
    ShardPhase,
    ShardSupervisor,
    ShardSupervisorStatus,
)
from dst_server.runtime import Server


class Clock:
    def __init__(self) -> None:
        self.pending: defaultdict[float, deque[asyncio.Event]] = defaultdict(deque)
        self.changed = asyncio.Event()

    async def __call__(self, duration: float) -> None:
        event = asyncio.Event()
        self.pending[duration].append(event)
        self.changed.set()
        await event.wait()

    async def release(self, duration: float) -> None:
        async with asyncio.timeout(1):
            while not self.pending[duration]:
                self.changed.clear()
                await self.changed.wait()
            self.pending[duration].popleft().set()
            await asyncio.sleep(0)


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
    *,
    attempts: int | None = None,
) -> ShardSupervisorStatus:
    async with asyncio.timeout(1):
        while supervisor.status.phase is not ShardPhase.UNAVAILABLE and (  # ruff: ignore[async-busy-wait]
            supervisor.status.phase is not phase
            or (attempts is not None and supervisor.status.attempts != attempts)
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
    started: list[object] = []
    stopped: list[object] = []
    supervisor = managed_supervisor(
        "Forest",
        factory,
        on_started=lambda server: _append(started, server),
        on_stopped=lambda server: _append(stopped, server),
    )

    running = await supervisor.start()
    assert running.phase is ShardPhase.RUNNING
    assert running.attempt_id == first.game_events.nonce
    assert (await supervisor.start()).attempt_id == running.attempt_id
    result = await getattr(supervisor, action)()

    assert (result.phase, result.desired) == (phase, desired)
    assert (first.stop_calls, first.kill_calls) == calls
    assert factory.calls == factory_calls
    assert started[0] is first
    assert stopped[0] is first


@pytest.mark.parametrize("stage", ["factory", "start", "exit"])
async def test_failures_share_one_retry_budget(
    managed_supervisor: Callable[..., ShardSupervisor],
    stage: str,
) -> None:
    clock = Clock()
    failed: list[object] = []
    servers = tuple(
        ProcessStub(
            start_error=RuntimeError("start failed") if stage == "start" else None
        )
        for _ in range(MAX_ATTEMPTS)
    )
    factory = Factory(
        *servers,
        error=RuntimeError("factory failed") if stage == "factory" else None,
    )
    supervisor = managed_supervisor(
        "Caves",
        factory,
        clock=clock,
        on_failed=lambda status: _append(failed, status),
    )
    starting = asyncio.create_task(supervisor.start())

    for attempt in range(1, MAX_ATTEMPTS):
        if stage == "exit":
            await wait_phase(supervisor, ShardPhase.RUNNING)
            servers[attempt - 1].exit(23)
        status = await wait_phase(
            supervisor,
            ShardPhase.RETRY_WAIT,
            attempts=attempt,
        )
        assert status.attempts == attempt
        await clock.release(RETRY_DELAY)
    if stage == "exit":
        await wait_phase(supervisor, ShardPhase.RUNNING)
        servers[-1].exit(23)

    status = (
        await wait_phase(supervisor, ShardPhase.FAILED)
        if stage == "exit"
        else await starting
    )
    assert status.phase is ShardPhase.FAILED
    assert status.attempts == MAX_ATTEMPTS
    assert failed == [status]


async def test_stable_window_resets_attempts(
    managed_supervisor: Callable[..., ShardSupervisor],
) -> None:
    first, second = ProcessStub(), ProcessStub()
    clock = Clock()
    supervisor = managed_supervisor("Forest", Factory(first, second), clock=clock)

    await supervisor.start()
    await clock.release(STABLE_WINDOW)
    await wait_phase(supervisor, ShardPhase.RUNNING, attempts=0)
    first.exit(1)

    assert (await wait_phase(supervisor, ShardPhase.RETRY_WAIT)).attempts == 0
    await clock.release(RETRY_DELAY)
    assert (await wait_phase(supervisor, ShardPhase.RUNNING)).attempts == 1


@pytest.mark.parametrize("action", ["stop", "kill", "restart", "aclose"])
async def test_action_interrupts_nonstable_startup(
    managed_supervisor: Callable[..., ShardSupervisor],
    action: str,
) -> None:
    server = ProcessStub(start_gate=asyncio.Event())
    supervisor = managed_supervisor("Forest", Factory(server, ProcessStub()))
    starting = asyncio.create_task(supervisor.start())
    await server.started.wait()
    await wait_phase(supervisor, ShardPhase.STARTING)

    await getattr(supervisor, action)()
    result = await starting

    if action == "restart":
        assert result.desired is ShardDesired.RUNNING
        assert supervisor.status.phase is ShardPhase.RUNNING
    elif action == "aclose":
        assert supervisor.status.phase is ShardPhase.UNAVAILABLE
    else:
        assert result.phase is ShardPhase.STOPPED


async def test_stop_wins_retry_completion_race(
    managed_supervisor: Callable[..., ShardSupervisor],
) -> None:
    first, second = ProcessStub(), ProcessStub()
    clock = Clock()
    factory = Factory(first, second)
    supervisor = managed_supervisor("Forest", factory, clock=clock)
    await supervisor.start()
    first.exit(1)
    await wait_phase(supervisor, ShardPhase.RETRY_WAIT)

    stopping = asyncio.create_task(supervisor.stop())
    await clock.release(RETRY_DELAY)

    assert (await stopping).phase is ShardPhase.STOPPED
    assert factory.calls == 1


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
    await server.stop_started.wait()

    await getattr(supervisor, action)()
    await stopping

    assert (server.stop_calls, server.kill_calls) == (1, 1)
    assert server.returncode == -signal.SIGKILL


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


@pytest.mark.parametrize("hook", ["started", "stopped", "failed"])
async def test_hook_failures_do_not_strand_the_supervisor(
    managed_supervisor: Callable[..., ShardSupervisor],
    hook: str,
) -> None:
    async def fail(_: object) -> None:
        await asyncio.sleep(0)
        message = "hook failed"
        raise RuntimeError(message)

    server = ProcessStub()
    clock = Clock()
    kwargs = {f"on_{hook}": fail}
    supervisor = managed_supervisor("Forest", Factory(server), clock=clock, **kwargs)

    if hook == "started":
        starting = asyncio.create_task(supervisor.start())
        assert (await wait_phase(supervisor, ShardPhase.RETRY_WAIT)).attempts == 1
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
    elif hook == "stopped":
        await supervisor.start()
        assert (await supervisor.stop()).phase is ShardPhase.STOPPED
    else:
        supervisor = managed_supervisor(
            "Caves",
            Factory(error=RuntimeError("factory failed")),
            clock=clock,
            on_failed=fail,
        )
        starting = asyncio.create_task(supervisor.start())
        for _ in range(MAX_ATTEMPTS - 1):
            await wait_phase(supervisor, ShardPhase.RETRY_WAIT)
            await clock.release(RETRY_DELAY)
        assert (await starting).phase is ShardPhase.FAILED


async def test_close_is_cancellation_safe(
    managed_supervisor: Callable[..., ShardSupervisor],
) -> None:
    gate = asyncio.Event()
    server = ProcessStub(kill_gate=gate)
    supervisor = managed_supervisor("Forest", Factory(server))
    await supervisor.start()
    closing = asyncio.create_task(supervisor.aclose())
    await server.kill_started.wait()

    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(RuntimeError, match="unavailable"):
        await supervisor.start()

    gate.set()
    await supervisor.aclose()
    assert supervisor.status.phase is ShardPhase.UNAVAILABLE


def test_supervisor_rejects_empty_shard() -> None:
    with pytest.raises(ValueError, match="shard must not be empty"):
        ShardSupervisor("", Factory(ProcessStub()))
