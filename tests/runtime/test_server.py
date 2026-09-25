import asyncio
import signal
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import JsonValue, ValidationError

from dst_server import commands as c
from dst_server.errors import IndeterminateCommandError
from dst_server.events import server as server_events
from dst_server.events import world
from dst_server.models.driver import DriverFailed, DriverHealth, DriverReady
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.console import Console, StaleGenerationError
from dst_server.runtime.lifecycle import Lifecycle
from dst_server.telemetry import TelemetryProfile, TelemetrySettings
from dst_server.timeouts import timeout_scope
from tests.helpers import wait_for_event
from tests.runtime.helpers import FAKE_SERVER, structured_result


async def native_ready(server: Server, generation: int) -> None:
    await server._observe_driver(
        DriverReady(
            nonce=server.game_events.nonce,
            health=DriverHealth(
                protocol=3,
                generation=generation,
                telemetry_status="active",
                last_error=None,
                events_emitted=generation + 1,
                errors=0,
            ),
        )
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

    try:
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
        assert await server.execute("print(1)\nprint(2)") == "result:print(1)\nprint(2)"
        assert await server.stop() == 0
        event = await server.read_event()
        assert event is not None
        assert event.event == "shutdown"
        event = await server.read_event()
        assert isinstance(event, server_events.SavedEvent)
        assert event.snapshot == 1
        event = await server.read_event()
        assert event is not None
        assert event.event == "stopping"
    finally:
        await server.kill()


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
        await server.game.invoke(c.Save())
        assert server.returncode is None


async def test_core_driver_failure_fails_startup_and_reaps_game(tmp_path: Path) -> None:
    server = make_fake_server(tmp_path, "core-failure", telemetry_profile="history")
    with pytest.raises(RuntimeError, match="installation_failed"):
        await server.start()
    assert server.driver_error == "installation_failed"
    assert server.returncode is not None
    assert server.closed
    assert not server.driver.is_ready(0)


async def test_missing_native_driver_ready_times_out_and_reaps_game(
    tmp_path: Path,
) -> None:
    server = make_fake_server(tmp_path, "driver-eof")
    with pytest.raises(TimeoutError):
        await server.start(startup_timeout=0.2)
    assert server.returncode is not None
    assert server.closed


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
        monkeypatch.setattr(server.driver, "wait_ready", blocked)

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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server(ServerConfig(shard="forest", persistent_storage_root=tmp_path))
    server.config = server.config.model_copy(
        update={"extra_args": ("invalid\0argument",)}
    )
    allocate = Mock(side_effect=AssertionError("invalid config allocated pipes"))
    monkeypatch.setattr("dst_server.runtime.server.open_pipes", allocate)

    with pytest.raises(ValidationError):
        await server.start()
    allocate.assert_not_called()
    assert not server.config.directory.exists()


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


@pytest.mark.parametrize(("request_timeout", "times_out"), [(1.0, False), (0.01, True)])
async def test_direct_game_call_respects_requested_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_timeout: float,
    times_out: bool,
) -> None:
    async with make_fake_server(tmp_path, "game-timeout") as server:
        # The first native Busy reply delays the retry past this inner default.
        monkeypatch.setattr("dst_server.runtime.server.DEFAULT_COMMAND_TIMEOUT", 0.02)
        if times_out:
            with pytest.raises(IndeterminateCommandError) as caught:
                await server.game.invoke(c.Save(timeout=request_timeout))
            assert isinstance(caught.value.__cause__, TimeoutError)
        else:
            assert await server.game.invoke(c.Save(timeout=request_timeout)) is None


async def test_game_request_timeout_is_not_extended_by_outer_operation(
    tmp_path: Path,
) -> None:
    async with (
        make_fake_server(tmp_path, "game-timeout") as server,
        timeout_scope(1),
    ):
        with pytest.raises(IndeterminateCommandError) as caught:
            await server.game.invoke(c.Save(timeout=0.01))
        assert isinstance(caught.value.__cause__, TimeoutError)


async def test_stop_timeout_must_be_positive_before_signalling() -> None:
    process = Mock()
    process.returncode = None
    server = Server(ServerConfig(shard="stop-timeout"))
    server.child = cast("asyncio.subprocess.Process", process)

    with pytest.raises(ValueError, match="greater than 0"):
        await server.stop(grace_period=0)

    process.terminate.assert_not_called()


async def test_native_stop_timeout_kills_and_reaps_before_raising() -> None:
    exited = asyncio.Event()
    process = Mock(returncode=None)

    async def wait() -> int:
        await exited.wait()
        assert isinstance(process.returncode, int)
        return process.returncode

    def kill() -> None:
        process.returncode = -signal.SIGKILL
        exited.set()

    process.kill.side_effect = kill
    process.wait = AsyncMock(side_effect=wait)
    server = Server(ServerConfig(shard="stop-timeout"))
    server.child = process

    with pytest.raises(TimeoutError):
        await server.stop(grace_period=0.01)

    assert server.closed
    assert server.returncode == -signal.SIGKILL
    process.terminate.assert_called_once_with()
    process.kill.assert_called_once_with()


async def test_context_manager_does_not_kill_again_after_reaped_stop_timeout(
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
    kill.assert_not_awaited()


async def test_cancelled_stop_waits_for_native_exit_without_killing() -> None:
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
    stopping = asyncio.create_task(server.stop())
    try:
        async with asyncio.timeout(5):
            await wait_for_event(process.wait_started, stopping)
            stopping.cancel()
            await asyncio.sleep(0)
            assert not stopping.done()
            assert not process.killed
            process.returncode = 0
            process.exited.set()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            assert process.terminated
            assert not process.killed
            assert process.wait_finished.is_set()
            assert server.closed
    finally:
        process.exited.set()
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
        reader_task: asyncio.Task[None] | None = None

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
    server.lifecycle_task = asyncio.create_task(server.lifecycle.pump(event_reader))
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


async def test_finish_wakes_native_driver_waiter() -> None:
    server = Server(ServerConfig(shard="driver-cleanup"))
    waiting = asyncio.create_task(server.driver.wait_ready())
    await asyncio.sleep(0)
    await server.finish()
    with pytest.raises(RuntimeError, match="closed"):
        await asyncio.wait_for(waiting, 1)
    assert server.closed
    await server.finish()


async def test_readiness_followed_by_fd5_eof_is_not_startup_success() -> None:
    lifecycle = Lifecycle()
    reader = asyncio.StreamReader()
    reader.feed_data(b"DST_SessionId|TEST\n")
    reader.feed_eof()

    await lifecycle.pump(reader)

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
    pump = asyncio.create_task(lifecycle.pump(reader))

    reader.feed_data(b"x" * 33)
    await asyncio.wait_for(overrun_drained.wait(), 1)
    reader.feed_data(b"DST_SessionId|FAKE\nDST_SessionId|REAL\n")
    reader.feed_eof()
    await pump

    assert lifecycle.session_id == "REAL"
    assert await lifecycle.read() == server_events.SessionEvent(session_id="REAL")
    assert await lifecycle.read() is None


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


async def test_late_fd5_session_does_not_invalidate_native_driver_generation() -> None:
    server = Server(ServerConfig(shard="test"))
    await native_ready(server, 2)
    for session_id in ("ONE", "TWO"):
        server.lifecycle.handle(server_events.SessionEvent(session_id=session_id))
    assert server.session_id == "TWO"
    assert server.driver.is_ready(2)
    assert server.driver_health.events_emitted == 3
    await server.finish()


async def test_fd5_eof_interrupts_native_driver_readiness() -> None:
    server = Server(ServerConfig(shard="test"))
    waiting = asyncio.create_task(server.driver.wait_ready())
    reader = asyncio.StreamReader()
    reader.feed_eof()
    await server._pump_lifecycle(reader)
    with pytest.raises(RuntimeError, match="closed"):
        await asyncio.wait_for(waiting, 1)
    await server.finish()


@pytest.mark.parametrize("generation", [None, 0])
async def test_stale_native_failure_does_not_disable_current_driver(
    generation: int | None,
) -> None:
    server = Server(ServerConfig(shard="test"))
    await native_ready(server, 1)
    observe = server.recorder.observe_log = Mock()
    await server._observe_driver(
        DriverFailed(
            nonce=server.game_events.nonce,
            generation=generation,
            error="installation_failed",
        )
    )
    assert server.driver.is_ready(1)
    assert server.driver_error is None
    observe.assert_not_called()
    await server.finish()


async def test_new_world_failure_prevents_driver_readiness() -> None:
    server = Server(ServerConfig(shard="test"))
    observe = server.recorder.observe_log = Mock()
    await native_ready(server, 0)
    try:
        await server._observe_driver(
            DriverFailed(
                nonce=server.game_events.nonce,
                generation=1,
                error="installation_failed",
            )
        )
        with pytest.raises(RuntimeError, match="installation_failed"):
            await server.driver.wait_ready()
        assert server.driver.generation == 1
        assert server.driver_error == "installation_failed"
        record = observe.call_args.kwargs
        assert record["event_name"] == "dst.runtime.diagnostic"
        assert record["body"]["generation"] == 1
    finally:
        await server.finish()


async def test_reload_retries_only_before_write_and_returns_on_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server(ServerConfig(shard="test"))
    await native_ready(server, 0)
    attempts = 0

    async def execute(
        method: str,
        arguments: dict[str, JsonValue],
        generation: int,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> bytes:
        nonlocal attempts
        assert method == "reset"
        assert arguments == {}
        assert generation == attempts
        attempts += 1
        assert generation_is_current is not None
        if attempts == 1:
            await native_ready(server, 1)
            assert generation_is_current() is False
            msg = "generation changed before write"
            raise StaleGenerationError(msg)
        assert generation_is_current() is True
        return structured_result(data=True)

    monkeypatch.setattr(server, "_execute", execute)
    try:
        await server.game.invoke(c.Reset(timeout=1))
        assert attempts == 2
        assert server.driver.generation == 1
    finally:
        await server.finish()


async def test_reload_timeout_does_not_replay_written_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server(ServerConfig(shard="test"))
    await native_ready(server, 0)
    execute = AsyncMock(side_effect=TimeoutError("response timed out"))
    monkeypatch.setattr(server, "_execute", execute)

    with pytest.raises(IndeterminateCommandError) as caught:
        await server.game.invoke(c.Reset(timeout=0.01))
    assert isinstance(caught.value.__cause__, TimeoutError)

    execute.assert_awaited_once()
    assert server.driver.generation == 0


async def test_failed_reload_response_is_reported_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = Server(ServerConfig(shard="test"))
    await native_ready(server, 0)
    execute = AsyncMock(return_value=b'{"ok":false,"error":"lua_error"}')
    monkeypatch.setattr(server, "_execute", execute)

    with pytest.raises(IndeterminateCommandError, match="could not be confirmed"):
        await server.game.invoke(c.Reset(timeout=0.01))

    execute.assert_awaited_once()
