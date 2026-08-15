from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import SecretStr

from dst_server.cluster import (
    Cluster,
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
    WorldOverrides,
    service,
)
from dst_server.cluster.layout import Shard
from dst_server.events.server import SavedEvent
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.lifecycle import RequestState


class RuntimeWorld:
    def __init__(self) -> None:
        self.requests = 0

    async def request_save(self) -> None:
        self.requests += 1


class RuntimeHandle:
    def __init__(self, shard: str) -> None:
        self.config = ServerConfig(shard=shard)
        self.world = RuntimeWorld()
        self.game = Mock(world=self.world)
        self.save_states: list[RequestState] = []

    async def execute(
        self,
        command: str,
        completion_timeout: float = 30,
    ) -> str:
        del completion_timeout
        return f"{self.config.shard}:{command}"

    async def _save(
        self,
        request: Callable[[], Awaitable[None]],
        completion_timeout: float,
        request_state: RequestState,
    ) -> SavedEvent:
        self.save_states.append(request_state)
        await request()
        return SavedEvent(
            path=f"session/{self.config.shard}/{int(completion_timeout)}",
            snapshot=1,
        )


def configuration() -> ClusterConfig:
    return ClusterConfig(
        shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))}
    )


def sharded_configuration() -> ClusterConfig:
    return ClusterConfig(
        settings=ClusterSettings(
            master_ip="127.0.0.1",
            cluster_key=SecretStr("test-key"),
        ),
        shards={
            "Master": ShardConfig(settings=ShardSettings(is_master=True)),
            "Caves": ShardConfig(
                settings=ShardSettings(
                    is_master=False,
                    name="Caves",
                    id=2,
                    server_port=11000,
                    master_server_port=27017,
                )
            ),
        },
    )


async def test_service_prepare_routes_logs_with_shard_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = tmp_path / "install"
    executable = install / service.EXECUTABLE
    executable.parent.mkdir(parents=True)
    executable.touch()
    cluster = tmp_path / "cluster"
    configuration().save(cluster)
    monkeypatch.setattr(service.mods, "prepare", lambda *_: ())
    monkeypatch.setattr(service.console, "ensure", Mock())
    monkeypatch.setenv("DST_SERVER_CLUSTER_NAME", "dst-room-42")
    lines: list[tuple[str, str]] = []

    _, servers = await service.prepare(
        install,
        cluster,
        update_mods=False,
        shard_log_handler=lambda shard, line: lines.append((shard, line)),
    )

    assert servers[0].log_handler is not None
    servers[0].log_handler("hello")
    assert lines == [("Master", "hello")]
    assert servers[0].config.telemetry_cluster == "dst-room-42"


async def test_cluster_context_saves_configuration_and_exposes_shards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = RuntimeHandle("Master")
    server = cast(Server, handle)
    prepared = (Shard(name="Master", master=True, console=tmp_path / "console"),)
    logs: list[tuple[str, str]] = []

    def log_handler(shard: str, line: str) -> None:
        logs.append((shard, line))

    def prepare(
        _: Path,
        cluster_path: Path,
        **__: object,
    ) -> tuple[tuple[Shard, ...], tuple[Server, ...]]:
        assert (cluster_path / "cluster.ini").is_file()
        assert (cluster_path / "Master" / "server.ini").is_file()
        assert __["shard_log_handler"] is log_handler
        return prepared, (server,)

    async def serve(
        _: tuple[Shard, ...],
        __: tuple[Server, ...],
        shutdown: asyncio.Event,
        *,
        ready: asyncio.Event | None = None,
        consume_game_events: bool = True,
    ) -> int:
        assert ready is not None
        assert not consume_game_events
        ready.set()
        await shutdown.wait()
        return 0

    monkeypatch.setattr(service, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(service, "serve", serve)
    runtime = Cluster(
        tmp_path / "cluster",
        config=configuration(),
        install_path=tmp_path / "install",
        update_mods=False,
        log_handler=log_handler,
    )
    with pytest.raises(RuntimeError, match="has not been started"):
        runtime["Master"]

    async with runtime:
        assert tuple(runtime.servers) == ("Master",)
        assert runtime["Master"] is server
        assert runtime["Master"].game is handle.game
        assert await runtime["Master"].execute("c_countprefabs()") == (
            "Master:c_countprefabs()"
        )
        assert await runtime.execute_all("c_rollback()") == {
            "Master": "Master:c_rollback()"
        }
        saved = await runtime.save(7)
        assert saved["Master"].path == "session/Master/7"
        waiting = asyncio.create_task(runtime.wait())
        await asyncio.sleep(0)
        assert not waiting.done()

    assert await waiting == 0
    assert not runtime.running


async def test_cluster_without_config_strictly_loads_existing_ini(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster_path = tmp_path / "cluster"
    configuration().save(cluster_path)
    (cluster_path / "cluster.ini").write_text(
        "[NETWORK]\nunknown_option = true\n",
        encoding="utf-8",
    )
    prepare = AsyncMock()
    monkeypatch.setattr(service, "prepare", prepare)

    with pytest.raises(ValueError, match="unknown DST INI option"):
        await Cluster(cluster_path, install_path=tmp_path / "install").start()

    prepare.assert_not_awaited()


async def test_cluster_forwards_level_and_world_override_registries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    level_types = {"Master": WorldOverrides}
    world_types = {"Master": WorldOverrides}
    load = Mock(side_effect=RuntimeError("loaded"))
    monkeypatch.setattr(ClusterConfig, "load", load)

    with pytest.raises(RuntimeError, match="loaded"):
        await Cluster(
            tmp_path / "cluster",
            level_overrides_types=level_types,
            world_overrides_types=world_types,
        ).start()

    load.assert_called_once_with(
        tmp_path / "cluster",
        level_overrides_types=level_types,
        world_overrides_types=world_types,
    )


async def test_cluster_save_requests_master_once_for_all_shards(
    tmp_path: Path,
) -> None:
    master = RuntimeHandle("Master")
    caves = RuntimeHandle("Caves")
    runtime = Cluster(tmp_path / "cluster", config=sharded_configuration())
    runtime._servers.update({
        "Master": cast(Server, master),
        "Caves": cast(Server, caves),
    })
    runtime._task = asyncio.create_task(asyncio.Event().wait())

    try:
        saved = await runtime.save(7)
    finally:
        runtime._task.cancel()
        await asyncio.gather(runtime._task, return_exceptions=True)

    assert tuple(saved) == ("Master", "Caves")
    assert master.world.requests == 1
    assert caves.world.requests == 0
    assert master.save_states[0].status is caves.save_states[0].status
    assert master.save_states[0].resolved is not caves.save_states[0].resolved


async def test_cluster_keeps_strictly_loaded_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster_path = tmp_path / "cluster"
    configuration().save(cluster_path)
    prepare = AsyncMock(side_effect=RuntimeError("stop after load"))
    monkeypatch.setattr(service, "prepare", prepare)
    runtime = Cluster(cluster_path, install_path=tmp_path / "install")

    with pytest.raises(RuntimeError, match="stop after load"):
        await runtime.start()

    assert runtime.config is not None
    assert tuple(runtime.config.shards) == ("Master",)


async def test_cluster_does_not_resolve_away_configuration_root_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    cluster_path = tmp_path / "cluster"
    cluster_path.symlink_to(target, target_is_directory=True)
    prepare = AsyncMock()
    monkeypatch.setattr(service, "prepare", prepare)

    with pytest.raises(ValueError, match="configuration root cannot be a symlink"):
        await Cluster(
            cluster_path,
            config=configuration(),
            install_path=tmp_path / "install",
        ).start()

    prepare.assert_not_awaited()


async def test_cluster_start_failure_uses_service_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = cast(Server, RuntimeHandle("Master"))
    shards = (Shard(name="Master", master=True, console=tmp_path / "console"),)

    def prepare(
        _: Path,
        __: Path,
        **___: object,
    ) -> tuple[tuple[Shard, ...], tuple[Server, ...]]:
        return shards, (handle,)

    failure = RuntimeError("injected partial startup failure")
    start = AsyncMock(side_effect=failure)
    stop = AsyncMock()
    monkeypatch.setattr(service, "prepare", AsyncMock(side_effect=prepare))
    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(service, "stop", stop)
    configure_otel = Mock()
    monkeypatch.setattr(service, "configure_otel", configure_otel)

    with pytest.raises(RuntimeError, match="injected partial startup failure"):
        await Cluster(
            tmp_path / "cluster",
            config=configuration(),
            install_path=tmp_path / "install",
        ).start()

    stop.assert_awaited_once_with((handle,))
    configure_otel.assert_not_called()


async def test_cluster_wait_and_close_propagate_late_service_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = cast(Server, RuntimeHandle("Master"))
    shards = (Shard(name="Master", master=True, console=tmp_path / "console"),)
    failed = asyncio.Event()

    prepare = AsyncMock(return_value=(shards, (handle,)))

    async def serve(
        _: tuple[Shard, ...],
        __: tuple[Server, ...],
        ___: asyncio.Event,
        *,
        ready: asyncio.Event | None = None,
        consume_game_events: bool = True,
    ) -> int:
        assert ready is not None
        assert not consume_game_events
        ready.set()
        await failed.wait()
        msg = "late service failure"
        raise RuntimeError(msg)

    monkeypatch.setattr(service, "prepare", prepare)
    monkeypatch.setattr(service, "serve", serve)
    runtime = Cluster(
        tmp_path / "cluster",
        config=configuration(),
        install_path=tmp_path / "install",
    )
    await runtime.start()
    failed.set()

    with pytest.raises(RuntimeError, match="late service failure"):
        await runtime.wait()
    with pytest.raises(RuntimeError, match="late service failure"):
        await runtime.close()
