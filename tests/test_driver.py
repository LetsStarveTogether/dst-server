from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast
from unittest.mock import Mock

import pytest

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


def health(events_emitted: int) -> DriverHealth:
    return DriverHealth(
        protocol=1,
        telemetry_status="active",
        telemetry_error=None,
        events_emitted=events_emitted,
        errors=0,
    )


class GatedReloadServer(StubServer):
    def __init__(self, responses: list[str], *, fail: bool = False) -> None:
        super().__init__(responses)
        self.fail = fail
        self.install_attempts = 0
        self.install_started = asyncio.Event()
        self.release_install = asyncio.Event()

    async def install_driver(self) -> DriverHealth:
        if self.initial_install:
            self.initial_install = False
            return health(1)
        return await super().install_driver()

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
        structured_result(health(2).model_dump(mode="json")),
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
    assert server.driver.task is None
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
    attempts = 0
    install_started = asyncio.Event()
    release_install = asyncio.Event()

    async def install() -> DriverHealth:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            install_started.set()
            await release_install.wait()
            msg = "first generation failed"
            raise RuntimeError(msg)
        return health(attempts)

    server = await StubServer([]).initialize()
    server.driver.install_driver = install

    async with asyncio.timeout(1):
        server.driver.session_started(1)
        await install_started.wait()
        server.driver.session_started(2)
        release_install.set()
        await server.driver.wait_ready()

    assert attempts == 2
    assert server.driver.installed_generation == 2


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
        structured_result(health(2).model_dump(mode="json")).encode(),
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
    attempts = 0
    started = (asyncio.Event(), asyncio.Event())
    release = (asyncio.Event(), asyncio.Event())

    async def install() -> DriverHealth:
        nonlocal attempts
        attempt = attempts
        attempts += 1
        started[attempt].set()
        await release[attempt].wait()
        if attempt == 0 and old_fails:
            msg = "stale install failed"
            raise RuntimeError(msg)
        return health(attempt + 1)

    driver = Driver(install, "cluster", "shard")
    installing = asyncio.create_task(driver.install(0))
    await started[0].wait()
    driver.session_started(1)
    release[0].set()
    await started[1].wait()

    assert not installing.done()
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health

    release[1].set()
    result = await asyncio.wait_for(installing, 1)

    assert result.events_emitted == 2
    assert driver.health.events_emitted == 2
    assert driver.installed_generation == 1
    assert attempts == 2


async def test_stale_reload_cannot_restore_health() -> None:
    attempts = -1
    started = (asyncio.Event(), asyncio.Event())
    release = (asyncio.Event(), asyncio.Event())

    async def install() -> DriverHealth:
        nonlocal attempts
        attempts += 1
        if attempts == 0:
            return health(1)
        attempt = attempts - 1
        started[attempt].set()
        await release[attempt].wait()
        if attempt == 1:
            msg = "current install failed"
            raise RuntimeError(msg)
        return health(attempt + 2)

    driver = Driver(install, "cluster", "shard")
    await driver.install(0)
    driver.session_started(1)
    task = driver.task
    assert task is not None
    await started[0].wait()

    driver.session_started(2)
    release[0].set()
    await started[1].wait()

    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health

    release[1].set()
    await asyncio.wait_for(task, 1)

    with pytest.raises(RuntimeError, match="unavailable for generation 2"):
        await driver.wait_ready()
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health
    assert driver.installed_generation == 0


async def test_get_health_does_not_replace_committed_health() -> None:
    server = await StubServer([
        structured_result(health(9).model_dump(mode="json")),
    ]).initialize()

    observed = await server.game.get_health()

    assert observed.events_emitted == 9
    assert server.driver_health.events_emitted == 0


async def test_closed_driver_does_not_commit_in_flight_install() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def install() -> DriverHealth:
        started.set()
        await release.wait()
        return health(1)

    driver = Driver(install, "cluster", "shard")
    installing = asyncio.create_task(driver.install(0))
    await started.wait()
    driver.close()
    release.set()

    with pytest.raises(RuntimeError, match="closed before installation completed"):
        await installing
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health
    assert driver.started is False


async def test_closed_driver_discards_cancel_suppressing_refresh() -> None:
    started = asyncio.Event()
    installs = 0

    async def install() -> DriverHealth:
        nonlocal installs
        installs += 1
        if installs == 1:
            return health(1)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return health(2)
        raise AssertionError

    driver = Driver(install, "cluster", "shard")
    await driver.install(0)
    driver.session_started(1)
    task = driver.task
    assert task is not None
    await started.wait()
    driver.close()
    await task

    assert driver.installed_generation == 0
    assert driver.task is None
    with pytest.raises(RuntimeError, match="not been installed"):
        _ = driver.health
