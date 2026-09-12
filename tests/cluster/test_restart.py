import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from dst_server import commands as c
from dst_server.cluster import service
from dst_server.cluster.agent import ShardAgent
from dst_server.configuration.files import Shard
from dst_server.errors import ControllerOperationError
from dst_server.models.cluster import ShardPhase
from dst_server.runtime import ServerConfig
from tests.cluster.helpers import layout, managed_controller
from tests.runtime.helpers import FAKE_SERVER


@pytest.mark.parametrize("automatic_updates", ["true", "false"])
async def test_cluster_restart_prepares_every_shard_and_forces_one_mod_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    automatic_updates: str,
) -> None:
    monkeypatch.setenv("DST_SERVER_MOD_AUTO_UPDATE", automatic_updates)
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, prepare, calls = room
        previous = await instance.status()
        prepare.reset_mock()
        calls.clear()

        def update(*_: object, **__: object) -> tuple[Shard, ...]:
            assert master.pid is None
            assert caves.pid is None
            return layout(tmp_path / "cluster")

        prepare.side_effect = update
        await instance.restart(notice=None)
        current = await instance.status()
        prepare.assert_awaited_once_with(
            tmp_path / "install", tmp_path / "cluster", update_mods=True
        )
        assert current.phase == "running"
        assert current.prepared
        assert previous.prepared
        for before, after in zip(previous.shards, current.shards, strict=True):
            assert before.name == after.name
            assert after.ready
            assert after.game_attempt != before.game_attempt
            assert calls.count(f"activate:{after.name}") == 1
            assert (
                calls.index(f"stop:{after.name}")
                < calls.index(f"activate:{after.name}")
                < calls.index(f"start:{after.name}")
            )
        assert "announce:Master" not in calls


@pytest.mark.parametrize("name", ["Master", "Caves"])
async def test_shard_restart_reactivates_only_its_target_without_shared_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, master, caves, prepare, calls = room
        target, peer = (master, caves) if name == "Master" else (caves, master)
        target_attempt, peer_attempt = target.attempt, peer.attempt
        target.requests.clear()
        peer.requests.clear()
        calls.clear()
        prepare.reset_mock()
        await instance.shard(name).restart(notice=None)
        assert target.ready
        assert target.attempt != target_attempt
        assert peer.ready
        assert peer.attempt == peer_attempt
        assert peer.requests == []
        assert calls == [f"save:{name}", f"activate:{name}", f"restart:{name}"]
        prepare.assert_not_awaited()
        assert (await instance.status()).phase == "running"


@pytest.mark.parametrize("scope", ["cluster", "shard"])
async def test_restart_rejects_a_game_that_returns_without_becoming_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    async with managed_controller(tmp_path, monkeypatch) as room:
        instance, _, caves, _, _ = room

        async def not_ready(command: c.Request[None]) -> None:
            await caves.dispatch(command)
            caves.ready = False

        caves.handlers[c.Start if scope == "cluster" else c.Restart] = not_ready
        endpoint = instance if scope == "cluster" else instance.shard("Caves")
        with pytest.raises((RuntimeError, ControllerOperationError)):
            await endpoint.restart(notice=None)
        assert (await instance.status()).phase != "running"


async def test_agent_restart_replaces_the_process_and_lua_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "fake-server"
    executable.write_text(FAKE_SERVER)
    executable.chmod(0o755)
    monkeypatch.setattr(
        service,
        "create_server_config",
        Mock(
            return_value=ServerConfig(
                shard="Master", executable=executable, persistent_storage_root=tmp_path
            )
        ),
    )
    monkeypatch.setattr(service, "activate_shard", Mock())
    monkeypatch.setattr(service, "configure_otel", Mock(return_value=None))
    agent = ShardAgent(
        Shard("Master", True), install_path=tmp_path, cluster_path=tmp_path
    )
    try:
        async with asyncio.timeout(10):
            await agent.activate()
            await agent.start()
            previous = agent.server
            assert previous.driver.is_ready(previous.driver.generation)
            await agent.restart()
            current = agent.server
        assert current is not previous
        assert current.process.pid != previous.process.pid
        assert current.game_events.nonce != previous.game_events.nonce
        assert current.lifecycle.ready
        assert current.driver is not previous.driver
        assert current.driver.is_ready(current.driver.generation)
        assert previous.closed
        assert previous.driver.closed
        assert previous.returncode is not None
        assert (await agent.runtime_status()).phase is ShardPhase.RUNNING
    finally:
        await agent.aclose()
