import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from ulid import ULID

from dst_server.errors import ErrorCode, ErrorInfo, RemoteError
from dst_server.models.cluster import ClusterStatus
from dst_server.rpc import ClusterClient
from dst_server.runtime import Server, ServerConfig
from tests.helpers import wait_for_event
from tests.system import helpers as system_helpers
from tests.system.helpers import wait_for_client


@pytest.mark.parametrize(
    "outcome", ["event", "error", "event-error", "early-return", "timeout"]
)
async def test_event_wait_observes_workers_and_reclaims_its_waiter(
    outcome: str,
) -> None:
    existing = asyncio.all_tasks()
    event = asyncio.Event()

    async def worker() -> None:
        if outcome == "event":
            event.set()
        elif outcome in {"error", "event-error"}:
            if outcome == "event-error":
                event.set()
            message = "worker failed"
            raise ValueError(message)
        elif outcome == "timeout":
            await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    try:
        if outcome == "event":
            await wait_for_event(event, task)
        else:
            error = {
                "error": ValueError,
                "event-error": ValueError,
                "early-return": AssertionError,
                "timeout": TimeoutError,
            }[outcome]
            with pytest.raises(error):
                await wait_for_event(event, task, timeout=0.01)
        assert asyncio.all_tasks() <= existing | {task}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.UNAVAILABLE,
        ErrorCode.INVALID_STATE,
        ErrorCode.INTERNAL,
        ErrorCode.TIMEOUT,
    ],
)
async def test_client_wait_retries_only_temporary_remote_state(
    code: ErrorCode, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = RemoteError(ErrorInfo(code, ULID(), "old controller is closing"))
    previous = Mock(spec=ClusterClient, status=AsyncMock(side_effect=error))
    status = Mock(spec=ClusterStatus, phase="running")
    replacement = Mock(spec=ClusterClient, status=AsyncMock(return_value=status))
    connect = AsyncMock(side_effect=[previous, replacement])
    monkeypatch.setattr(ClusterClient, "connect", connect)

    if code is ErrorCode.UNAVAILABLE:
        assert await wait_for_client(
            Path("unused.sock"), lambda value: value.phase == "running"
        ) == (replacement, status)
        assert connect.await_count == 2
        replacement.close.assert_not_called()
    else:
        with pytest.raises(RemoteError) as caught:
            await wait_for_client(Path("unused.sock"), lambda _: True)
        assert caught.value is error
        connect.assert_awaited_once()
    previous.close.assert_called_once_with()


async def test_client_wait_retries_local_handshake_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = Mock(spec=ClusterStatus, phase="running")
    client = Mock(spec=ClusterClient, status=AsyncMock(return_value=status))
    connect = AsyncMock(side_effect=[TimeoutError, client])
    monkeypatch.setattr(ClusterClient, "connect", connect)

    assert await wait_for_client(
        Path("unused.sock"), lambda value: value.phase == "running"
    ) == (client, status)
    assert connect.await_count == 2
    client.close.assert_not_called()


async def test_client_wait_deadline_interrupts_an_unresponsive_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def connect(_path: Path) -> ClusterClient:
        await asyncio.Event().wait()
        pytest.fail("unresponsive handshake unexpectedly returned")

    monkeypatch.setattr(ClusterClient, "connect", connect)
    monkeypatch.setattr(system_helpers, "STARTUP_TIMEOUT", 0.01)

    async with asyncio.timeout(1):
        with pytest.raises(TimeoutError):
            await wait_for_client(Path("unused.sock"), lambda _: True)


async def test_closed_server_still_reaps_its_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = Server(ServerConfig(shard="forest", executable=tmp_path / "unused"))
    await server.finish()
    assert server.closed
    remove = AsyncMock()
    monkeypatch.setattr(system_helpers, "remove_container", remove)

    await system_helpers.reap_server(server, "closed-container")

    remove.assert_awaited_once_with("closed-container")


async def test_cancelled_cluster_start_removes_pod_before_closing_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    starting = asyncio.Event()
    pod_removed = asyncio.Event()
    order: list[str] = []

    async def wait_idle() -> None:
        starting.set()
        await asyncio.Event().wait()

    async def close_controller() -> None:
        order.append("close")
        await pod_removed.wait()

    def run_command(*command: str) -> tuple[int, str]:
        if command[:3] == ("podman", "pod", "rm"):
            order.append("remove")
            pod_removed.set()
        return 0, ""

    controller = Mock(
        register=AsyncMock(),
        wait_idle=AsyncMock(side_effect=wait_idle),
        aclose=AsyncMock(side_effect=close_controller),
    )
    agents = [Mock(activate=AsyncMock(), aclose=AsyncMock()) for _ in range(2)]
    monkeypatch.setattr(
        system_helpers, "run_command", AsyncMock(side_effect=run_command)
    )
    monkeypatch.setattr(
        system_helpers, "ClusterController", Mock(return_value=controller)
    )
    monkeypatch.setattr(system_helpers, "ShardAgent", Mock(side_effect=agents))

    async def start() -> None:
        async with system_helpers.running_sharded_cluster(tmp_path):
            pytest.fail("cancelled startup unexpectedly completed")

    task = asyncio.create_task(start())
    try:
        await wait_for_event(starting, task, timeout=1)
        task.cancel()
        done, _ = await asyncio.wait((task,), timeout=1)
        assert task in done, "SDK cleanup is blocked on the remaining Pod"
        with pytest.raises(asyncio.CancelledError):
            await task
        assert order[:2] == ["remove", "close"]
        controller.aclose.assert_awaited_once_with()
        for agent in agents:
            agent.aclose.assert_awaited_once_with()
    finally:
        pod_removed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
