from collections.abc import AsyncIterator
from configparser import ConfigParser
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from dst_server.announcements import Template
from dst_server.host.maintenance import maintain_restart


async def test_restart_runs_inside_service_and_isolates_room_failures(
    tmp_path: Path,
) -> None:
    clients = {
        0: SimpleNamespace(restart=AsyncMock()),
        1: SimpleNamespace(restart=AsyncMock(side_effect=RuntimeError("busy"))),
    }

    @asynccontextmanager
    async def connect(number: int) -> AsyncIterator[SimpleNamespace]:
        yield clients[number]

    host = Mock(cluster_root=tmp_path, connect=connect)
    results = await maintain_restart(host, (0, 1, 0), timeout=900)
    assert results == {
        0: {"status": "restarted"},
        1: {"status": "failed", "error": "busy"},
    }
    for client in clients.values():
        client.restart.assert_awaited_once()
        arguments = client.restart.call_args.kwargs
        assert arguments["timeout"] == 900
        assert arguments["notice"].delay == 480
        assert arguments["notice"].interval == 60
    host.restart.assert_not_called()
    host.systemd.assert_not_called()
    assert not (tmp_path / ".dst-maintenance").exists()


async def test_restart_passes_custom_countdown_to_service() -> None:
    client = SimpleNamespace(restart=AsyncMock())

    @asynccontextmanager
    async def connect(_number: int) -> AsyncIterator[SimpleNamespace]:
        yield client

    await maintain_restart(
        Mock(connect=connect),
        (4,),
        delay=30,
        reason=Template.MOD_UPDATE,
        estimated_duration=120,
    )
    notice = client.restart.call_args.kwargs["notice"]
    assert notice.delay == 30
    assert "MOD" in notice.render(30)


async def test_restart_requires_explicit_rooms() -> None:
    with pytest.raises(ValueError, match="at least one room"):
        await maintain_restart(Mock(), ())


def test_recycling_runs_after_both_successful_and_failed_schedule() -> None:
    config = ConfigParser(interpolation=None)
    config.read(
        Path(__file__).parents[2]
        / "src/dst_server/host/systemd/dst-room-schedule.service"
    )
    assert config["Unit"]["OnSuccess"] == "dst-room-recycle.service"
    assert config["Unit"]["OnFailure"] == "dst-room-recycle.service"
