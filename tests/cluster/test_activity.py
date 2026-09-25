from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dst_server.activity import ActivityCheckpoint
from dst_server.models.cluster import ShardDesired, ShardPhase
from dst_server.rooms import Control, DailyWindow, read_control, write_control
from tests.cluster.helpers import managed_controller


@pytest.mark.parametrize("fault", [None, "forced", "gap", "world", "attempt", "manual"])
async def test_only_clean_final_observations_bridge_closed_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        controller, master, cave, _, _ = room
        now = datetime.now(UTC)
        checkpoint = ActivityCheckpoint(
            sessions={agent.name: agent.runtime.session_id for agent in (master, cave)},
            observations={
                agent.name: f"{agent.attempt}:1:0" for agent in (master, cave)
            },
            last_active_at=now - timedelta(days=3),
        )
        write_control(
            controller.cluster_path,
            Control(
                recycle=True,
                schedule=(DailyWindow(start=time(10), end=time(18)),),
                paused=fault == "manual",
                activity=checkpoint,
            ),
        )
        for agent in (master, cave):
            status = (await agent.runtime_status()).replace(
                desired=ShardDesired.STOPPED,
                phase=ShardPhase.STOPPED,
                pid=None,
                ready=False,
                returncode=0,
                activity_observation=f"{agent.attempt}:1",
                activity_reliable=True,
                # A connection after the last timer sample must renew activity.
                last_active_at=now if agent is cave else None,
            )
            if agent is cave:
                status = status.replace(
                    **{
                        "forced": {"returncode": -9},
                        "gap": {"activity_reliable": False},
                        "world": {"session_id": "OTHER"},
                        "attempt": {"activity_observation": "different:1"},
                    }.get(fault, {})
                )
            monkeypatch.setattr(agent, "runtime_status", AsyncMock(return_value=status))
        await controller._checkpoint_activity()
        stored = read_control(controller.cluster_path).activity
        assert stored is not None
        assert stored.clean_shutdown is (fault is None)
        if fault is not None:
            assert stored == checkpoint
            return
        assert stored.last_active_at == now
        bridge = await controller._consume_activity()
        assert bridge == stored
        assert await controller._consume_activity() is None
        consumed = read_control(controller.cluster_path).activity
        assert consumed is not None
        assert not consumed.clean_shutdown
        await controller._resume_activity(bridge)
        resumed = read_control(controller.cluster_path).activity
        assert resumed is not None
        assert resumed.last_active_at == now
        assert resumed.observations != checkpoint.observations
        assert not resumed.clean_shutdown


async def test_shutdown_bridge_is_consumed_before_start_can_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        controller = room[0]
        checkpoint = ActivityCheckpoint(
            sessions={"Master": "S"},
            last_active_at=datetime.now(UTC) - timedelta(days=3),
            clean_shutdown=True,
        )
        write_control(
            controller.cluster_path, Control(recycle=True, activity=checkpoint)
        )

        async def start() -> None:  # ruff: ignore[unused-async]
            stored = read_control(controller.cluster_path).activity
            assert stored is not None
            assert not stored.clean_shutdown
            msg = "failed during startup"
            raise RuntimeError(msg)

        monkeypatch.setattr(controller, "_start_desired", start)
        await controller._initialize()
        assert controller._fatal.is_set()
        assert await controller._consume_activity() is None
