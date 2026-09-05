import asyncio
import importlib.util
import inspect
import json
import os
import socket
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from ulid import ULID

from dst_server.cluster import cli, daemon

HEAVY_MODULES = {
    "asyncio",
    "capnp",
    "logbook",
    "luaparser",
    "opentelemetry",
    "pydantic",
    "pydantic_core",
}


@pytest.fixture(autouse=True)
def clear_notification_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(params=["pathname", "abstract"])
def notification_socket(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[socket.socket]:
    address = (
        str(tmp_path / "n")
        if request.param == "pathname"
        else f"@dst-watchdog-{ULID()}"
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.setblocking(False)
        receiver.bind("\0" + address[1:] if address.startswith("@") else address)
        monkeypatch.setenv("NOTIFY_SOCKET", address)
        yield receiver


async def test_ready_precedes_runtime_and_watchdog_runs_on_the_same_event_loop(
    notification_socket: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    intervals: asyncio.Queue[float] = asyncio.Queue()
    ticks: asyncio.Queue[None] = asyncio.Queue()
    loops = []

    async def sleep(interval: float) -> None:
        loops.append(asyncio.get_running_loop())
        intervals.put_nowait(interval)
        await ticks.get()

    async def operation(_shutdown: asyncio.Event) -> None:
        assert notification_socket.recv(64) == b"READY=1"
        for _ in range(2):
            assert await intervals.get() == 60
            ticks.put_nowait(None)
            assert await loop.sock_recv(notification_socket, 64) == b"WATCHDOG=1"

    monkeypatch.setattr(daemon.asyncio, "sleep", sleep)
    async with asyncio.timeout(5):
        await daemon._run_with_watchdog(operation, asyncio.Event())
    assert loops
    assert all(value is loop for value in loops)
    with pytest.raises(BlockingIOError):
        notification_socket.recv(64)
    assert not any(task.get_name() == "dst-watchdog" for task in asyncio.all_tasks())


async def test_no_notify_socket_runs_directly_without_creating_a_notifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False
    notifier = Mock(side_effect=AssertionError("unexpected notification socket"))
    monkeypatch.setattr(daemon, "socket", notifier)

    async def operation(_: asyncio.Event) -> None:  # ruff: ignore[unused-async]
        nonlocal called
        called = True

    await daemon._run_with_watchdog(operation, asyncio.Event())
    assert called
    notifier.assert_not_called()


@pytest.mark.parametrize(
    "address",
    [
        "",
        "@",
        "/",
        "relative",
        "./socket",
        "../socket",
        "unix:/run/n",
        "tcp:123",
        "/run/\0n",
        "@n\0x",
    ],
)
async def test_invalid_notify_socket_is_rejected_before_runtime_or_socket_creation(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    monkeypatch.setattr(daemon.os, "environ", {"NOTIFY_SOCKET": address})
    notifier = Mock(side_effect=AssertionError("invalid address reached socket"))
    monkeypatch.setattr(daemon, "socket", notifier)

    async def operation(_: asyncio.Event) -> None:  # ruff: ignore[unused-async]
        pytest.fail("invalid address must not start runtime")

    with pytest.raises(ValueError, match="NOTIFY_SOCKET"):
        await daemon._run_with_watchdog(operation, asyncio.Event())
    notifier.assert_not_called()


@pytest.mark.parametrize("command", ["master", "serve"])
async def test_preexisting_shutdown_sends_no_ready_and_does_not_initialize(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    shutdown = asyncio.Event()
    shutdown.set()
    monkeypatch.setenv("NOTIFY_SOCKET", "invalid")

    async def operation(*_: object) -> None:  # ruff: ignore[unused-async]
        pytest.fail("pre-stopped daemon must not initialize")

    monkeypatch.setattr(daemon, "_run_agent", operation)
    monkeypatch.setattr(daemon, "_run_master", operation)
    options = {"shard": "cave"} if command == "serve" else {}
    assert await getattr(daemon, command)(shutdown=shutdown, **options) == 0


@pytest.mark.parametrize(
    "cause", ["open", "connect", "ready", "watchdog", "backpressure"]
)
async def test_notification_failures_stop_runtime_and_close_the_socket(
    notification_socket: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
) -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    created: list[socket.socket] = []
    loop = asyncio.get_running_loop()
    failure = (
        BlockingIOError("notification queue full")
        if cause == "backpressure"
        else OSError("notification failed")
    )

    class FailingSocket(socket.socket):
        def connect(self, address: Any) -> None:
            if cause == "connect":
                raise failure
            super().connect(address)

        def sendall(self, data: Any, flags: int = 0) -> None:
            if (cause == "ready" and data == b"READY=1") or (
                cause in {"watchdog", "backpressure"} and data == b"WATCHDOG=1"
            ):
                raise failure
            super().sendall(data, flags)

    def notifier(*arguments: Any, **options: Any) -> socket.socket:
        if cause == "open":
            raise failure
        value = FailingSocket(*arguments, **options)
        created.append(value)
        return value

    async def sleep(interval: float) -> None:
        assert interval == 60
        await started.wait()

    async def operation(*_: object) -> None:
        assert asyncio.get_running_loop() is loop
        assert notification_socket.recv(64) == b"READY=1"
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    monkeypatch.setattr(daemon, "socket", notifier)
    monkeypatch.setattr(daemon.asyncio, "sleep", sleep)
    monkeypatch.setattr(daemon, "_run_agent", operation)
    async with asyncio.timeout(5):
        assert await daemon.serve(shard="cave", shutdown=asyncio.Event()) == 1
    assert started.is_set() is (cause in {"watchdog", "backpressure"})
    assert cleaned.is_set() is started.is_set()
    assert len(created) == (0 if cause == "open" else 1)
    assert all(notifier.fileno() == -1 for notifier in created)
    assert not any(task.get_name() == "dst-watchdog" for task in asyncio.all_tasks())


@pytest.mark.parametrize("outcome", ["return", "error", "cancel"])
async def test_runtime_completion_cleans_up_watchdog_and_notifier(
    notification_socket: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    running = asyncio.Event()
    stopped = asyncio.Event()
    shutdown = asyncio.Event()
    created: list[socket.socket] = []
    failure = RuntimeError("runtime failed")

    def notifier(*arguments: Any, **options: Any) -> socket.socket:
        value = socket.socket(*arguments, **options)
        created.append(value)
        return value

    async def operation(*_: object) -> None:
        assert notification_socket.recv(64) == b"READY=1"
        assert created[0].getblocking() is False
        assert created[0].get_inheritable() is False
        running.set()
        try:
            await shutdown.wait()
            if outcome == "error":
                raise failure
        finally:
            await asyncio.sleep(0)
            stopped.set()

    monkeypatch.setattr(daemon, "socket", notifier)
    monkeypatch.setattr(daemon, "_run_agent", operation)
    task = asyncio.create_task(daemon.serve(shard="cave", shutdown=shutdown))
    try:
        async with asyncio.timeout(5):
            await running.wait()
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                shutdown.set()
                assert await task == (1 if outcome == "error" else 0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert stopped.is_set()
    assert created[0].fileno() == -1
    assert not any(task.get_name() == "dst-watchdog" for task in asyncio.all_tasks())


async def test_missing_notify_endpoint_fails_instead_of_running_unsupervised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "missing"))

    async def operation(*_: object) -> None:  # ruff: ignore[unused-async]
        pytest.fail("unreachable notification socket must prevent startup")

    with pytest.raises(FileNotFoundError):
        await daemon._run_with_watchdog(operation, asyncio.Event())


def test_old_file_heartbeat_api_is_removed() -> None:
    assert importlib.util.find_spec("dst_server.health") is None
    for method in (daemon.master, daemon.serve):
        parameters = inspect.signature(method).parameters
        assert "heartbeat_path" not in parameters
        assert "heartbeat_interval" not in parameters
    assert daemon.WATCHDOG_INTERVAL == 60


def test_removed_healthcheck_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(("healthcheck",))
    assert error.value.code == 2


@pytest.mark.parametrize("module", ["dst_server", "dst_server.cluster"])
def test_importing_a_package_does_not_initialize_its_subsystems(module: str) -> None:
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [
            sys.executable,
            "-c",
            (
                "import importlib, json, sys; importlib.import_module(sys.argv[1]); "
                "print(json.dumps(sorted(sys.modules)))"
            ),
            module,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    modules = json.loads(result.stdout)
    assert not HEAVY_MODULES.intersection(name.split(".")[0] for name in modules)
    assert [name for name in modules if name.startswith("dst_server.")] == (
        ["dst_server.cluster"] if module.endswith(".cluster") else []
    )


def test_installed_help_runs_without_third_party_packages() -> None:
    entrypoint = Path(sys.executable).with_name("dst-server")
    source = Path(__file__).parents[1] / "src"
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-S", str(entrypoint), "--help"],
        env={**os.environ, "PYTHONPATH": str(source)},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert all(command in result.stdout for command in ("prepare", "master", "serve"))
    assert "healthcheck" not in result.stdout


@pytest.mark.parametrize(
    "arguments", [("--help",), ("master", "--help"), ("serve", "--help")]
)
def test_installed_help_does_not_import_runtime_dependencies(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    entrypoint = Path(sys.executable).with_name("dst-server")
    report = tmp_path / "modules.json"
    program = """
import atexit
import json
import runpy
import sys
from pathlib import Path
entrypoint, report, *arguments = sys.argv[1:]
atexit.register(lambda: Path(report).write_text(json.dumps(sorted(sys.modules))))
sys.argv = [entrypoint, *arguments]
runpy.run_path(entrypoint, run_name="__main__")
"""
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", program, str(entrypoint), str(report), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stderr == ""
    modules = json.loads(report.read_text())
    assert "dst_server.health" not in modules
    assert not HEAVY_MODULES.intersection(name.split(".")[0] for name in modules)
