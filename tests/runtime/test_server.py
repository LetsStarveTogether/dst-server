import asyncio
import signal
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from dst_server import commands as c
from dst_server.errors import IndeterminateCommandError
from dst_server.events import server as server_events
from dst_server.events import world
from dst_server.models.driver import DriverHealth
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.console import Console, StaleGenerationError
from dst_server.runtime.lifecycle import Lifecycle
from dst_server.runtime.request import RequestState
from dst_server.telemetry import TelemetryProfile, TelemetrySettings
from tests.helpers import FAKE_SERVER, StubServer, structured_result, wait_for_event


class ReloadingServer(Server):
    def __init__(self) -> None:
        super().__init__(ServerConfig(shard="test"))
        self.installs = 0

    async def install_driver(self, generation: int) -> DriverHealth:
        self.installs += 1
        return DriverHealth.model_validate(
            {
                "protocol": 2,
                "generation": generation,
                "telemetry_status": "active",
                "last_error": None,
                "events_emitted": self.installs,
                "errors": 0,
            },
            strict=True,
        )


def make_fake_server(
    tmp_path: Path,
    shard: str,
    *,
    telemetry_profile: TelemetryProfile = "critical",
    log_handler: Callable[[str], None] | None = None,
) -> Server:
    executable = tmp_path / "fake-server"
    executable.write_text(FAKE_SERVER, encoding="utf-8")
    executable.chmod(0o755)
    return Server(
        ServerConfig(
            shard=shard,
            executable=executable,
            persistent_storage_root=tmp_path,
            conf_dir="conf",
            cluster="Cluster_1",
            ugc_directory=None,
            extra_args=(),
            telemetry=TelemetrySettings(profile=telemetry_profile),
        ),
        log_handler=log_handler,
    )


async def test_cloud_protocol_and_lifecycle(tmp_path: Path) -> None:
    logs: list[str] = []
    command_logged = asyncio.Event()

    def capture_log(line: str) -> None:
        logs.append(line)
        if line == "command received":
            command_logged.set()

    server = make_fake_server(
        tmp_path,
        "forest",
        telemetry_profile="history",
        log_handler=capture_log,
    )

    await server.start()

    assert server.driver_health.telemetry_status == "active"
    assert server.session_id == "TEST"
    observed = await server.read_game_event()
    assert observed is not None
    assert isinstance(observed.record, world.StateChangedEvent)
    assert observed.record.data.name == "cycles"
    assert observed.observed_timestamp_ns > 0
    assert await server.execute('print("hello")') == 'result:print("hello")'
    await asyncio.wait_for(command_logged.wait(), 1)
    assert "command received" in logs
    observed = await server.read_game_event()
    assert observed is not None
    assert observed.record.event == "dst.entity.death"
    event = await server.read_event()
    assert isinstance(event, server_events.SessionEvent)
    assert event.session_id == "TEST"
    with pytest.raises(ValueError, match="single line"):
        await server.execute("print(1)\nprint(2)")
    assert await server.stop() == -signal.SIGKILL
    event = await server.read_event()
    assert event is not None
    assert event.event == "shutdown"
    event = await server.read_event()
    assert isinstance(event, server_events.SavedEvent)
    assert event.snapshot == 1
    event = await server.read_event()
    assert event is not None
    assert event.event == "stopping"


def test_server_config() -> None:
    config = ServerConfig(shard="cave")
    command = config.command(monitor_parent_process=42)

    assert config.telemetry.profile == "critical"
    assert command[-3:] == ("42", "-skip_update_server_mods", "-cloudserver")
    assert command[command.index("-shard") + 1] == "cave"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shard", ""),
        ("shard", ".."),
        ("shard", "../forest"),
        ("shard", "forest\0cave"),
        ("extra_args", ("-flag\0value",)),
        ("monitor_parent_process", "false"),
        ("telemetry", {"profile": "unknown"}),
    ],
)
def test_server_configuration_rejects_invalid_boundaries(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        ServerConfig.model_validate({"shard": "forest", field: value})


def test_server_configuration_revalidates_copies_before_launch() -> None:
    config = ServerConfig(shard="forest")
    corrupted = config.model_copy(update={"extra_args": ("invalid\0argument",)})
    with pytest.raises(ValidationError):
        corrupted.command()


async def test_telemetry_install_failure_keeps_core_driver_running(
    tmp_path: Path,
) -> None:
    server = make_fake_server(
        tmp_path,
        "telemetry-failure",
        telemetry_profile="history",
    )
    async with server:
        assert server.driver_health.telemetry_status == "failed"
        assert server.returncode is None
        assert await server.game.invoke(c.ListPlayers()) == ()
        await server.game.request_save()
        assert server.returncode is None


async def test_core_driver_install_failure_degrades_without_stopping_game(
    tmp_path: Path,
) -> None:
    server = make_fake_server(
        tmp_path,
        "core-failure",
        telemetry_profile="history",
    )

    async with server:
        assert server.driver_error == "DST Lua request failed: lua_error"
        assert server.returncode is None
        with pytest.raises(RuntimeError, match="has not been installed"):
            _ = server.driver_health
        with pytest.raises(RuntimeError, match="has not been installed"):
            await server.game.invoke(c.ListPlayers())
        assert await server.execute('print("hello")') == 'result:print("hello")'

    assert server.returncode is not None
    assert server.closed is True


async def test_driver_result_eof_degrades_without_stopping_game(tmp_path: Path) -> None:
    server = make_fake_server(tmp_path, "driver-eof")

    async with server:
        assert server.driver_error == (
            "DST result stream closed before the command response completed"
        )
        assert server.returncode is None


async def test_failed_stdout_wakes_lifecycle_observer_before_process_exit(
    tmp_path: Path,
) -> None:
    server = make_fake_server(tmp_path, "forest")
    await server.start()
    assert isinstance(await server.read_event(), server_events.SessionEvent)
    failure = OSError("injected stdout read failure")
    stdout = server.process.stdout
    assert stdout is not None
    stdout.set_exception(failure)

    try:
        assert await asyncio.wait_for(server.read_event(), 1) is None
        assert server.returncode is None
    finally:
        with pytest.raises(OSError, match="injected stdout read failure") as raised:
            await server.kill()
        assert raised.value is failure
    assert server.closed


async def test_startup_timeout_cleans_up_process(tmp_path: Path) -> None:
    executable = tmp_path / "hanging-server"
    executable.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    executable.chmod(0o755)
    server = Server(
        ServerConfig(
            shard="timeout",
            executable=executable,
            persistent_storage_root=tmp_path,
            conf_dir="conf",
            cluster="Cluster_1",
            ugc_directory=None,
            extra_args=(),
        )
    )

    with pytest.raises(TimeoutError):
        await server.start(startup_timeout=0.2)

    assert server.returncode is not None
    assert server.closed is True


@pytest.mark.parametrize("phase", ["readiness", "driver"])
@pytest.mark.parametrize("cancellations", [1, 3])
async def test_cancelled_start_reaps_process_and_streams_before_propagating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    cancellations: int,
) -> None:
    existing_tasks = asyncio.all_tasks()
    server = make_fake_server(tmp_path, "startup-cancellation")
    startup, reaping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(*_: object) -> None:
        startup.set()
        await asyncio.Event().wait()

    if phase == "readiness":
        monkeypatch.setattr(server, "wait_ready", blocked)
    else:
        monkeypatch.setattr(server.driver, "install_driver", blocked)

    starting = asyncio.create_task(server.start())
    try:
        async with asyncio.timeout(2):
            await startup.wait()
            wait = server.process.wait

            async def slow_reap() -> int:
                reaping.set()
                await release.wait()
                return await wait()

            monkeypatch.setattr(server.process, "wait", slow_reap)
            starting.cancel()
            await reaping.wait()
            for _ in range(cancellations):
                starting.cancel()
                await asyncio.sleep(0)
                assert not starting.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await starting
        assert server.returncode is not None
        assert server.closed
        assert server.console is not None
        assert server.console.writer.is_closing()
        assert all(transport.is_closing() for transport in server.read_transports)
        assert asyncio.all_tasks() <= existing_tasks
    finally:
        release.set()
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
        if server.child is not None:
            await server.kill()


async def test_startup_failure_after_spawn_reaps_child_and_closes_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = make_fake_server(tmp_path, "startup-observation-failure")
    observe = server._observe_operational

    async def fail_once(event: str, *args: object, **kwargs: object) -> None:
        if event == "dst.server.process_started":
            message = "startup observation failed"
            raise RuntimeError(message)
        await observe(event, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(server, "_observe_operational", fail_once)
    try:
        with pytest.raises(RuntimeError, match="startup observation failed"):
            await server.start()
        assert server.child is not None
        assert server.returncode is not None
        assert server.closed
    finally:
        if server.child is not None:
            await server.kill()


async def test_start_validates_configuration_before_allocating_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server(ServerConfig(shard="forest"))
    server.config = server.config.model_copy(
        update={"extra_args": ("invalid\0argument",)}
    )
    allocate = Mock(side_effect=AssertionError("invalid config allocated pipes"))
    monkeypatch.setattr("dst_server.runtime.server.open_pipes", allocate)

    with pytest.raises(ValidationError):
        await server.start()
    allocate.assert_not_called()


async def test_startup_timeout_must_be_positive() -> None:
    server = Server(ServerConfig(shard="timeout"))

    with pytest.raises(ValueError, match="greater than 0"):
        await server.start(startup_timeout=0)

    assert server.child is None


async def test_execute_timeout_includes_server_readiness() -> None:
    process = Mock()
    process.returncode = None
    server = Server(ServerConfig(shard="execute-timeout"))
    server.child = cast("asyncio.subprocess.Process", process)

    with pytest.raises(TimeoutError):
        await server.execute("return true", completion_timeout=0.01)


async def test_stop_timeout_must_be_positive_before_signalling() -> None:
    process = Mock()
    process.returncode = None
    server = Server(ServerConfig(shard="stop-timeout"))
    server.child = cast("asyncio.subprocess.Process", process)

    with pytest.raises(ValueError, match="greater than 0"):
        await server.stop(grace_period=0)

    process.terminate.assert_not_called()


async def test_context_manager_kills_after_stop_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server(ServerConfig(shard="stop-timeout"))
    stop = AsyncMock(side_effect=TimeoutError("graceful stop timed out"))
    kill = AsyncMock(return_value=-signal.SIGKILL)
    monkeypatch.setattr(server, "stop", stop)
    monkeypatch.setattr(server, "kill", kill)

    with pytest.raises(TimeoutError, match="graceful stop timed out"):
        await server.__aexit__(None, None, None)

    stop.assert_awaited_once_with()
    kill.assert_awaited_once_with()


async def test_cancelled_stop_reaps_its_wait_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False
            self.killed = False
            self.exited = asyncio.Event()
            self.wait_started = asyncio.Event()
            self.wait_finished = asyncio.Event()

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -signal.SIGKILL
            self.exited.set()

        async def wait(self) -> int:
            self.wait_started.set()
            try:
                await self.exited.wait()
            finally:
                self.wait_finished.set()
            assert self.returncode is not None
            return self.returncode

    process = HangingProcess()
    server = Server(ServerConfig(shard="stop-cancel"))
    server.child = cast("asyncio.subprocess.Process", process)
    stopping_started = asyncio.Event()
    stopping_finished = asyncio.Event()
    wait_for_stopping = server.lifecycle.stopping.wait

    async def observe_stopping() -> None:
        stopping_started.set()
        try:
            await wait_for_stopping()
        finally:
            stopping_finished.set()

    monkeypatch.setattr(server.lifecycle.stopping, "wait", observe_stopping)
    stopping = asyncio.create_task(server.stop())
    try:
        watchdog = asyncio.timeout(5)
        async with watchdog:
            await wait_for_event(process.wait_started, stopping)
            await wait_for_event(stopping_started, stopping)
            stopping.cancel()

            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert process.terminated is True
            assert process.killed is True
            assert process.wait_finished.is_set()
            assert stopping_finished.is_set()
            assert server.closed is True
        assert not watchdog.expired()
    finally:
        async with asyncio.timeout(5):
            process.kill()
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
            await server.finish()


@pytest.mark.parametrize("cancellations", [1, 3])
async def test_cancelled_kill_finishes_reaping_before_propagating(
    cancellations: int,
) -> None:
    class SlowWaitProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.killed = False
            self.wait_started = asyncio.Event()
            self.release_wait = asyncio.Event()

        def kill(self) -> None:
            self.killed = True
            self.returncode = -signal.SIGKILL

        async def wait(self) -> int:
            self.wait_started.set()
            await self.release_wait.wait()
            assert self.returncode is not None
            return self.returncode

    process = SlowWaitProcess()
    server = Server(ServerConfig(shard="kill-cancel"))
    server.child = cast("asyncio.subprocess.Process", process)
    killing = asyncio.create_task(server.kill())
    try:
        await process.wait_started.wait()
        for _ in range(cancellations):
            killing.cancel()
            await asyncio.sleep(0)
            assert not killing.done()
        process.release_wait.set()
        with pytest.raises(asyncio.CancelledError):
            await killing
        assert process.killed is True
        assert server.closed is True
    finally:
        process.release_wait.set()
        await asyncio.gather(killing, return_exceptions=True)


@pytest.mark.parametrize("phase", ["console", "tasks"])
@pytest.mark.parametrize("cancellations", [1, 3])
async def test_finish_reclaims_all_resources_before_propagating_cancellation(
    phase: str,
    cancellations: int,
) -> None:
    existing_tasks = asyncio.all_tasks()
    entered, release = asyncio.Event(), asyncio.Event()

    class BlockingConsole:
        pending_result: asyncio.Task[str] | None = None

        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            if phase == "console":
                entered.set()
                await release.wait()

    async def background(stopped: asyncio.Event) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            if phase == "tasks":
                entered.set()
                await release.wait()
            stopped.set()

    server = Server(ServerConfig(shard="finish-cancel"))
    console = BlockingConsole()
    server.console = cast("Console", console)
    lifecycle_stopped = asyncio.Event()
    log_stopped = asyncio.Event()
    server.lifecycle_task = asyncio.create_task(background(lifecycle_stopped))
    server.log_task = asyncio.create_task(background(log_stopped))
    transport = Mock()
    server.read_transports = (transport,)
    await asyncio.sleep(0)
    finishing = asyncio.create_task(server.finish())
    try:
        async with asyncio.timeout(1):
            await entered.wait()
            for _ in range(cancellations):
                finishing.cancel()
                await asyncio.sleep(0)
                assert not finishing.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await finishing
        assert server.closed
        assert lifecycle_stopped.is_set()
        assert log_stopped.is_set()
        transport.close.assert_called_once()
        await server.finish()
        assert console.close_calls == 1
        assert asyncio.all_tasks() <= existing_tasks
    finally:
        release.set()
        await asyncio.gather(finishing, return_exceptions=True)
        await server.finish()


async def test_finish_closes_streams_when_pumps_never_started() -> None:
    server = Server(ServerConfig(shard="finish-before-pump"))
    event_reader = asyncio.StreamReader()
    log_reader = asyncio.StreamReader()
    server.lifecycle_task = asyncio.create_task(
        server.lifecycle.pump(event_reader, server.driver.session_started)
    )
    server.log_task = asyncio.create_task(server.pump_logs(log_reader))
    lifecycle_read = asyncio.create_task(server.read_event())
    game_read = asyncio.create_task(server.read_game_event())
    server.lifecycle_task.cancel()
    server.log_task.cancel()

    await server.finish()

    async with asyncio.timeout(1):
        assert await lifecycle_read is None
        assert await game_read is None
        assert await server.read_event() is None
        assert await server.read_game_event() is None
    assert server.lifecycle.eof is True


async def test_finish_does_not_cancel_driver_cleanup_twice() -> None:
    server = Server(ServerConfig(shard="driver-cleanup"))
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = asyncio.Event()

    async def install(_: int) -> DriverHealth:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()
        raise AssertionError

    server.driver.install_driver = install
    server.console = cast(
        Console,
        Mock(pending_result=None, close=AsyncMock(side_effect=cleaning.wait)),
    )
    installing = asyncio.create_task(server.driver.install(0))
    finishing: asyncio.Task[None] | None = None
    try:
        await wait_for_event(started, installing)
        finishing = asyncio.create_task(server.finish())
        await wait_for_event(cleaning, finishing)
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.shield(finishing), timeout=5)
        with pytest.raises(RuntimeError, match="closed"):
            await asyncio.wait_for(asyncio.shield(installing), timeout=5)
        assert cleaned.is_set()
        assert server.closed
    finally:
        async with asyncio.timeout(5):
            release.set()
            pending = (installing,) if finishing is None else (installing, finishing)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if not server.closed:
                await server.finish()


async def test_save_waits_for_fd5_completion() -> None:
    server = await StubServer([structured_result(data=True)]).initialize()
    reader = asyncio.StreamReader()
    pump = asyncio.create_task(
        server.lifecycle.pump(reader, server.driver.session_started)
    )
    request_complete = asyncio.Event()
    request_save = server.game.request_save
    request_state = RequestState()

    async def observe_request() -> None:
        request_state.mark_sent()
        await request_save()
        request_complete.set()

    saving = asyncio.create_task(server._save(observe_request, 30, request_state))
    try:
        async with asyncio.timeout(5):
            await wait_for_event(request_complete, saving, pump)

            reader.feed_data(b"DST_Saved|session/TEST/27\n")

            saved = await saving
            reader.feed_eof()
            await pump
            assert saved.snapshot == 27
    finally:
        async with asyncio.timeout(5):
            reader.feed_eof()
            saving.cancel()
            pump.cancel()
            await asyncio.gather(saving, pump, return_exceptions=True)
            await server.finish()


async def test_save_timeout_includes_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = await StubServer([]).initialize()
    request_started = asyncio.Event()

    async def request_save() -> None:
        request_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(server.game, "request_save", request_save)

    with pytest.raises(TimeoutError):
        await server.save(completion_timeout=0.01)
    assert request_started.is_set()


@pytest.mark.parametrize("phase", ["lock", "barrier", "request"])
async def test_lifecycle_save_timeout_covers_every_wait(phase: str) -> None:
    lifecycle = Lifecycle()
    request = AsyncMock(side_effect=asyncio.Event().wait)
    if phase == "lock":
        await lifecycle.save_lock.acquire()
    elif phase == "barrier":
        lifecycle._save_confirmation_barrier = RequestState(sent=True)
    watchdog = asyncio.timeout(1)

    try:
        with pytest.raises(TimeoutError):
            async with watchdog:
                await lifecycle.wait_for_save(request, completion_timeout=0.01)
        assert not watchdog.expired()
    finally:
        if lifecycle.save_lock.locked():
            lifecycle.save_lock.release()
        lifecycle.close()

    if phase == "request":
        request.assert_awaited_once()
    else:
        request.assert_not_awaited()


async def test_save_prewrite_failure_does_not_create_confirmation_barrier() -> None:
    server = Server(ServerConfig(shard="save-prewrite-failure"))

    async with asyncio.timeout(1):
        for _ in range(2):
            with pytest.raises(RuntimeError, match="not been installed"):
                await server.save()


async def test_concurrent_saves_wait_for_separate_confirmations() -> None:
    lifecycle = Lifecycle()
    first_request_started = asyncio.Event()
    release_first_request = asyncio.Event()
    second_call_started = asyncio.Event()
    second_request_started = asyncio.Event()
    requests = 0

    async def request() -> None:
        nonlocal requests
        requests += 1
        if requests == 1:
            first_request_started.set()
            await release_first_request.wait()
        else:
            second_request_started.set()

    async def wait_for_save(
        started: asyncio.Event | None = None,
    ) -> server_events.SavedEvent:
        if started is not None:
            started.set()
        return await lifecycle.wait_for_save(request, 1)

    first = asyncio.create_task(wait_for_save())
    second: asyncio.Task[server_events.SavedEvent] | None = None
    try:
        async with asyncio.timeout(5):
            await wait_for_event(first_request_started, first)
            second = asyncio.create_task(wait_for_save(second_call_started))
            await wait_for_event(second_call_started, second)

            assert not second_request_started.is_set()

            release_first_request.set()
            lifecycle.handle(
                server_events.SavedEvent(path="session/TEST/1", snapshot=1),
                lambda _: None,
            )
            assert (await first).snapshot == 1

            await wait_for_event(second_request_started, second)
            assert not second.done()
            lifecycle.handle(
                server_events.SavedEvent(path="session/TEST/2", snapshot=2),
                lambda _: None,
            )
            assert (await second).snapshot == 2
    finally:
        async with asyncio.timeout(5):
            release_first_request.set()
            pending = (first,) if second is None else (first, second)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


async def test_save_accepts_confirmation_before_request_returns() -> None:
    lifecycle = Lifecycle()
    expected = server_events.SavedEvent(path="session/REQUEST/1", snapshot=1)

    async def request() -> None:  # ruff:ignore[unused-async]
        lifecycle.handle(expected, lambda _: None)

    assert await lifecycle.wait_for_save(request, 1) == expected


async def test_save_ignores_confirmation_before_command_write() -> None:
    lifecycle = Lifecycle()
    state = RequestState()
    request_started = asyncio.Event()
    write_command = asyncio.Event()

    async def request() -> None:
        request_started.set()
        await write_command.wait()
        state.mark_sent()

    saving = asyncio.create_task(lifecycle.wait_for_save(request, 1, state))
    try:
        async with asyncio.timeout(5):
            await wait_for_event(request_started, saving)
            lifecycle.handle(
                server_events.SavedEvent(path="session/AUTOSAVE/1", snapshot=1),
                lambda _: None,
            )
            write_command.set()
            await asyncio.sleep(0)
            assert not saving.done()

            expected = server_events.SavedEvent(path="session/REQUEST/2", snapshot=2)
            lifecycle.handle(expected, lambda _: None)
            assert await saving == expected
    finally:
        async with asyncio.timeout(5):
            write_command.set()
            saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)


async def test_save_ignores_confirmation_from_rejected_attempt() -> None:
    lifecycle = Lifecycle()
    state = RequestState()

    async def request() -> None:  # ruff:ignore[unused-async]
        state.mark_sent()
        lifecycle.handle(
            server_events.SavedEvent(path="session/AUTOSAVE/1", snapshot=1),
            lambda _: None,
        )
        state.mark_rejected()
        state.mark_sent()

    saving = asyncio.create_task(lifecycle.wait_for_save(request, 1, state))
    await asyncio.sleep(0)
    assert not saving.done()

    expected = server_events.SavedEvent(path="session/REQUEST/2", snapshot=2)
    lifecycle.handle(expected, lambda _: None)
    assert await saving == expected


async def test_save_rejects_confirmation_when_request_was_not_executed() -> None:
    lifecycle = Lifecycle()
    state = RequestState()

    async def request() -> None:  # ruff:ignore[unused-async]
        state.mark_sent()
        lifecycle.handle(
            server_events.SavedEvent(path="session/AUTOSAVE/1", snapshot=1),
            lambda _: None,
        )
        state.mark_rejected()

    with pytest.raises(TimeoutError):
        await lifecycle.wait_for_save(request, 0.01, state)


async def test_save_keeps_first_confirmation_for_attempt() -> None:
    lifecycle = Lifecycle()
    first = server_events.SavedEvent(path="session/REQUEST/1", snapshot=1)

    async def request() -> None:  # ruff:ignore[unused-async]
        lifecycle.handle(first, lambda _: None)
        lifecycle.handle(
            server_events.SavedEvent(path="session/OTHER/2", snapshot=2),
            lambda _: None,
        )

    assert await lifecycle.wait_for_save(request, 1) == first


@pytest.mark.parametrize("rejected", [False, True])
async def test_requests_do_not_share_save_confirmation(rejected: bool) -> None:
    first, second = RequestState(sent=True), RequestState(sent=True)
    first.resolved.set()
    waiting = asyncio.create_task(second.resolved.wait())
    await asyncio.sleep(0)
    assert not waiting.done()

    if rejected:
        second.mark_rejected()
        assert not second.sent
    else:
        second.resolved.set()
    await waiting


async def test_failed_retry_blocks_next_save_until_late_confirmation() -> None:
    lifecycle = Lifecycle()
    state = RequestState()

    async def failed_request() -> None:  # ruff:ignore[unused-async]
        state.mark_sent()
        lifecycle.handle(
            server_events.SavedEvent(path="session/AUTOSAVE/1", snapshot=1),
            lambda _: None,
        )
        state.mark_rejected()
        state.mark_sent()
        msg = "failed after retry write"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="failed after retry write"):
        await lifecycle.wait_for_save(failed_request, 1, state)

    second_request_started = asyncio.Event()

    async def second_request() -> None:  # ruff:ignore[unused-async]
        second_request_started.set()

    saving = asyncio.create_task(lifecycle.wait_for_save(second_request, 1))
    try:
        async with asyncio.timeout(5):
            await asyncio.sleep(0)
            assert not second_request_started.is_set()

            lifecycle.handle(
                server_events.SavedEvent(path="session/LATE/2", snapshot=2),
                lambda _: None,
            )
            await wait_for_event(second_request_started, saving)
            expected = server_events.SavedEvent(path="session/REQUEST/3", snapshot=3)
            lifecycle.handle(expected, lambda _: None)
            assert await saving == expected
    finally:
        async with asyncio.timeout(5):
            saving.cancel()
            await asyncio.gather(saving, return_exceptions=True)


@pytest.mark.parametrize("failure", ["cancel", "timeout", "error"])
async def test_incomplete_save_waits_for_late_event_before_next_request(
    failure: str,
) -> None:
    lifecycle = Lifecycle()
    request_started = (asyncio.Event(), asyncio.Event())
    second_call_started = asyncio.Event()
    requests = 0

    async def request() -> None:  # ruff:ignore[unused-async]
        nonlocal requests
        request_started[requests].set()
        requests += 1
        if failure == "error" and requests == 1:
            msg = "request failed after write"
            raise RuntimeError(msg)

    saving = asyncio.create_task(
        lifecycle.wait_for_save(request, 0 if failure == "timeout" else 60)
    )
    second: asyncio.Task[server_events.SavedEvent] | None = None
    try:
        watchdog = asyncio.timeout(5)
        async with watchdog:
            if failure == "cancel":
                await wait_for_event(request_started[0], saving)
                saving.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await saving
            elif failure == "timeout":
                with pytest.raises(TimeoutError):
                    await saving
            else:
                with pytest.raises(RuntimeError, match="failed after write"):
                    await saving
            assert request_started[0].is_set()

            async def save_again() -> server_events.SavedEvent:
                second_call_started.set()
                return await lifecycle.wait_for_save(request, 1)

            second = asyncio.create_task(save_again())
            await wait_for_event(second_call_started, second)
            assert requests == 1

            lifecycle.handle(
                server_events.SavedEvent(path="session/LATE/1", snapshot=1),
                lambda _: None,
            )
            await wait_for_event(request_started[1], second)
            assert not second.done()
            lifecycle.handle(
                server_events.SavedEvent(path="session/REQUEST/2", snapshot=2),
                lambda _: None,
            )
            assert (await second).path == "session/REQUEST/2"
            assert requests == 2
        assert not watchdog.expired()
    finally:
        async with asyncio.timeout(5):
            pending = (saving,) if second is None else (saving, second)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


async def test_readiness_followed_by_fd5_eof_is_not_startup_success() -> None:
    lifecycle = Lifecycle()
    reader = asyncio.StreamReader()
    reader.feed_data(b"DST_SessionId|TEST\n")
    reader.feed_eof()

    await lifecycle.pump(reader, lambda _: None)

    assert lifecycle.ready is True
    with pytest.raises(EOFError, match="closed before the server became ready"):
        await lifecycle.wait_ready()


async def test_lifecycle_discards_whole_oversized_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = Lifecycle()
    reader = asyncio.StreamReader(limit=32)
    overrun_drained = asyncio.Event()
    readexactly = reader.readexactly

    async def track_overrun(size: int) -> bytes:
        result = await readexactly(size)
        overrun_drained.set()
        return result

    monkeypatch.setattr(reader, "readexactly", track_overrun)
    generations: list[int] = []
    pump = asyncio.create_task(lifecycle.pump(reader, generations.append))

    reader.feed_data(b"x" * 33)
    await asyncio.wait_for(overrun_drained.wait(), 1)
    reader.feed_data(b"DST_SessionId|FAKE\nDST_SessionId|REAL\n")
    reader.feed_eof()
    await pump

    assert lifecycle.session_id == "REAL"
    assert generations == [1]


async def test_log_pump_survives_oversized_line_and_handler_failure() -> None:
    observed: list[str] = []

    def capture(line: str) -> None:
        observed.append(line)
        if line == "handler-failure":
            msg = "injected log handler failure"
            raise RuntimeError(msg)

    server = Server(ServerConfig(shard="logs"), log_handler=capture)
    reader = asyncio.StreamReader(limit=32)
    reader.feed_data(b"x" * 33 + b"\nhandler-failure\nsentinel\n")
    reader.feed_eof()

    await server.pump_logs(reader)

    assert observed == ["handler-failure", "sentinel"]
    assert reader.at_eof()
    await server.finish()


async def test_fd5_eof_interrupts_save_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = Lifecycle()
    reader = asyncio.StreamReader()
    confirmation_started = asyncio.Event()
    request_complete = asyncio.Event()
    request_complete.set()
    wait = lifecycle.saved.wait
    pump = asyncio.create_task(lifecycle.pump(reader, lambda _: None))

    async def track_confirmation_wait() -> None:
        confirmation_started.set()
        await wait()

    async def request() -> None:
        await request_complete.wait()

    monkeypatch.setattr(lifecycle.saved, "wait", track_confirmation_wait)
    saving = asyncio.create_task(lifecycle.wait_for_save(request, 60))
    try:
        async with asyncio.timeout(5):
            await wait_for_event(confirmation_started, saving, pump)
            reader.feed_eof()

            with pytest.raises(EOFError, match="closed before save completed"):
                await asyncio.wait_for(saving, 1)
            await pump
    finally:
        async with asyncio.timeout(5):
            reader.feed_eof()
            saving.cancel()
            pump.cancel()
            await asyncio.gather(saving, pump, return_exceptions=True)


async def test_driver_is_reinstalled_after_lua_session_reload() -> None:
    server = ReloadingServer()
    await server.driver.install(0)
    reader = asyncio.StreamReader()
    pump = asyncio.create_task(
        server.lifecycle.pump(reader, server.driver.session_started)
    )

    reader.feed_data(b"DST_SessionId|ONE\nDST_SessionId|ONE\n")
    reader.feed_eof()
    await pump
    if server.driver.task is not None:
        await server.driver.task
    assert server.installs == 2
    assert server.driver_health.events_emitted == 2


async def test_reload_retries_only_before_write_and_waits_for_next_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ReloadingServer()
    await server.driver.install(0)
    written = asyncio.Event()
    attempts = 0

    async def execute(  # ruff:ignore[unused-async]
        command: str,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> str:
        nonlocal attempts
        del command
        attempts += 1
        assert generation_is_current is not None
        if attempts == 1:
            server._session_started(1)
            assert generation_is_current() is False
            msg = "generation changed before write"
            raise StaleGenerationError(msg)
        assert generation_is_current() is True
        written.set()
        return structured_result(data=True)

    monkeypatch.setattr(server, "_execute", execute)
    resetting = asyncio.create_task(server.game.invoke(c.Reset(timeout=1)))
    try:
        async with asyncio.timeout(5):
            await wait_for_event(written, resetting)
            await asyncio.sleep(0)

            assert attempts == 2
            assert server.driver.generation == 1
            assert not resetting.done()

            server._session_started(2)
            async with asyncio.timeout(1):
                await resetting
            assert server.installs == 3
    finally:
        async with asyncio.timeout(5):
            resetting.cancel()
            await asyncio.gather(resetting, return_exceptions=True)
            await server.finish()


async def test_reload_timeout_does_not_replay_written_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ReloadingServer()
    await server.driver.install(0)
    execute = AsyncMock(return_value=structured_result(data=True))
    monkeypatch.setattr(server, "_execute", execute)

    with pytest.raises(IndeterminateCommandError) as caught:
        await server.game.invoke(c.Reset(timeout=0.01))
    assert isinstance(caught.value.__cause__, TimeoutError)

    execute.assert_awaited_once()
    assert server.driver.generation == 0


async def test_failed_reload_response_is_reported_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ReloadingServer()
    await server.driver.install(0)
    execute = AsyncMock(
        return_value='DST_SERVER_RESULT|{"ok":false,"error":"lua_error"}'
    )
    monkeypatch.setattr(server, "_execute", execute)

    with pytest.raises(RuntimeError, match="lua_error"):
        await server.game.invoke(c.Reset(timeout=0.01))

    execute.assert_awaited_once()
