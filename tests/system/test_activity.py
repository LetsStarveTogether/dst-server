import asyncio
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from dst_server import commands as c
from dst_server.activity import ActivityCheckpoint, observe
from dst_server.rooms import Control, DailyWindow, read_control, write_control
from tests.system.helpers import OPERATION_TIMEOUT, running_sharded_cluster

pytestmark = pytest.mark.system


async def test_native_shutdown_preserves_scheduled_room_idle_across_reopen(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "cluster"
    old_activity = datetime.now(UTC) - timedelta(days=2)
    async with running_sharded_cluster(tmp_path, pause_when_empty=True) as (
        controller,
        agents,
    ):
        presence = {
            name: await agent.invoke(c.Presence()) for name, agent in agents.items()
        }
        assert all(value.reliable for value in presence.values())
        assert all(
            not (value.client_count or value.player_count)
            for value in presence.values()
        )
        checkpoint = ActivityCheckpoint(
            sessions={name: value.session_id for name, value in presence.items()},
            observations={name: value.observation for name, value in presence.items()},
            last_active_at=old_activity,
        )
        write_control(
            directory,
            Control(
                recycle=True,
                schedule=(DailyWindow(start=time(10), end=time(18)),),
                activity=checkpoint,
            ),
        )
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while True:
                if all(
                    shard.activity_reliable
                    for shard in (await controller.status()).shards
                ):
                    break
                await asyncio.sleep(0.1)

    closed = read_control(directory).activity
    assert closed is not None
    assert closed.clean_shutdown
    assert closed.last_active_at == old_activity

    async with running_sharded_cluster(tmp_path, pause_when_empty=True) as (_, agents):
        resumed = read_control(directory).activity
        assert resumed is not None
        assert not resumed.clean_shutdown
        assert resumed.sessions == checkpoint.sessions
        assert resumed.observations != checkpoint.observations
        assert resumed.last_active_at == old_activity
        presence = {
            name: await agent.invoke(c.Presence()) for name, agent in agents.items()
        }
        assert (
            observe(resumed, presence, datetime.now(UTC)).last_active_at == old_activity
        )
