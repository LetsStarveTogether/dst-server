from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from dst_server.events import server as server_events
from dst_server.events import world
from dst_server.game import DriverHealth
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.console import Console, StaleGenerationError
from dst_server.runtime.lifecycle import Lifecycle, RequestState, RequestStatus
from dst_server.telemetry import TelemetrySettings
from tests.helpers import FAKE_SERVER, StubServer, structured_result


class ReloadingServer(Server):
    def __init__(self) -> None:
        super().__init__(ServerConfig(shard="test"))
        self.installs = 0

    async def install_driver(self) -> DriverHealth:
        self.installs += 1
        return DriverHealth.model_validate(
            {
                "protocol": 1,
                "telemetry_status": "active",
                "telemetry_error": None,
                "events_emitted": self.installs,
                "errors": 0,
            },
            strict=True,
        )


async def test_cloud_protocol_and_lifecycle(tmp_path: Path) -> None:
    executable = tmp_path / "fake-server"
    executable.write_text(FAKE_SERVER, encoding="utf-8")
    executable.chmod(0o755)
    config = ServerConfig(
        shard="forest",
        executable=executable,
        persistent_storage_root=tmp_path,
        conf_dir="conf",
        cluster="Cluster_1",
        ugc_directory=None,
        extra_args=(),
        telemetry=TelemetrySettings(profile="history"),
    )
    logs: list[str] = []
    command_logged = asyncio.Event()

    def capture_log(line: str) -> None:
        logs.append(line)
        if line == "command received":
            command_logged.set()

    server = Server(config, log_handler=capture_log)

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


async def test_telemetry_install_failure_keeps_core_driver_running(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-server"
    executable.write_text(FAKE_SERVER, encoding="utf-8")
    executable.chmod(0o755)
    server = Server(
        ServerConfig(
            shard="telemetry-failure",
            executable=executable,
            persistent_storage_root=tmp_path,
            conf_dir="conf",
            cluster="Cluster_1",
            ugc_directory=None,
            extra_args=(),
            telemetry=TelemetrySettings(profile="history"),
        )
    )
    async with server:
        assert server.driver_health.telemetry_status == "failed"
        assert server.returncode is None
        assert await server.game.players.list() == ()
        await server.game.world.request_save()
        assert server.returncode is None


async def test_core_driver_install_failure_degrades_without_stopping_game(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-server"
    executable.write_text(FAKE_SERVER, encoding="utf-8")
    executable.chmod(0o755)
    server = Server(
        ServerConfig(
            shard="core-failure",
            executable=executable,
            persistent_storage_root=tmp_path,
            conf_dir="conf",
            cluster="Cluster_1",
            ugc_directory=None,
            extra_args=(),
            telemetry=TelemetrySettings(profile="history"),
        )
    )

    async with server:
        assert server.driver_error == "DST Lua request failed: core install failed"
        assert server.returncode is None
        with pytest.raises(RuntimeError, match="has not been installed"):
            _ = server.driver_health
        with pytest.raises(RuntimeError, match="has not been installed"):
            await server.game.players.list()
        assert await server.execute('print("hello")') == 'result:print("hello")'

    assert server.returncode is not None
    assert server.closed is True


async def test_driver_result_eof_degrades_without_stopping_game(tmp_path: Path) -> None:
    executable = tmp_path / "fake-server"
    executable.write_text(FAKE_SERVER, encoding="utf-8")
    executable.chmod(0o755)
    server = Server(
        ServerConfig(
            shard="driver-eof",
            executable=executable,
            persistent_storage_root=tmp_path,
            conf_dir="conf",
            cluster="Cluster_1",
            ugc_directory=None,
            extra_args=(),
        )
    )

    async with server:
        assert server.driver_error == (
            "DST result stream closed before the command response completed"
        )
        assert server.returncode is None


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


async def test_startup_timeout_must_be_positive() -> None:
    server = Server(ServerConfig(shard="timeout"))

    with pytest.raises(ValueError, match="startup timeout must be positive"):
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

    with pytest.raises(ValueError, match="grace period timeout must be positive"):
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
    await process.wait_started.wait()
    await stopping_started.wait()
    stopping.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert process.terminated is True
    assert process.killed is True
    assert process.wait_finished.is_set()
    assert stopping_finished.is_set()
    assert server.closed is True


async def test_cancelled_kill_finishes_reaping_before_propagating() -> None:
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
    await process.wait_started.wait()
    killing.cancel()
    process.release_wait.set()

    with pytest.raises(asyncio.CancelledError):
        await killing
    assert process.killed is True
    assert server.closed is True


async def test_cancelled_finish_can_retry_cleanup() -> None:
    class BlockingConsole:
        pending_result: asyncio.Task[str] | None = None

        def __init__(self) -> None:
            self.close_started = asyncio.Event()
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                self.close_started.set()
                await asyncio.Event().wait()

    async def background(stopped: asyncio.Event) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    server = Server(ServerConfig(shard="finish-cancel"))
    console = BlockingConsole()
    server.console = cast("Console", console)
    lifecycle_stopped = asyncio.Event()
    log_stopped = asyncio.Event()
    server.lifecycle_task = asyncio.create_task(background(lifecycle_stopped))
    server.log_task = asyncio.create_task(background(log_stopped))
    finishing = asyncio.create_task(server.finish())
    await console.close_started.wait()
    finishing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await finishing
    assert server.closed is False
    assert lifecycle_stopped.is_set()
    assert log_stopped.is_set()

    await server.finish()

    assert server.closed is True
    assert console.close_calls == 2


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
    assert server.game_events.eof is True


async def test_save_waits_for_fd5_completion() -> None:
    server = await StubServer([structured_result(data=True)]).initialize()
    reader = asyncio.StreamReader()
    pump = asyncio.create_task(
        server.lifecycle.pump(reader, server.driver.session_started)
    )
    request_complete = asyncio.Event()
    request_save = server.game.world.request_save
    request_state = RequestState()

    async def observe_request() -> None:
        request_state.mark_sent()
        await request_save()
        request_complete.set()

    saving = asyncio.create_task(server._save(observe_request, 30, request_state))
    await request_complete.wait()

    reader.feed_data(b"DST_Saved|session/TEST/27\n")

    saved = await saving
    reader.feed_eof()
    await pump
    assert saved.snapshot == 27


async def test_save_timeout_includes_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = await StubServer([]).initialize()
    request_started = asyncio.Event()

    async def request_save() -> None:
        request_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(server.game.world, "request_save", request_save)

    with pytest.raises(TimeoutError):
        await server.save(completion_timeout=0.01)
    assert request_started.is_set()


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
    await first_request_started.wait()
    second = asyncio.create_task(wait_for_save(second_call_started))
    await second_call_started.wait()

    assert not second_request_started.is_set()

    release_first_request.set()
    lifecycle.handle(
        server_events.SavedEvent(path="session/TEST/1", snapshot=1),
        lambda _: None,
    )
    assert (await first).snapshot == 1

    await second_request_started.wait()
    assert not second.done()
    lifecycle.handle(
        server_events.SavedEvent(path="session/TEST/2", snapshot=2),
        lambda _: None,
    )
    assert (await second).snapshot == 2


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
    await request_started.wait()
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


async def test_shared_request_rejection_does_not_share_save_confirmation() -> None:
    status = RequestStatus(sent=True)
    first = RequestState(status)
    second = RequestState(status)
    first.resolved.set()
    waiting = asyncio.create_task(second.wait_resolved())
    await asyncio.sleep(0)
    assert not waiting.done()

    status.rejected.set()
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
    await asyncio.sleep(0)
    assert not second_request_started.is_set()

    lifecycle.handle(
        server_events.SavedEvent(path="session/LATE/2", snapshot=2),
        lambda _: None,
    )
    await second_request_started.wait()
    expected = server_events.SavedEvent(path="session/REQUEST/3", snapshot=3)
    lifecycle.handle(expected, lambda _: None)
    assert await saving == expected


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
    await request_started[0].wait()
    if failure == "cancel":
        saving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await saving
    elif failure == "timeout":
        with pytest.raises(TimeoutError):
            await saving
    else:
        with pytest.raises(RuntimeError, match="failed after write"):
            await saving

    async def save_again() -> server_events.SavedEvent:
        second_call_started.set()
        return await lifecycle.wait_for_save(request, 1)

    second = asyncio.create_task(save_again())
    await second_call_started.wait()
    assert requests == 1

    lifecycle.handle(
        server_events.SavedEvent(path="session/LATE/1", snapshot=1),
        lambda _: None,
    )
    await request_started[1].wait()
    assert not second.done()
    lifecycle.handle(
        server_events.SavedEvent(path="session/REQUEST/2", snapshot=2),
        lambda _: None,
    )
    assert (await second).path == "session/REQUEST/2"
    assert requests == 2


async def test_lifecycle_observation_queue_drops_oldest() -> None:
    lifecycle = Lifecycle()
    sessions: list[int] = []

    lifecycle.handle(server_events.ReadyEvent(detail=""), sessions.append)
    lifecycle.handle(server_events.SessionEvent(session_id="TEST"), sessions.append)
    lifecycle.handle(
        server_events.SavedEvent(path="session/TEST/1", snapshot=1),
        sessions.append,
    )
    lifecycle.handle(server_events.StoppingEvent(), sessions.append)
    for index in range(lifecycle.queue.maxsize):
        lifecycle.handle(server_events.UnknownEvent(line=str(index)), sessions.append)

    assert lifecycle.queue.qsize() == lifecycle.queue.maxsize
    assert lifecycle.ready is True
    assert lifecycle.session_id == "TEST"
    assert lifecycle.save_count == 1
    assert lifecycle.stopping.is_set()
    assert sessions == [1]
    assert await lifecycle.read() == server_events.UnknownEvent(line="0")


async def test_lifecycle_eof_survives_full_observation_queue() -> None:
    lifecycle = Lifecycle()
    reader = asyncio.StreamReader()
    for index in range(lifecycle.queue.maxsize + 1):
        reader.feed_data(f"unknown-{index}\n".encode())
    reader.feed_eof()

    await lifecycle.pump(reader, lambda _: None)

    observed = [await lifecycle.read() for _ in range(lifecycle.queue.maxsize)]
    assert observed[0] == server_events.UnknownEvent(line="unknown-2")
    assert observed[-1] is None
    assert await lifecycle.read() is None


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
    assert server.game_events.eof is False
    await server.finish()
    assert server.game_events.eof is True


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
    await confirmation_started.wait()
    reader.feed_eof()

    with pytest.raises(EOFError, match="closed before save completed"):
        await asyncio.wait_for(saving, 1)
    await pump


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
    resetting = asyncio.create_task(server.game.world.reset(completion_timeout=1))
    await written.wait()
    await asyncio.sleep(0)

    assert attempts == 2
    assert server.driver.generation == 1
    assert not resetting.done()

    server._session_started(2)
    async with asyncio.timeout(1):
        await resetting
    assert server.installs == 3


async def test_reload_timeout_does_not_replay_written_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ReloadingServer()
    await server.driver.install(0)
    execute = AsyncMock(return_value=structured_result(data=True))
    monkeypatch.setattr(server, "_execute", execute)

    with pytest.raises(TimeoutError):
        await server.game.world.reset(completion_timeout=0.01)

    execute.assert_awaited_once()
    assert server.driver.generation == 0


async def test_failed_reload_response_is_reported_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ReloadingServer()
    await server.driver.install(0)
    execute = AsyncMock(
        return_value='DST_SERVER_RESULT|{"ok":false,"error":"reload failed"}'
    )
    monkeypatch.setattr(server, "_execute", execute)

    with pytest.raises(RuntimeError, match="reload failed"):
        await server.game.world.reset(completion_timeout=0.01)

    execute.assert_awaited_once()
