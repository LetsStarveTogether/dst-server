from __future__ import annotations

import asyncio
import errno
import os
import select
import signal
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from dst_server.cluster import Shard, service
from dst_server.cluster import cli as cluster_cli
from dst_server.cluster.service import run
from dst_server.game.rpc import lua_string
from dst_server.runtime import Server, ServerConfig
from dst_server.telemetry import TelemetrySettings, otel
from tests.helpers import StubServer

FAKE_SERVER = r"""#!/usr/bin/env python3
import json
import os
import re
import signal
import sys

arguments = sys.argv[1:]
shard = arguments[arguments.index("-shard") + 1]
commands = os.fdopen(3)
results = os.fdopen(4, "w", buffering=1)
events = os.fdopen(5, "w", buffering=1)
events.write("DST_SessionId|TEST-" + shard + "\n")

def stop(signum, frame):
    del signum, frame
    events.write("DST_Shutdown\nDST_Saved|session/TEST/1\nDST_Stopping\n")

signal.signal(signal.SIGTERM, stop)

def start_frame(command):
    match = re.search(r"DST_SERVER_FRAME\|([0-9A-HJKMNP-TV-Z]{26})\|START", command)
    assert match is not None
    token = match.group(1)
    results.write(f"DST_SERVER_FRAME|{token}|START\n")
    return token

def finish_frame(token):
    results.write(
        f"DST_SERVER_FRAME|{token}|END\nDST_RemoteCommandDone\n"
    )

for command in commands:
    token = start_frame(command)
    if "driver.install" in command:
        failed = shard == "cave"
        health = {
            "protocol": 1,
            "telemetry_status": "failed" if failed else "active",
            "telemetry_error": "world hook unavailable" if failed else None,
            "events_emitted": 0,
            "errors": 0,
        }
        results.write("DST_SERVER_RESULT|" + json.dumps({"ok": True, "data": health}))
        results.write("\n")
        finish_frame(token)
        print("READY|" + shard, flush=True)
        continue
    print("COMMAND|" + shard + "|" + command.rstrip("\n"), flush=True)
    results.write("console-result\n")
    finish_frame(token)
"""


@pytest.mark.parametrize("profile", ["off", "critical", "history"])
def test_cli_reads_telemetry_profile_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    running = AsyncMock(return_value=7)
    monkeypatch.setenv("DST_SERVER_TELEMETRY_PROFILE", profile)
    monkeypatch.setattr(cluster_cli, "run", running)

    assert cluster_cli.main(()) == 7
    running.assert_awaited_once_with(
        telemetry=TelemetrySettings.model_validate({"profile": profile}),
        shard=None,
        external_port=None,
        update_mods=True,
    )


def test_cli_rejects_invalid_telemetry_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = AsyncMock()
    monkeypatch.setenv("DST_SERVER_TELEMETRY_PROFILE", "verbose")
    monkeypatch.setattr(cluster_cli, "run", running)

    with pytest.raises(
        ValueError, match="Input should be 'off', 'critical' or 'history'"
    ):
        cluster_cli.main(())

    running.assert_not_called()


def test_cli_prepares_once_and_runs_one_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparing = AsyncMock()
    running = AsyncMock(return_value=0)
    monkeypatch.setattr(cluster_cli, "prepare", preparing)
    monkeypatch.setattr(cluster_cli, "run", running)

    assert cluster_cli.main(("prepare",)) == 0
    preparing.assert_awaited_once_with()
    assert cluster_cli.main(("run", "cave")) == 0
    running.assert_awaited_once_with(
        telemetry=TelemetrySettings(),
        shard="cave",
        external_port=None,
        update_mods=False,
    )


def test_cli_validates_external_port(monkeypatch: pytest.MonkeyPatch) -> None:
    running = AsyncMock(return_value=0)
    monkeypatch.setattr(cluster_cli, "run", running)

    assert cluster_cli.main(("run", "--external-port", "30007", "cave")) == 0
    running.assert_awaited_once_with(
        telemetry=TelemetrySettings(),
        shard="cave",
        external_port=30007,
        update_mods=False,
    )
    with pytest.raises(SystemExit):
        cluster_cli.main(("run", "--external-port", "1023", "cave"))


def test_cli_accepts_an_option_like_shard_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    running = AsyncMock(return_value=0)
    monkeypatch.setattr(cluster_cli, "run", running)

    assert cluster_cli.main(("run", "--", "--help")) == 0
    running.assert_awaited_once_with(
        telemetry=TelemetrySettings(),
        shard="--help",
        external_port=None,
        update_mods=False,
    )


def test_otel_resource_uses_explicit_cluster_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline = object()
    configure = Mock(return_value=pipeline)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector")
    monkeypatch.setattr(otel, "configure", configure)

    config = ServerConfig(shard="forest", telemetry_cluster="dst-room-42")
    assert service.configure_otel(config) is pipeline
    configure.assert_called_once_with(
        resource_attributes={"dst.cluster.name": "dst-room-42"}
    )


def write_shard(path: Path, *, master: bool) -> None:
    path.mkdir()
    (path / "server.ini").write_text(
        f"[SHARD]\nis_master = {str(master).lower()}\n",
        encoding="utf-8",
    )
    (path / "modoverrides.lua").write_text("return {}", encoding="utf-8")


async def write_fifo_when_ready(path: Path, data: bytes) -> None:
    assert len(data) < select.PIPE_BUF
    while True:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
            await asyncio.sleep(0.01)
        else:
            break
    try:
        assert os.write(descriptor, data) == len(data)
    finally:
        os.close(descriptor)


class ServiceServer:
    def __init__(self, shard: str, *, exit_code: int | None = None) -> None:
        self.config = ServerConfig(shard=shard)
        self.process = Mock()
        self.process.returncode = None
        self.exit_code = exit_code
        self.exit = asyncio.Event()
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()

    async def wait(self) -> int:
        if self.exit_code is None:
            await self.stopped.wait()
        else:
            await self.exit.wait()
            self.process.returncode = self.exit_code
        assert self.process.returncode is not None
        return self.process.returncode

    async def stop(self) -> int:
        self.process.returncode = 0
        self.stopped.set()
        return 0

    async def kill(self) -> int:
        self.process.returncode = -signal.SIGKILL
        self.stopped.set()
        return self.process.returncode


async def test_startup_signal_cancels_prepare_and_removes_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    handlers: dict[signal.Signals, Callable[[], None]] = {}
    removed: list[signal.Signals] = []
    cancelled = asyncio.Event()

    def add_handler(value: signal.Signals, callback: Callable[[], None]) -> None:
        handlers[value] = callback

    def remove_handler(value: signal.Signals) -> bool:
        removed.append(value)
        return handlers.pop(value, None) is not None

    async def preparing(
        install_path: Path,
        cluster_path: Path,
        **_: object,
    ) -> tuple[tuple[Shard, ...], tuple[Server, ...]]:
        assert install_path == tmp_path / "install"
        assert cluster_path == tmp_path / "cluster"
        handlers[signal.SIGTERM]()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError

    monkeypatch.setattr(loop, "add_signal_handler", add_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", remove_handler)
    monkeypatch.setattr(service, "prepare", preparing)
    serving = AsyncMock()
    monkeypatch.setattr(service, "serve", serving)
    monkeypatch.chdir(tmp_path)

    assert (
        await service.run(
            install_path=Path("install"),
            cluster_path=Path("cluster"),
        )
        == 0
    )

    assert cancelled.is_set()
    serving.assert_not_awaited()
    assert set(removed) == {signal.SIGINT, signal.SIGTERM}


async def test_shutdown_propagates_prepare_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = asyncio.Event()

    async def preparing(
        _: Path,
        __: Path,
        **___: object,
    ) -> tuple[tuple[Shard, ...], tuple[Server, ...]]:
        shutdown.set()
        try:
            await asyncio.Event().wait()
        finally:
            msg = "prepare cleanup failed"
            raise RuntimeError(msg)

    monkeypatch.setattr(service, "prepare", preparing)

    with pytest.raises(RuntimeError, match="prepare cleanup failed"):
        await service.run(shutdown=shutdown)


async def test_external_cancellation_groups_prepare_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def preparing(
        _: Path,
        __: Path,
        **___: object,
    ) -> tuple[tuple[Shard, ...], tuple[Server, ...]]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            msg = "prepare cleanup failed"
            raise RuntimeError(msg)

    monkeypatch.setattr(service, "prepare", preparing)
    running = asyncio.create_task(service.run(shutdown=asyncio.Event()))
    async with asyncio.timeout(1):
        await started.wait()
    running.cancel()

    with pytest.raises(BaseExceptionGroup) as raised:
        async with asyncio.timeout(1):
            await running

    cancellation, cleanup = raised.value.exceptions
    assert isinstance(cancellation, asyncio.CancelledError)
    assert isinstance(cleanup, RuntimeError)
    assert str(cleanup) == "prepare cleanup failed"


async def test_preexisting_shutdown_skips_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown = asyncio.Event()
    shutdown.set()
    preparing = AsyncMock()
    monkeypatch.setattr(service, "prepare", preparing)
    serving = AsyncMock()
    monkeypatch.setattr(service, "serve", serving)

    assert await service.run(shutdown=shutdown) == 0

    preparing.assert_not_awaited()
    serving.assert_not_awaited()


@pytest.mark.parametrize("external_cancel", [False, True], ids=["signal", "cancel"])
async def test_wait_for_start_preserves_start_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    external_cancel: bool,
) -> None:
    started = asyncio.Event()
    shutdown = asyncio.Event()

    async def starting(_: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            msg = "start cleanup failed"
            raise RuntimeError(msg)

    monkeypatch.setattr(service, "start", starting)
    signal_task = asyncio.create_task(shutdown.wait())
    waiting = asyncio.create_task(service.wait_for_start((), signal_task, []))
    async with asyncio.timeout(1):
        await started.wait()
    if external_cancel:
        waiting.cancel()
        with pytest.raises(BaseExceptionGroup) as raised:
            async with asyncio.timeout(1):
                await waiting
        cancellation, cleanup = raised.value.exceptions
        assert isinstance(cancellation, asyncio.CancelledError)
        assert isinstance(cleanup, RuntimeError)
        assert str(cleanup) == "start cleanup failed"
    else:
        shutdown.set()
        with pytest.raises(RuntimeError, match="start cleanup failed"):
            async with asyncio.timeout(1):
                await waiting
    signal_task.cancel()
    async with asyncio.timeout(1):
        await asyncio.gather(signal_task, return_exceptions=True)


async def test_background_failure_stops_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ServiceServer("forest")
    events_started = asyncio.Event()

    async def consume(_: object) -> None:
        events_started.set()
        await asyncio.Event().wait()

    async def fail(_: Path, __: object) -> None:
        await events_started.wait()
        msg = "console forwarding failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(service, "configure_otel", lambda _: None)
    monkeypatch.setattr(service, "log_events", consume)
    monkeypatch.setattr(service.console, "forward", fail)
    shard = Shard(name="forest", master=True, console=tmp_path / "console")

    with pytest.raises(RuntimeError, match="console forwarding failed"):
        async with asyncio.timeout(1):
            await service.serve(
                (shard,),
                cast(tuple[Server, ...], (server,)),
                asyncio.Event(),
            )

    assert server.stopped.is_set()


async def test_background_normal_exit_stops_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ServiceServer("forest")
    consume = AsyncMock()

    async def forward(_: Path, __: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "configure_otel", lambda _: None)
    monkeypatch.setattr(service, "log_events", consume)
    monkeypatch.setattr(service.console, "forward", forward)
    shard = Shard(name="forest", master=True, console=tmp_path / "console")

    with pytest.raises(
        RuntimeError,
        match="critical background task stopped unexpectedly: dst-events-forest",
    ):
        async with asyncio.timeout(1):
            await service.serve(
                (shard,),
                cast(tuple[Server, ...], (server,)),
                asyncio.Event(),
            )

    assert server.stopped.is_set()


async def test_serve_groups_runtime_and_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ServiceServer("forest")
    events_started = asyncio.Event()
    events_cancelled = asyncio.Event()

    async def consume(_: object, __: object) -> None:
        events_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events_cancelled.set()

    async def fail(_: Path, __: object) -> None:
        await events_started.wait()
        msg = "console forwarding failed"
        raise RuntimeError(msg)

    stop_server = AsyncMock(side_effect=RuntimeError("stop failed"))
    monkeypatch.setattr(server, "stop", stop_server)
    pipeline = Mock()
    pipeline.shutdown = AsyncMock(side_effect=RuntimeError("pipeline shutdown failed"))
    monkeypatch.setattr(service, "configure_otel", lambda _: pipeline)
    monkeypatch.setattr(service, "export_events", consume)
    monkeypatch.setattr(service.console, "forward", fail)
    shard = Shard(name="forest", master=True, console=tmp_path / "console")

    with pytest.raises(ExceptionGroup) as raised:
        async with asyncio.timeout(1):
            await service.serve(
                (shard,),
                cast(tuple[Server, ...], (server,)),
                asyncio.Event(),
            )

    assert [str(error) for error in raised.value.exceptions] == [
        "console forwarding failed",
        "stop failed",
        "pipeline shutdown failed",
    ]
    stop_server.assert_awaited_once_with()
    pipeline.shutdown.assert_awaited_once_with()
    assert events_cancelled.is_set()


async def test_stop_propagates_failure_after_stopping_every_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = ServiceServer("failed")
    survivor = ServiceServer("survivor")
    failure = AsyncMock(side_effect=RuntimeError("stop failed"))
    monkeypatch.setattr(failed, "stop", failure)

    with pytest.raises(RuntimeError, match="stop failed"):
        await service.stop(cast(tuple[Server, ...], (failed, survivor)))

    failure.assert_awaited_once_with()
    assert survivor.stopped.is_set()


async def test_stop_one_kills_after_graceful_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ServiceServer("forest")
    graceful = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(server, "stop", graceful)

    await service.stop_one(cast(Server, server))

    graceful.assert_awaited_once_with()
    assert server.process.returncode == -signal.SIGKILL


async def test_abnormal_shard_exit_cleans_up_other_shards_and_background_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = ServiceServer("failed", exit_code=23)
    survivor = ServiceServer("survivor")
    background_started = asyncio.Event()
    never = asyncio.Event()
    expected = {
        ("events", "failed"),
        ("events", "survivor"),
        ("console", "failed"),
        ("console", "survivor"),
    }
    started: set[tuple[str, str]] = set()
    cancelled: set[tuple[str, str]] = set()

    async def background(kind: str, server: ServiceServer) -> None:
        key = (kind, server.config.shard)
        started.add(key)
        if started == expected:
            background_started.set()
        try:
            await never.wait()
        finally:
            cancelled.add(key)

    async def consume(server: ServiceServer) -> None:
        await background("events", server)

    async def forward(_: Path, server: ServiceServer) -> None:
        await background("console", server)

    monkeypatch.setattr(service, "configure_otel", lambda _: None)
    monkeypatch.setattr(service, "log_events", consume)
    monkeypatch.setattr(service.console, "forward", forward)
    shards = tuple(
        Shard(
            name=server.config.shard, master=index == 0, console=tmp_path / str(index)
        )
        for index, server in enumerate((failed, survivor))
    )
    servers = cast(tuple[Server, ...], (failed, survivor))
    serving = asyncio.create_task(service.serve(shards, servers, asyncio.Event()))

    try:
        async with asyncio.timeout(1):
            await failed.started.wait()
            await survivor.started.wait()
            await background_started.wait()
            failed.exit.set()
            assert await serving == 23
    finally:
        if not serving.done():
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)

    assert failed.process.returncode == 23
    assert survivor.stopped.is_set()
    assert cancelled == expected


@pytest.mark.parametrize("start_fails", [False, True], ids=["signal", "failure"])
async def test_event_consumers_start_before_shards_and_are_cleaned_up(
    monkeypatch: pytest.MonkeyPatch,
    start_fails: bool,
) -> None:
    server = await StubServer([]).initialize()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def consume(_: object) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def wait_for_start(_: object, __: object, ___: object) -> bool:
        await started.wait()
        if start_fails:
            msg = "start failed"
            raise RuntimeError(msg)
        return False

    monkeypatch.setattr(service, "configure_otel", lambda _: None)
    monkeypatch.setattr(service, "log_events", consume)
    monkeypatch.setattr(service, "wait_for_start", wait_for_start)

    if start_fails:
        with pytest.raises(RuntimeError, match="start failed"):
            async with asyncio.timeout(1):
                await service.serve((), (server,), asyncio.Event())
    else:
        async with asyncio.timeout(1):
            assert await service.serve((), (server,), asyncio.Event()) == 0

    assert cancelled.is_set()


async def test_export_events_does_not_guess_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = await StubServer([]).initialize()
    server.lifecycle.session_id = "CURRENT"
    observed = Mock()
    monkeypatch.setattr(
        server,
        "read_game_event",
        AsyncMock(side_effect=(observed, None)),
    )
    emit = Mock()
    monkeypatch.setattr(otel, "emit", emit)
    otel_logger = Mock()

    await service.export_events(server, otel_logger)

    emit.assert_called_once_with(
        otel_logger,
        observed,
        attributes={
            "dst.cluster.name": "cluster",
            "dst.shard.name": "test",
        },
    )


async def test_event_tasks_stay_local_without_a_log_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = ServiceServer("forest")
    local = AsyncMock()
    exported = AsyncMock()
    monkeypatch.setattr(service, "log_events", local)
    monkeypatch.setattr(service, "export_events", exported)

    tasks = service.start_event_tasks(
        cast(tuple[Server, ...], (server,)),
        Mock(logger=None),
    )
    await asyncio.gather(*tasks)

    local.assert_awaited_once_with(server)
    exported.assert_not_awaited()


async def test_service_runs_shards_and_falls_back_from_otlp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = tmp_path / "install"
    cluster = tmp_path / "cluster"
    executable = install / "bin64" / "dontstarve_dedicated_server_nullrenderer_x64"
    executable.parent.mkdir(parents=True)
    executable.write_text(FAKE_SERVER, encoding="utf-8")
    executable.chmod(0o755)
    cluster.mkdir()
    (cluster / "cluster.ini").touch()
    (cluster / "cluster_token.txt").touch()
    write_shard(cluster / "forest", master=True)
    write_shard(cluster / "cave", master=False)
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    configure = Mock(side_effect=RuntimeError("collector unavailable"))
    monkeypatch.setattr(otel, "configure", configure)
    ready = {name: asyncio.Event() for name in ("forest", "cave")}
    command_seen = asyncio.Event()
    observed_commands: list[str] = []

    def observe_log(_: str, **values: object) -> None:
        line = values.get("line")
        if not isinstance(line, str):
            return
        if line.startswith("READY|"):
            ready[line.partition("|")[2]].set()
        elif line.startswith("COMMAND|forest|"):
            observed_commands.append(line.partition("|forest|")[2])
            command_seen.set()

    test_logger = Mock()
    test_logger.info.side_effect = observe_log
    monkeypatch.setattr(service, "logger", test_logger)
    monkeypatch.chdir(tmp_path)
    shutdown = asyncio.Event()
    running = asyncio.create_task(
        run(
            install_path=Path("install"),
            cluster_path=Path("cluster"),
            update_mods=False,
            telemetry=TelemetrySettings(profile="history"),
            shutdown=shutdown,
        )
    )

    async with asyncio.timeout(10):
        await asyncio.gather(*(event.wait() for event in ready.values()))
        await write_fifo_when_ready(cluster / "console", b'print("hello")\n')
        await command_seen.wait()
        shutdown.set()
        assert await running == 0

    configure.assert_called_once()
    assert (cluster / "console").is_fifo()
    assert (cluster / "cave" / "console").is_fifo()
    assert (install / "mods").resolve() == (cluster / "mods").resolve()
    assert any(lua_string('print("hello")') in value for value in observed_commands)
