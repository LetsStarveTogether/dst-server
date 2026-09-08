import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from weakref import ref

import logbook
import pytest
from ulid import ULID

from dst_server import commands as c
from dst_server.cluster import agent as agent_module
from dst_server.cluster.agent import ShardAgent
from dst_server.configuration.files import Shard
from dst_server.errors import IndeterminateError
from dst_server.events import ObservedGameEvent
from dst_server.events.server import Event, SavedEvent, SessionEvent, UnknownEvent
from dst_server.events.world import CycleState, StateChangedEvent
from dst_server.models.cluster import ObservationCursor
from dst_server.models.snapshot import Snapshot, SnapshotCatalog, WorldSnapshotMetadata
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.lifecycle import ObservedLifecycleEvent
from dst_server.runtime.operational import OperationalRecord
from dst_server.runtime.supervisor import (
    ShardDesired,
    ShardPhase,
    ShardSupervisor,
    ShardSupervisorStatus,
)
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


@pytest.fixture
def snapshot_catalog(
    agent: ShardAgent, running_server: SimpleNamespace
) -> SnapshotCatalog:
    world_file = "session/SESSION/0000000031"
    path = agent.cluster_path / agent.name / "save" / world_file
    path.parent.mkdir(parents=True)
    path.write_bytes(b"world snapshot")
    path.with_suffix(".meta").write_bytes(
        b'return {clock={cycles=20,phase="night"},'
        b'seasons={season="winter",remainingdaysinseason=15}}\0'
    )
    catalog = SnapshotCatalog(
        session_id="SESSION",
        snapshots=(Snapshot(snapshot_id=31, world_file=world_file),),
        has_more=True,
    )
    running_server.session_id = catalog.session_id
    running_server.game = SimpleNamespace(invoke=AsyncMock(return_value=catalog))
    attach(agent, running_server)
    return catalog


async def test_list_snapshots_loads_native_metadata(
    agent: ShardAgent,
    running_server: SimpleNamespace,
    snapshot_catalog: SnapshotCatalog,
) -> None:
    catalog = await agent.list_snapshots(3, before=50)

    running_server.game.invoke.assert_awaited_once_with(c.Snapshots(limit=3, before=50))
    assert catalog.session_id == snapshot_catalog.session_id
    assert catalog.has_more
    snapshot = catalog.snapshots[0]
    assert snapshot.snapshot_id == 31
    assert snapshot.world_file == snapshot_catalog.snapshots[0].world_file
    assert snapshot.metadata is not None
    assert snapshot.metadata.day == 21
    assert snapshot.metadata.clock.phase == "night"
    assert snapshot.metadata.seasons.season == "winter"
    assert snapshot.metadata.seasons.remainingdaysinseason == 15
    assert snapshot_catalog.snapshots[0].metadata is None


@pytest.mark.parametrize("missing", ["world", "metadata", "world_file"])
async def test_list_snapshots_missing_files_have_no_metadata(
    agent: ShardAgent,
    running_server: SimpleNamespace,
    snapshot_catalog: SnapshotCatalog,
    missing: str,
) -> None:
    snapshot = snapshot_catalog.snapshots[0].replace(metadata=WorldSnapshotMetadata())
    path = agent.cluster_path / agent.name / "save/session/SESSION/0000000031"
    if missing == "world_file":
        snapshot = snapshot.replace(world_file=None)
    else:
        (path if missing == "world" else path.with_suffix(".meta")).unlink()
    running_server.game.invoke.return_value = snapshot_catalog.replace(
        snapshots=(snapshot,)
    )

    catalog = await agent.list_snapshots()

    assert catalog.snapshots[0].metadata is None


@pytest.mark.parametrize(
    ("session_id", "world_file"),
    [
        ("SESSION", "/session/SESSION/0000000031"),
        ("SESSION", "session/SESSION/../0000000031"),
        ("..", "session/../0000000031"),
        ("SESSION", "session/OTHER/0000000031"),
        ("SESSION", "session/SESSION/0000000032"),
    ],
)
async def test_list_snapshots_rejects_paths_outside_native_snapshot(
    agent: ShardAgent,
    running_server: SimpleNamespace,
    snapshot_catalog: SnapshotCatalog,
    session_id: str,
    world_file: str,
) -> None:
    running_server.game.invoke.return_value = snapshot_catalog.replace(
        session_id=session_id,
        snapshots=(Snapshot(snapshot_id=31, world_file=world_file),),
    )

    with pytest.raises(ValueError, match="native snapshot path"):
        await agent.list_snapshots()


@pytest.mark.parametrize(
    "component",
    ["shard", "save", "session_root", "session", "world", "metadata", "dangling"],
)
async def test_list_snapshots_rejects_symlinks(
    agent: ShardAgent,
    snapshot_catalog: SnapshotCatalog,
    component: str,
) -> None:
    source = agent.cluster_path / agent.name / "save/session/SESSION/0000000031"
    path = {
        "shard": agent.cluster_path / agent.name,
        "save": source.parent.parent.parent,
        "session_root": source.parent.parent,
        "session": source.parent,
        "world": source,
        "metadata": source.with_suffix(".meta"),
        "dangling": source.with_suffix(".meta"),
    }[component]
    target = path.with_name(path.name + ".real")
    if component == "dangling":
        path.unlink()
    else:
        path.rename(target)
    path.symlink_to(target)
    assert snapshot_catalog.snapshots[0].metadata is None

    with pytest.raises(ValueError, match="symlink"):
        await agent.list_snapshots()


@pytest.mark.parametrize("changed", ["session", "attempt"])
async def test_list_snapshots_rejects_world_changes_during_metadata_read(
    agent: ShardAgent,
    running_server: SimpleNamespace,
    snapshot_catalog: SnapshotCatalog,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    read = agent._read_snapshot_metadata

    def change_world(catalog: SnapshotCatalog) -> SnapshotCatalog:
        result = read(catalog)
        if changed == "session":
            running_server.session_id = "NEW-SESSION"
        else:
            attach(agent, SimpleNamespace(**vars(running_server)))
        return result

    monkeypatch.setattr(agent, "_read_snapshot_metadata", change_world)
    assert snapshot_catalog.session_id == running_server.session_id

    with pytest.raises(RuntimeError, match="world session changed"):
        await agent.list_snapshots()


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
    agent.config = agent.config.replace(executable=executable)
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


@pytest.mark.parametrize("phase", ShardPhase)
async def test_runtime_status_uses_supervisor_phase(
    agent: ShardAgent,
    phase: ShardPhase,
) -> None:
    attach(agent, None, phase)
    assert (await agent.runtime_status()).phase is phase


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

    future = ObservationCursor(
        attempt=ULID.from_str(running_server.game_events.nonce), sequence=1
    )
    with pytest.raises(ValueError, match="future save cursor"):
        await agent.wait_saved(future, None, 1)
    with pytest.raises(ValueError, match="future generation cursor"):
        await agent.wait_generation(future, 1)

    marker = await agent.save_marker()
    saved = SavedEvent(path="session/8", snapshot=8)
    await relay_lifecycle(agent, running_server, saved)

    with pytest.raises(TimeoutError):
        await agent.wait_saved(marker, 9, 0.01)
    assert await agent.wait_saved(marker, None, 1) == saved


async def test_cursors_do_not_require_a_marker_lookup_history(
    agent: ShardAgent,
    running_server: SimpleNamespace,
) -> None:
    attach(agent, running_server)
    oldest_save = await agent.save_marker()
    oldest_generation = await agent.generation_marker()
    assert oldest_save.attempt == ULID.from_str(running_server.game_events.nonce)
    assert oldest_save.sequence == 0

    for sequence in range(65):
        await relay_lifecycle(
            agent, running_server, SessionEvent(session_id=f"SESSION-{sequence}")
        )
    saved = SavedEvent(path="session/9", snapshot=9)
    await relay_lifecycle(agent, running_server, saved)
    assert await agent.wait_saved(oldest_save, None, 1) == saved
    assert await agent.wait_generation(oldest_generation, 1) == 65


@pytest.mark.parametrize("count", [63, 64, 65])
async def test_save_cursor_detects_lost_confirmations(
    agent: ShardAgent,
    running_server: SimpleNamespace,
    count: int,
) -> None:
    attach(agent, running_server)
    cursor = await agent.save_marker()
    await relay_lifecycle(
        agent,
        running_server,
        *(
            SavedEvent(path=f"session/{number}", snapshot=number)
            for number in range(1, count + 1)
        ),
    )
    if count > 64:
        with pytest.raises(IndeterminateError):
            await agent.wait_saved(cursor, None, 1)
    else:
        assert (await agent.wait_saved(cursor, None, 1)).snapshot == 1


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


async def test_unread_failures_coalesce_to_the_latest_status(agent: ShardAgent) -> None:
    failed = supervisor_status(ShardPhase.FAILED)
    for attempt in range(1000):
        latest = replace(failed, attempts=attempt)
        await agent._failed(latest)
    assert agent.failures.qsize() == 1
    assert await agent.next_failure() == latest


@pytest.mark.parametrize("kind", ["lifecycle", "game", "operational"])
async def test_relays_release_consumed_records_while_idle(
    agent: ShardAgent,
    running_server: SimpleNamespace,
    kind: str,
) -> None:
    class Body(dict[str, Any]):
        pass

    idle, stop = asyncio.Event(), asyncio.Event()
    if kind == "lifecycle":
        value = UnknownEvent(line="x" * 65536)
        pending = [ObservedLifecycleEvent(value, 1)]
    elif kind == "game":
        value = StateChangedEvent(
            v=2,
            nonce=running_server.game_events.nonce,
            generation=1,
            session_id=None,
            seq=1,
            event="dst.world.state_changed",
            tick=1,
            monotonic_ms=1,
            cycle=None,
            data=CycleState(name="cycles", value=1),
        )
        pending = [ObservedGameEvent(value, 1)]
    else:
        value = Body(message="x" * 65536)
        pending = [OperationalRecord("uid", "test", value, 1, "INFO")]
    released = ref(value)
    del value

    async def read() -> Any:
        if pending:
            return pending.pop()
        idle.set()
        await stop.wait()
        return None

    setattr(running_server, f"read_{kind}_event", read)
    relay = {
        "lifecycle": agent._drain_lifecycle,
        "game": agent._drain_game_events,
        "operational": agent._drain_operational,
    }[kind]
    task = asyncio.create_task(relay(cast("Server", running_server)))
    try:
        async with asyncio.timeout(1):
            await idle.wait()
        assert released() is None
    finally:
        stop.set()
        await task


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
