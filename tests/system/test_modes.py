import os
import shutil
from pathlib import Path

import pytest
from pydantic import SecretStr

from dst_server import commands as c
from dst_server.presets.lst import fleet_room
from tests.system.helpers import OPERATION_TIMEOUT, STARTUP_TIMEOUT, managed_server

pytestmark = pytest.mark.system


@pytest.mark.parametrize(
    ("number", "capability"), [(206, "gorge_voter"), (207, "lobbyvote")]
)
async def test_deployed_mode_capabilities_in_isolated_world(
    tmp_path: Path, number: int, capability: str
) -> None:
    cache = os.environ.get("DST_TEST_MOD_CACHE")
    if cache is None:
        pytest.skip("DST_TEST_MOD_CACHE must point to existing room Mod caches")
    source = Path(cache) / f"{number:03d}" / "mods"
    assert source.is_dir()
    cluster = fleet_room(number, token=SecretStr("")).cluster
    cluster = cluster.replace(
        settings=cluster.settings.replace(
            offline_cluster=True,
            lan_only_cluster=True,
            internet_broadcasting_enabled=False,
            pause_when_empty=True,
        )
    )
    directory = tmp_path / "cluster"
    cluster = cluster.replace(shards={"forest": next(iter(cluster.shards.values()))})
    cluster.save(directory)
    shutil.copytree(source, directory / "mods", dirs_exist_ok=True)
    async with managed_server(tmp_path, directory) as server:
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        health = await server.game.invoke(c.Health())
        assert health.capabilities[capability] == "active"
        assert health.telemetry_status == "active"
        presence = await server.game.invoke(c.Presence())
        assert presence.reliable
        assert presence.client_count == presence.player_count == 0
        await server.stop(grace_period=OPERATION_TIMEOUT)
        assert server.returncode == 0
