import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import logbook
import pytest
from ulid import ULID

from dst_server.cluster import agent as agent_module
from dst_server.cluster.agent import ShardAgent
from dst_server.cluster.layout import Shard
from dst_server.cluster.supervisor import (
    ShardDesired,
    ShardPhase,
    ShardSupervisor,
    ShardSupervisorStatus,
)
from dst_server.events.server import Event, SavedEvent, SessionEvent
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.lifecycle import ObservedLifecycleEvent
from tests.helpers import FAKE_SERVER


def supervisor_status(
    phase: ShardPhase,
    server: SimpleNamespace | Server | None = None,
) -> ShardSupervisorStatus:
    return ShardSupervisorStatus(
        shard="Master",
        desired=ShardDesired.RUNNING,
        phase=phase,
        attempt_id=(server.game_events.nonce if server is not None else None),
        attempts=1,
        returncode=(server.returncode if server is not None else None),
    )


def attach(
    agent: ShardAgent,
    server: SimpleNamespace | Server | None,
    phase: ShardPhase = ShardPhase.RUNNING,
) -> SimpleNamespace:
    status = supervisor_status(phase, server)
    supervisor = SimpleNamespace(
        server=server,
        status=status,
        start=AsyncMock(return_value=status),
        restart=AsyncMock(return_value=status),
        aclose=AsyncMock(),
    )
    agent.supervisor = cast("ShardSupervisor", supervisor)
    return supervisor


async def relay_lifecycle(
    agent: ShardAgent,
    server: SimpleNamespace,
    *events: Event,
) -> None:
    server.read_lifecycle_event = AsyncMock(
        side_effect=(
            *(
                ObservedLifecycleEvent(event, index)
                for index, event in enumerate(events, 1)
            ),
            None,
        )
    )
    await agent._drain_lifecycle(cast("Server", server))


async def raise_error(error: Exception) -> None:
    await asyncio.sleep(0)
    raise error


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ShardAgent:
    config = ServerConfig(shard="Master", executable=tmp_path / "unused")
    monkeypatch.setattr(
        agent_module.service,
        "create_server_config",
        Mock(return_value=config),
    )
    return ShardAgent(
        Shard("Master", True, tmp_path / "console"),
        install_path=tmp_path,
        cluster_path=tmp_path,
    )


@pytest.fixture
def running_server(agent: ShardAgent) -> SimpleNamespace:
    return SimpleNamespace(
        config=agent.config,
        game_events=SimpleNamespace(nonce=str(ULID())),
        returncode=None,
        driver=SimpleNamespace(wait_ready=AsyncMock()),
        recorder=SimpleNamespace(attributes=Mock(return_value={})),
    )


async def test_child_stdout_and_stderr_share_the_agent_log_output(
    agent: ShardAgent,
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-server"
    executable.write_text(
        FAKE_SERVER.replace(
            "busy = True",
            'os.write(1, b"merged-source\\n")\n'
            'os.write(2, b"merged-source\\n")\n'
            "busy = True",
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    agent.config = replace(agent.config, executable=executable)
    subscription = agent.logs.subscribe()
    with logbook.TestHandler() as output:
        server = agent._new_server()
        try:
            async with asyncio.timeout(3):
                await server.start(startup_timeout=3)
                records = [(await subscription.next(1))[0] for _ in range(2)]
            assert [record.line for record in records] == ["merged-source"] * 2
            assert [record.sequence for record in records] == [1, 2]
            assert server.process.stderr is None
        finally:
            if server.child is not None and not server.closed:
                await server.kill()
            await agent._stopped(server)
            subscription.close()
    assert [
        record.message
        for record in output.records
        if record.channel == agent_module.logger.name
        and record.message == "Master: merged-source"
    ] == ["Master: merged-source"] * 2


@pytest.mark.parametrize(
    ("phase", "rpc_phase"),
    [
        (ShardPhase.UNAVAILABLE, "unavailable"),
        (ShardPhase.STOPPED, "stopped"),
        (ShardPhase.STARTING, "starting"),
        (ShardPhase.RUNNING, "running"),
        (ShardPhase.STOPPING, "stopping"),
        (ShardPhase.RETRY_WAIT, "retryWait"),
        (ShardPhase.FAILED, "failed"),
    ],
)
async def test_runtime_status_maps_every_phase(
    agent: ShardAgent,
    phase: ShardPhase,
    rpc_phase: str,
) -> None:
    attach(agent, None, phase)

    status = await agent.runtime_status()

    assert status.phase == rpc_phase


async def test_activate_is_idempotent_and_guards_start_and_restart(
    agent: ShardAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = attach(agent, None, ShardPhase.STOPPED)
    activate = Mock()
    pipeline = SimpleNamespace(logger=object(), shutdown=AsyncMock())
    configure = Mock(return_value=pipeline)
    monkeypatch.setattr(agent_module.service, "activate_shard", activate)
    monkeypatch.setattr(agent_module.service, "configure_otel", configure)

    for action in (agent.start, agent.restart):
        with pytest.raises(RuntimeError, match="not prepared"):
            await action()

    await agent.activate()
    await agent.activate()
    await agent.start()
    await agent.restart()

    assert activate.call_count == 2
    activate.assert_called_with(agent.install_path, agent.cluster_path, agent.shard)
    configure.assert_called_once_with(agent.config, instance_id=agent.incarnation)
    supervisor.start.assert_awaited_once()
    supervisor.restart.assert_awaited_once()


async def test_markers_filter_the_current_attempt_and_publish_lifecycle(
    agent: ShardAgent,
    running_server: SimpleNamespace,
) -> None:
    attach(agent, running_server)
    lifecycle = agent.lifecycle.subscribe()
    generation_marker = await agent.generation_marker()
    save_marker = await agent.save_marker()
    saved = SavedEvent(path="session/9", snapshot=9)

    await relay_lifecycle(
        agent,
        running_server,
        SessionEvent(session_id="SESSION"),
        saved,
    )

    assert await agent.wait_generation(generation_marker, 1) == 1
    assert await agent.wait_saved(save_marker, 9, 1) == saved
    records = await lifecycle.next(2)
    assert [record.event for record in records] == [
        SessionEvent(session_id="SESSION"),
        saved,
    ]
    assert [record.observed_timestamp_ns for record in records] == [1, 2]
    running_server.driver.wait_ready.assert_awaited_once()


async def test_unknown_markers_and_snapshot_mismatch_are_rejected(
    agent: ShardAgent,
    running_server: SimpleNamespace,
) -> None:
    attach(agent, running_server)

    with pytest.raises(ValueError, match="unknown save marker"):
        await agent.wait_saved(-1, None, 1)
    with pytest.raises(ValueError, match="unknown generation marker"):
        await agent.wait_generation(-1, 1)

    marker = await agent.save_marker()
    saved = SavedEvent(path="session/8", snapshot=8)
    await relay_lifecycle(agent, running_server, saved)

    with pytest.raises(TimeoutError):
        await agent.wait_saved(marker, 9, 0.01)
    assert await agent.wait_saved(marker, None, 1) == saved


async def test_marker_history_evicts_entries_older_than_64(
    agent: ShardAgent,
    running_server: SimpleNamespace,
) -> None:
    attach(agent, running_server)
    oldest_save = oldest_generation = 0

    for sequence in range(65):
        save_marker = await agent.save_marker()
        generation_marker = await agent.generation_marker()
        if sequence == 0:
            oldest_save = save_marker
            oldest_generation = generation_marker
        await relay_lifecycle(
            agent,
            running_server,
            SessionEvent(session_id=f"SESSION-{sequence}"),
        )

    with pytest.raises(ValueError, match="unknown save marker"):
        await agent.wait_saved(oldest_save, None, 1)
    with pytest.raises(ValueError, match="unknown generation marker"):
        await agent.wait_generation(oldest_generation, 1)


@pytest.mark.parametrize("kind", ["save", "generation"])
async def test_wait_marker_fails_when_its_attempt_exits(
    agent: ShardAgent,
    running_server: SimpleNamespace,
    kind: str,
) -> None:
    attach(agent, running_server)
    if kind == "save":
        marker = await agent.save_marker()
    else:
        marker = await agent.generation_marker()
    running_server.returncode = 0
    waiter = (
        agent.wait_saved(marker, None, 1)
        if kind == "save"
        else agent.wait_generation(marker, 1)
    )

    with pytest.raises(RuntimeError, match="attempt changed"):
        await waiter


async def test_generation_timeout_includes_driver_readiness(
    agent: ShardAgent,
    running_server: SimpleNamespace,
) -> None:
    attach(agent, running_server)
    marker = await agent.generation_marker()
    await relay_lifecycle(
        agent,
        running_server,
        SessionEvent(session_id="SESSION"),
    )
    running_server.driver.wait_ready = AsyncMock(side_effect=asyncio.Event().wait)

    with pytest.raises(TimeoutError):
        await agent.wait_generation(marker, 0.01)

    running_server.driver.wait_ready.assert_awaited_once()


async def test_failure_is_queued_and_public_status_is_sanitized(
    agent: ShardAgent,
) -> None:
    failed = supervisor_status(ShardPhase.FAILED)
    attach(agent, None, ShardPhase.FAILED)

    await agent._failed(failed)

    status = await agent.runtime_status()
    assert status.error == "DST shard failed"
    assert status.error_id is not None
    assert await agent.next_failure() == failed


@pytest.mark.parametrize(
    ("phase", "critical", "fatal"),
    [
        (ShardPhase.RUNNING, True, True),
        (ShardPhase.RUNNING, False, False),
        (ShardPhase.STOPPING, True, True),
    ],
)
async def test_background_failure_boundary(
    agent: ShardAgent,
    running_server: SimpleNamespace,
    phase: ShardPhase,
    critical: bool,
    fatal: bool,
) -> None:
    attach(agent, running_server, phase)
    sensitive_message = "must-not-appear-in-public-error"
    task = asyncio.create_task(
        raise_error(RuntimeError(sensitive_message)),
        name="relay",
    )
    await asyncio.gather(task, return_exceptions=True)

    agent._background_done(
        cast("Server", running_server),
        task,
        critical=critical,
    )

    if fatal:
        with pytest.raises(RuntimeError, match="shard background task failed") as error:
            await agent.wait_fatal()
        assert sensitive_message not in str(error.value)
    else:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                await agent.wait_fatal()


async def test_close_failure_still_flushes_and_retry_is_idempotent(
    agent: ShardAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = attach(agent, None, ShardPhase.STOPPED)
    supervisor.aclose.side_effect = [RuntimeError("still alive"), None]
    pipeline = SimpleNamespace(logger=object(), shutdown=AsyncMock())
    monkeypatch.setattr(agent_module.service, "activate_shard", Mock())
    monkeypatch.setattr(
        agent_module.service, "configure_otel", Mock(return_value=pipeline)
    )
    await agent.activate()
    subscription = agent.logs.subscribe()

    with pytest.raises(RuntimeError, match="still alive"):
        await agent.aclose()

    assert await subscription.next(1) == ()
    pipeline.shutdown.assert_awaited_once()
    await agent.aclose()
    await agent.aclose()
    assert supervisor.aclose.await_count == 2
