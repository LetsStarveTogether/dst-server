import asyncio
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from dst_server.events import GAME_EVENT_ADAPTER, GameEvent
from dst_server.events import server as server_events
from dst_server.game import DriverHealth
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.console import Console
from dst_server.runtime.driver import Driver
from tests.helpers import (
    StubServer,
    StubWriter,
    feed_frame,
    next_frame,
    structured_result,
)


def diagnostic(count: int) -> dict[str, str | int]:
    return {"stage": "callback", "message": "callback_failed", "count": count}


def health(
    events_emitted: int, *, generation: int = 0, errors: int = 0
) -> DriverHealth:
    return DriverHealth.model_validate({
        "protocol": 2,
        "generation": generation,
        "telemetry_status": "degraded" if errors else "active",
        "last_error": diagnostic(errors) if errors else None,
        "events_emitted": events_emitted,
        "errors": errors,
    })


def event(
    generation: int = 0, seq: int = 1, *, error_count: int | None = None
) -> GameEvent:
    return GAME_EVENT_ADAPTER.validate_python({
        "v": 2,
        "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "generation": generation,
        "session_id": "TEST",
        "seq": seq,
        "event": (
            "dst.telemetry.error"
            if error_count is not None
            else "dst.world.state_changed"
        ),
        "tick": 10,
        "monotonic_ms": 20,
        "cycle": 2,
        "data": (
            diagnostic(error_count)
            if error_count is not None
            else {"name": "cycles", "value": 2}
        ),
    })


class GatedReloadServer(StubServer):
    def __init__(self, responses: list[str], *, fail: bool = False) -> None:
        super().__init__(responses)
        self.fail = fail
        self.install_attempts = 0
        self.install_started = asyncio.Event()
        self.release_install = asyncio.Event()

    async def install_driver(self, generation: int) -> DriverHealth:
        if self.initial_install:
            self.initial_install = False
            return health(1, generation=generation)
        return await super().install_driver(generation)

    async def _execute(
        self,
        command: str,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> str:
        if "driver.install" in command:
            self.install_attempts += 1
            self.install_started.set()
            await self.release_install.wait()
            if self.fail:
                msg = "reload failed"
                raise RuntimeError(msg)
        return await super()._execute(command, generation_is_current)


async def test_typed_request_waits_for_current_driver_generation() -> None:
    server = await GatedReloadServer([
        structured_result(health(2, generation=1).model_dump(mode="json")),
        structured_result(data=True),
    ]).initialize()

    async with asyncio.timeout(1):
        server.driver.session_started(1)
        request = asyncio.create_task(server.game.world.request_save())
        await server.install_started.wait()

        with pytest.raises(RuntimeError, match="not been installed"):
            _ = server.driver_health

        server.release_install.set()
        await request

    assert server.driver.installed_generation == 1
    assert server.driver.task is not None
    assert server.driver.task.done()
    assert server.driver_health.events_emitted == 2
    assert len(server.commands) == 2


async def test_failed_reload_blocks_requests_without_retrying() -> None:
    server = await GatedReloadServer([], fail=True).initialize()

    async with asyncio.timeout(1):
        server.driver.session_started(1)
        request = asyncio.create_task(server.game.world.request_save())
        await server.install_started.wait()
        server.release_install.set()

        with pytest.raises(RuntimeError, match="unavailable for generation 1"):
            await request
        with pytest.raises(RuntimeError, match="unavailable for generation 1"):
            await server.game.world.request_save()

    with pytest.raises(RuntimeError, match="not been installed"):
        _ = server.driver_health
    assert server.install_attempts == 1
    assert server.commands == []


async def test_new_generation_is_not_stranded_by_previous_failure() -> None:
    attempts: list[int] = []
    install_started = asyncio.Event()
    release_install = asyncio.Event()

    async def install(generation: int) -> DriverHealth:
        attempts.append(generation)
        if len(attempts) == 1:
            install_started.set()
            await release_install.wait()
            msg = "first generation failed"
            raise RuntimeError(msg)
        return health(len(attempts), generation=generation)

    server = await StubServer([]).initialize()
    server.driver.install_driver = install

    async with asyncio.timeout(1):
        server.driver.session_started(1)
        await install_started.wait()
        server.driver.session_started(2)
        release_install.set()
        await server.driver.wait_ready()

    assert attempts == [1, 2]
    assert server.driver.installed_generation == 2


@pytest.mark.parametrize("initial", [True, False], ids=["initial", "reload"])
async def test_new_session_recovers_after_installation_has_failed(
    initial: bool,
) -> None:
    install = AsyncMock(return_value=health(0))
    driver = Driver(install, "cluster", "shard")
    generation = 0 if initial else 1
    if not initial:
        await driver.install(0)
    install.side_effect = RuntimeError("installation failed")
    driver.session_started(generation)
    operation = driver.install(generation) if initial else driver.wait_ready()
    with pytest.raises(RuntimeError):
        await operation

    attempts = install.await_count
    driver.session_started(generation)
    with pytest.raises(RuntimeError):
        await driver.wait_ready()
    assert install.await_count == attempts

    install.side_effect = None
    install.return_value = health(1, generation=generation + 1)
    driver.session_started(generation + 1)
    assert await asyncio.wait_for(driver.wait_ready(), 1) == generation + 1
    assert install.await_count == attempts + 1


async def test_initial_install_is_shared_and_survives_waiter_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def install(generation: int) -> DriverHealth:
        entered.set()
        await release.wait()
        return health(0, generation=generation)

    callback = AsyncMock(side_effect=install)
    driver = Driver(callback, "cluster", "shard")
    installing = asyncio.create_task(driver.install(0))
    await asyncio.wait_for(entered.wait(), 1)
    driver.observe_event(event(seq=4))
    assert not driver.is_ready(0)
    duplicate = asyncio.create_task(driver.install(0))
    waiting = asyncio.create_task(driver.wait_ready())
    try:
        await asyncio.sleep(0)
        assert not waiting.done()
        duplicate.cancel()
        with pytest.raises(asyncio.CancelledError):
            await duplicate
        release.set()
        assert await asyncio.wait_for(waiting, 1) == 0
        assert (await installing).events_emitted == 4
        callback.assert_awaited_once_with(0)
    finally:
        driver.close()
        for task in (installing, duplicate, waiting):
            task.cancel()
        await asyncio.gather(installing, duplicate, waiting, return_exceptions=True)


async def test_session_while_request_waits_for_console_installs_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server(ServerConfig(shard="test"))
    writer = StubWriter()
    reader = asyncio.StreamReader()
    server.child = cast("asyncio.subprocess.Process", Mock(returncode=None))
    server.console = Console(
        cast("asyncio.StreamWriter", writer),
        reader,
        server.game_events,
    )
    server.lifecycle.handle(server_events.ReadyEvent(detail=""), lambda _: None)

    installing = asyncio.create_task(server.driver.install(0))
    install_start, install_end, install_command = await next_frame(writer)
    assert b"driver.install" in install_command
    feed_frame(
        reader,
        install_start,
        install_end,
        structured_result(health(1).model_dump(mode="json")).encode(),
    )
    await installing
    writer.commands.clear()

    ready_checked = asyncio.Event()
    wait_ready = server.driver.wait_ready

    async def observe_ready() -> int:
        generation = await wait_ready()
        ready_checked.set()
        return generation

    monkeypatch.setattr(server.driver, "wait_ready", observe_ready)
    await server.console.lock.acquire()
    request = asyncio.create_task(server.game.world.request_save())
    await ready_checked.wait()
    server.lifecycle.handle(
        server_events.SessionEvent(session_id="NEW"),
        server.driver.session_started,
    )
    server.console.lock.release()

    install_start, install_end, install_command = await next_frame(writer)
    assert b"driver.install" in install_command
    feed_frame(
        reader,
        install_start,
        install_end,
        structured_result(health(2, generation=1).model_dump(mode="json")).encode(),
    )
    request_start, request_end, request_command = await next_frame(writer)
    assert b"save" in request_command
    feed_frame(
        reader,
        request_start,
        request_end,
        structured_result(data=True).encode(),
    )

    await asyncio.wait_for(request, 1)
    assert server.driver.installed_generation == 1
    assert server.driver_health.events_emitted == 2
    assert len(writer.commands) == 2


@pytest.mark.parametrize("old_fails", [False, True], ids=["success", "failure"])
async def test_initial_install_chases_generation_change(old_fails: bool) -> None:
    generations: list[int] = []
    started = (asyncio.Event(), asyncio.Event())
    release = (asyncio.Event(), asyncio.Event())

    async def install(generation: int) -> DriverHealth:
        attempt = len(generations)
        generations.append(generation)
        started[attempt].set()
        await release[attempt].wait()
        if attempt == 0 and old_fails:
            msg = "stale install failed"
            raise RuntimeError(msg)
        return health(attempt + 1, generation=generation)

    driver = Driver(install, "cluster", "shard")
    installing = asyncio.create_task(driver.install(0))
    await asyncio.wait_for(started[0].wait(), 1)
    driver.session_started(1)
    release[0].set()
    await asyncio.wait_for(started[1].wait(), 1)

    assert not installing.done()
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health

    release[1].set()
    result = await asyncio.wait_for(installing, 1)

    assert result.events_emitted == 2
    assert driver.health.events_emitted == 2
    assert driver.installed_generation == 1
    assert generations == [0, 1]


async def test_stale_reload_cannot_restore_health() -> None:
    generations: list[int] = []
    started = (asyncio.Event(), asyncio.Event())
    release = (asyncio.Event(), asyncio.Event())

    async def install(generation: int) -> DriverHealth:
        generations.append(generation)
        if len(generations) == 1:
            return health(1, generation=generation)
        attempt = len(generations) - 2
        started[attempt].set()
        await release[attempt].wait()
        if attempt == 1:
            msg = "current install failed"
            raise RuntimeError(msg)
        return health(attempt + 2, generation=generation)

    driver = Driver(install, "cluster", "shard")
    await driver.install(0)
    driver.session_started(1)
    task = driver.task
    assert task is not None
    await asyncio.wait_for(started[0].wait(), 1)

    driver.session_started(2)
    release[0].set()
    await asyncio.wait_for(started[1].wait(), 1)

    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health

    release[1].set()
    await asyncio.wait_for(task, 1)

    with pytest.raises(RuntimeError, match="unavailable for generation 2"):
        await driver.wait_ready()
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health
    assert driver.installed_generation == 0
    assert generations == [0, 1, 2]


async def test_get_health_updates_committed_health() -> None:
    server = await StubServer([
        structured_result(health(9).model_dump(mode="json")),
    ]).initialize()

    observed = await server.game.get_health()

    assert observed.events_emitted == 9
    assert server.driver_health.events_emitted == 9


async def test_closed_driver_does_not_commit_in_flight_install() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def install(_generation: int) -> DriverHealth:
        started.set()
        await release.wait()
        return health(1)

    driver = Driver(install, "cluster", "shard")
    installing = asyncio.create_task(driver.install(0))
    await asyncio.wait_for(started.wait(), 1)
    driver.close()
    release.set()

    with pytest.raises(RuntimeError, match="closed"):
        await installing
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health
    assert driver.installed_generation is None


async def test_closed_driver_discards_cancel_suppressing_refresh() -> None:
    started = asyncio.Event()
    installs = 0

    async def install(generation: int) -> DriverHealth:
        nonlocal installs
        installs += 1
        if installs == 1:
            return health(1, generation=generation)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return health(2, generation=generation)
        raise AssertionError

    driver = Driver(install, "cluster", "shard")
    await driver.install(0)
    driver.session_started(1)
    task = driver.task
    assert task is not None
    await asyncio.wait_for(started.wait(), 1)
    driver.close()
    await task

    assert driver.installed_generation == 0
    assert driver.task is task
    assert task.done()
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health


async def test_accepted_events_update_immutable_live_health() -> None:
    driver = Driver(AsyncMock(return_value=health(0)), "cluster", "shard")
    await driver.install(0)
    installed = driver.health
    driver.observe_event(event(seq=7))
    observed = driver.health
    driver.observe_event(event(seq=8, error_count=2))
    degraded = driver.health
    driver.observe_event(event(seq=9))

    assert installed.events_emitted == 0
    assert observed.events_emitted == 7
    assert observed.telemetry_status == "active"
    assert degraded.events_emitted == 8
    assert degraded.errors == 2
    assert degraded.telemetry_status == "degraded"
    assert degraded.last_error is not None
    assert degraded.last_error.model_dump() == diagnostic(2)
    assert driver.health.events_emitted == 9
    assert driver.health.errors == 2
    assert driver.health.last_error == degraded.last_error
    assert driver.health.telemetry_status == "degraded"


async def test_event_and_health_snapshots_never_regress_within_a_generation() -> None:
    driver = Driver(AsyncMock(return_value=health(0)), "cluster", "shard")
    await driver.install(0)
    driver.observe_event(event(seq=10, error_count=3))
    latest = driver.health

    driver.observe_event(event(seq=2, error_count=1))
    driver.observe_health(0, health(5, errors=2))
    driver.observe_health(0, health(7))

    assert driver.health == latest
    driver.observe_health(0, health(15, errors=4))
    assert driver.health == health(15, errors=4)
    driver.observe_event(event(seq=16, error_count=5))
    assert driver.health == health(16, errors=5)


@pytest.mark.parametrize("generation", [-1, 1])
async def test_event_and_health_from_other_generations_are_ignored(
    generation: int,
) -> None:
    driver = Driver(AsyncMock(return_value=health(1, generation=1)), "cluster", "shard")
    await driver.install(1)
    current = driver.health
    foreign_generation = driver.generation + generation

    driver.observe_event(event(foreign_generation, seq=99, error_count=4))
    driver.observe_health(
        foreign_generation, health(99, generation=foreign_generation, errors=4)
    )

    assert driver.health == current
    assert driver.installed_generation == 1


async def test_observations_cannot_make_uninstalled_driver_ready() -> None:
    driver = Driver(AsyncMock(return_value=health(0)), "cluster", "shard")
    driver.observe_event(event(error_count=1))
    driver.observe_health(0, health(1, errors=1))

    assert not driver.is_ready(0)
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health
    with pytest.raises(RuntimeError, match="not been installed"):
        await driver.wait_ready()

    await driver.install(0)
    assert driver.health == health(1, errors=1)


async def test_generation_change_discards_old_health_until_install_completes() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def install(generation: int) -> DriverHealth:
        if generation == 2:
            started.set()
            await release.wait()
        return health(0, generation=generation)

    driver = Driver(install, "cluster", "shard")
    await driver.install(1)
    driver.observe_event(event(1, seq=8, error_count=2))
    driver.session_started(2)
    await asyncio.wait_for(started.wait(), 1)
    driver.observe_health(1, health(20, generation=1, errors=5))
    driver.observe_event(event(1, seq=21, error_count=6))
    driver.observe_health(2, health(99, generation=2, errors=9))
    driver.observe_event(event(2, seq=1, error_count=1))

    assert not driver.is_ready(2)
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health

    release.set()
    assert await asyncio.wait_for(driver.wait_ready(), 1) == 2
    assert driver.health == health(1, generation=2, errors=1)
    driver.observe_event(event(2, seq=2))
    assert driver.health == health(2, generation=2, errors=1)


@pytest.mark.parametrize("generation", [0, 1])
async def test_duplicate_or_stale_session_does_not_invalidate_current_driver(
    generation: int,
) -> None:
    install = AsyncMock(return_value=health(3, generation=1))
    driver = Driver(install, "cluster", "shard")
    await driver.install(1)
    current = driver.health
    driver.session_started(generation)

    assert driver.generation == 1
    assert driver.is_ready(1)
    assert driver.health == current
    assert driver.task is not None
    assert driver.task.done()
    install.assert_awaited_once_with(1)


@pytest.mark.parametrize("status", ["disabled", "degraded", "failed"])
async def test_telemetry_status_does_not_disable_working_core_driver(
    status: str,
) -> None:
    snapshot = health(0, errors=0 if status == "disabled" else 1).replace(
        telemetry_status=status
    )
    driver = Driver(AsyncMock(return_value=snapshot), "cluster", "shard")
    await driver.install(0)

    assert driver.is_ready(0)
    assert await driver.wait_ready() == 0
    assert driver.health == snapshot


@pytest.mark.parametrize("status", ["disabled", "failed"])
@pytest.mark.parametrize("diagnostic_first", [False, True])
async def test_diagnostics_preserve_inactive_telemetry_status(
    status: str,
    diagnostic_first: bool,
) -> None:
    snapshot = health(0, errors=1 if status == "failed" else 0).replace(
        telemetry_status=status
    )
    driver = Driver(AsyncMock(return_value=snapshot), "cluster", "shard")
    if diagnostic_first:
        driver.observe_event(event(seq=1, error_count=1))
    await driver.install(0)
    if not diagnostic_first:
        driver.observe_event(event(seq=1, error_count=1))
    assert driver.health.telemetry_status == status

    driver.observe_event(event(seq=2, error_count=2))
    driver.observe_health(0, snapshot)
    assert driver.is_ready(0)
    assert driver.health == snapshot.replace(
        events_emitted=2,
        errors=2,
        last_error=diagnostic(2),
    )


async def test_close_invalidates_readiness_and_rejects_late_observations() -> None:
    driver = Driver(AsyncMock(return_value=health(1)), "cluster", "shard")
    await driver.install(0)
    driver.close()
    driver.observe_event(event(seq=2, error_count=1))
    driver.observe_health(0, health(3, errors=2))

    assert not driver.is_ready(0)
    with pytest.raises(RuntimeError, match="closed"):
        await driver.wait_ready()
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health


async def test_new_install_drops_pending_old_diagnostics() -> None:
    driver = Driver(AsyncMock(return_value=health(0, generation=1)), "cluster", "shard")
    driver.observe_event(event(0, seq=9, error_count=3))

    await driver.install(1)

    assert driver.health == health(0, generation=1)
    assert driver.is_ready(1)


@pytest.mark.parametrize("response_generation", [0, 2])
async def test_install_rejects_response_from_a_different_generation(
    response_generation: int,
) -> None:
    install = AsyncMock(return_value=health(4, generation=response_generation))
    driver = Driver(install, "cluster", "shard")

    with pytest.raises(RuntimeError, match="generation"):
        await asyncio.wait_for(driver.install(1), 1)

    assert not driver.is_ready(1)
    assert driver.installed_generation is None
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health
    install.assert_awaited_once_with(1)


async def test_reload_rejects_response_from_another_generation_without_retrying() -> (
    None
):
    install = AsyncMock(side_effect=[health(0), health(4)])
    driver = Driver(install, "cluster", "shard")
    await driver.install(0)
    driver.session_started(1)

    with pytest.raises(RuntimeError, match="unavailable for generation 1"):
        await asyncio.wait_for(driver.wait_ready(), 1)

    assert not driver.is_ready(1)
    assert driver.installed_generation == 0
    assert install.await_count == 2
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health


@pytest.mark.parametrize(
    ("observed_generation", "response_generation"), [(0, 1), (1, 0)]
)
async def test_observe_health_rejects_mismatched_response_generation(
    observed_generation: int, response_generation: int
) -> None:
    driver = Driver(AsyncMock(return_value=health(1)), "cluster", "shard")
    await driver.install(0)
    initial = driver.health

    driver.observe_health(
        observed_generation, health(99, generation=response_generation, errors=4)
    )

    assert driver.health == initial
