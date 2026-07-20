from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

from dst_server.events import server as server_events
from dst_server.events import world
from dst_server.game import DriverHealth
from dst_server.runtime import Server, ServerConfig
from tests.helpers import FAKE_SERVER, StubServer, structured_result


class ReloadingServer(Server):
    def __init__(self) -> None:
        super().__init__(ServerConfig(shard="test"))
        self.installs = 0

    async def install_driver(self) -> DriverHealth:
        self.installs += 1
        health = DriverHealth.model_validate(
            {
                "protocol": 1,
                "installed": True,
                "profile": "history",
                "events_emitted": self.installs,
                "errors": 0,
                "players": 0,
                "action_hook": True,
                "shard_hook": True,
            },
            strict=True,
        )
        self.game.health = health
        return health


async def test_cloud_protocol_and_lifecycle(tmp_path: Path) -> None:
    executable = tmp_path / "fake-server"
    executable.write_text(FAKE_SERVER, encoding="utf-8")
    executable.chmod(0o755)
    config = ServerConfig(
        shard="forest",
        executable=executable,
        persistent_storage_root=tmp_path,
        conf_dir="conf",
        cluster="Cluster_1",
        ugc_directory=None,
        extra_args=(),
    )
    logs: list[str] = []
    server = Server(config, log_handler=logs.append)

    await server.start()

    assert server.driver_health.installed is True
    assert server.driver_health.action_hook is True
    assert server.driver_health.shard_hook is True
    assert server.session_id == "TEST"
    observed = await server.read_game_event()
    assert observed is not None
    assert isinstance(observed.record, world.StateChangedEvent)
    assert observed.record.data.name == "cycles"
    assert observed.observed_timestamp_ns > 0
    assert await server.execute('print("hello")') == 'result:print("hello")'
    await asyncio.sleep(0)
    assert "command received" in logs
    observed = await server.read_game_event()
    assert observed is not None
    assert observed.record.event == "dst.entity.death"
    event = await server.read_event()
    assert isinstance(event, server_events.SessionEvent)
    assert event.session_id == "TEST"
    with pytest.raises(ValueError, match="single line"):
        await server.execute("print(1)\nprint(2)")
    assert await server.stop() == -signal.SIGKILL
    event = await server.read_event()
    assert event is not None
    assert event.event == "shutdown"
    event = await server.read_event()
    assert isinstance(event, server_events.SavedEvent)
    assert event.snapshot == 1
    event = await server.read_event()
    assert event is not None
    assert event.event == "stopping"


def test_server_config() -> None:
    config = ServerConfig(shard="cave")
    command = config.command(monitor_parent_process=42)

    assert command[-3:] == ("42", "-skip_update_server_mods", "-cloudserver")
    assert command[command.index("-shard") + 1] == "cave"
    lua_files = {
        path.relative_to(config.lua_directory).as_posix()
        for path in config.lua_directory.rglob("*.lua")
    }
    assert "dst_server.lua" in lua_files
    assert "dst_server/commands.lua" in lua_files
    assert "dst_server/world_events.lua" in lua_files


async def test_save_waits_for_fd5_completion() -> None:
    server = StubServer([structured_result(data=True)])
    reader = asyncio.StreamReader()
    pump = asyncio.create_task(
        server.lifecycle.pump(reader, server.driver.session_started)
    )
    saving = asyncio.create_task(server.save())
    await asyncio.sleep(0)

    reader.feed_data(b"DST_Saved|session/TEST/27\n")

    saved = await saving
    reader.feed_eof()
    await pump
    assert saved.snapshot == 27


async def test_driver_is_reinstalled_after_lua_session_reload() -> None:
    server = ReloadingServer()
    await server.driver.install(0)
    reader = asyncio.StreamReader()
    pump = asyncio.create_task(
        server.lifecycle.pump(reader, server.driver.session_started)
    )

    reader.feed_data(b"DST_SessionId|ONE\nDST_SessionId|ONE\n")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    if server.driver.task is not None:
        await server.driver.task
    reader.feed_eof()
    await pump
    assert server.installs == 2
    assert server.driver_health.events_emitted == 2
