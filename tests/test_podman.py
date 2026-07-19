from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

import pytest

from dst_server import Server, ServerArgs, TelemetrySettings
from dst_server.events import (
    EntityDeathEvent,
    PlayerCombatReceivedEvent,
    PlayerConditionChangedEvent,
    PlayerShardEnteredEvent,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("DST_SERVER_PODMAN_TEST") != "1",
    reason="set DST_SERVER_PODMAN_TEST=1 to run the local image smoke test",
)

CONTAINER_NAME = "dst-server-sdk-test"
GAME_EXECUTABLE = "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"


async def test_existing_image_with_real_save(tmp_path: Path) -> None:
    game_data = Path(
        os.environ.get("DST_SERVER_GAME_DATA", "/srv/lst-bot/ref/game-data-example")
    )
    image = os.environ.get("DST_SERVER_IMAGE", "quay.io/wh2099/dst-server")
    host_lua = ServerArgs(shard="unused").lua_directory
    wrapper = tmp_path / "podman-dst-server"
    command = [
        "podman",
        "run",
        "--rm",
        "--name",
        CONTAINER_NAME,
        "--preserve-fds=3",
        "--network",
        "none",
        "--workdir",
        "/install/bin64",
        "--volume",
        f"{game_data}:/cluster:O",
        "--volume",
        f"{host_lua}:/dst-server-lua:ro",
        "--entrypoint",
        GAME_EXECUTABLE,
        image,
    ]
    wrapper.write_text(
        "#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    server = Server(
        ServerArgs(
            shard="forest",
            executable=wrapper,
            persistent_storage_root=Path("/"),
            conf_dir="/",
            cluster="cluster",
            ugc_directory=Path("/cluster/mods/ugc"),
            extra=("-skip_update_server_mods", "-offline"),
            lua_directory=Path("/dst-server-lua"),
            telemetry=TelemetrySettings(profile="history"),
            monitor_parent_process=False,
        )
    )
    started = False

    try:
        async with asyncio.timeout(120):
            await server.start()
            started = True
            assert server.driver_health.installed is True
            assert server.driver_health.action_hook is True
            assert server.driver_health.shard_hook is True
            assert (await server.game.world.room()).is_dedicated is True
            assert (await server.game.world.state()).cycles >= 0
            runtime = await server.game.world.runtime()
            assert runtime.session_id == server.session_id
            assert runtime.snapshot >= 0
            assert isinstance(await server.game.world.mods(), tuple)
            saved = await server.save(completion_timeout=30)
            assert saved.snapshot is None or saved.snapshot >= runtime.snapshot
            assert (
                await server.game.world.execute(
                    "local rabbit=SpawnPrefab('rabbit');"
                    "local player=SpawnPrefab('wilson');"
                    "player.userid='KU_DRIVER_TEST';"
                    "TheWorld:PushEvent('ms_playerjoined',player);"
                    "player:PushEvent('startstarving');"
                    "player.components.combat:GetAttacked(rabbit,1);"
                    "rabbit:AddTag('epic');"
                    "TheWorld:PushEvent('entity_death',{inst=rabbit,corpsing=false});"
                    "rabbit:Remove();player:Remove();return true"
                )
                is True
            )
            assert (await server.game.get_health()).events_emitted >= 4
            assert server.telemetry_invalid == 0
            expected = {
                EntityDeathEvent,
                PlayerCombatReceivedEvent,
                PlayerConditionChangedEvent,
                PlayerShardEnteredEvent,
            }
            while expected:
                observed = await asyncio.wait_for(server.read_game_event(), 10)
                assert observed is not None
                expected.discard(type(observed.record))
                if isinstance(observed.record, EntityDeathEvent):
                    assert observed.record.data.victim.prefab == "rabbit"
            await server.stop(grace_period=5)
    finally:
        cleanup = await asyncio.create_subprocess_exec(
            "podman",
            "rm",
            "--force",
            "--time",
            "0",
            "--ignore",
            CONTAINER_NAME,
        )
        assert await cleanup.wait() == 0
        if started and server.returncode is None:
            await server.kill()
