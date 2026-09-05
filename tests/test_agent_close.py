import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from dst_server.cluster import agent as agent_module
from dst_server.cluster.agent import ShardAgent
from dst_server.cluster.layout import Shard
from dst_server.runtime import Server, ServerConfig
from dst_server.telemetry.otel import Pipeline


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ShardAgent:
    monkeypatch.setattr(
        agent_module.service,
        "create_server_config",
        Mock(return_value=ServerConfig(shard="forest")),
    )
    return ShardAgent(
        Shard("forest", True, tmp_path / "console"), cluster_path=tmp_path
    )


async def test_cancelled_close_keeps_tail_relays_until_shared_cleanup_finishes(
    agent: ShardAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    completed: list[str] = []
    server = Server(agent.config)

    async def persist_tail() -> None:
        await release.wait()
        completed.append("persisted")

    async def stop() -> None:
        entered.set()
        await release.wait()
        await agent._stopped(server)
        completed.append("stopped")

    stop_mock = AsyncMock(side_effect=stop)
    monkeypatch.setattr(agent.supervisor, "_close", stop_mock)
    pipeline = Mock(shutdown=AsyncMock())
    agent._pipeline = cast("Pipeline", pipeline)
    tail = asyncio.create_task(persist_tail())
    agent._attempt_tasks = (tail,)
    first = asyncio.create_task(agent.aclose())
    second: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(1):
            await entered.wait()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        second = asyncio.create_task(agent.aclose())
        await asyncio.sleep(0)

        assert not tail.done()
        assert not second.done()
        pipeline.shutdown.assert_not_awaited()
        release.set()
        async with asyncio.timeout(1):
            await second
        await agent.aclose()

        assert completed == ["persisted", "stopped"]
        stop_mock.assert_awaited_once()
        pipeline.shutdown.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(
            first,
            tail,
            *(task for task in (second,) if task is not None),
            return_exceptions=True,
        )
        if agent.supervisor._close_task is not None:
            await asyncio.gather(agent.supervisor._close_task, return_exceptions=True)


@pytest.mark.parametrize("supervisor_fails", [False, True])
@pytest.mark.parametrize("pipeline_fails", [False, True])
async def test_close_keeps_detached_tail_failure_and_cleanup_errors(
    agent: ShardAgent,
    monkeypatch: pytest.MonkeyPatch,
    supervisor_fails: bool,
    pipeline_fails: bool,
) -> None:
    release = asyncio.Event()
    server = Server(agent.config)
    agent.supervisor._server = server

    async def persist_tail() -> None:
        await release.wait()
        message = "private outbox write failed"
        raise OSError(message)

    tail = asyncio.create_task(persist_tail())
    agent._attempt_tasks = (tail,)
    supervisor_error = RuntimeError("supervisor cleanup failed")
    pipeline_error = OSError("pipeline cleanup failed")

    async def stop() -> None:
        release.set()
        await asyncio.gather(tail, return_exceptions=True)
        agent.supervisor._server = None
        agent._background_done(server, tail, critical=True)
        if supervisor_fails:
            raise supervisor_error

    monkeypatch.setattr(agent.supervisor, "aclose", AsyncMock(side_effect=stop))
    pipeline = Mock(
        shutdown=AsyncMock(side_effect=pipeline_error if pipeline_fails else None)
    )
    agent._pipeline = cast("Pipeline", pipeline)

    with pytest.raises((RuntimeError, ExceptionGroup)) as failure:
        await agent.aclose()

    errors = (
        failure.value.exceptions
        if isinstance(failure.value, ExceptionGroup)
        else (failure.value,)
    )
    assert any("shard background task failed" in str(error) for error in errors)
    assert "private outbox" not in str(failure.value)
    if supervisor_fails:
        assert supervisor_error in errors
    if pipeline_fails:
        assert pipeline_error in errors
    pipeline.shutdown.assert_awaited_once()
    assert agent._pipeline is None
