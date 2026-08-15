from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Self

from dst_server.events.server import SavedEvent
from dst_server.runtime import Server
from dst_server.runtime.lifecycle import RequestState, RequestStatus
from dst_server.telemetry import TelemetrySettings

from . import service
from .config import ClusterConfig
from .world import WorldOverrides

type ClusterLogHandler = Callable[[str, str], None]


async def _wait_until_ready(
    service_task: asyncio.Task[int],
    ready_task: asyncio.Task[bool],
) -> None:
    done, _ = await asyncio.wait(
        (ready_task, service_task),
        return_when=asyncio.FIRST_COMPLETED,
    )
    if service_task in done:
        returncode = service_task.result()
        msg = f"DST cluster stopped during startup with status {returncode}"
        raise RuntimeError(msg)
    await ready_task


class Cluster:
    def __init__(
        self,
        cluster_path: Path,
        *,
        config: ClusterConfig | None = None,
        install_path: Path = service.DEFAULT_INSTALL_PATH,
        update_mods: bool = True,
        telemetry: TelemetrySettings | None = None,
        log_handler: ClusterLogHandler | None = None,
        consume_game_events: bool = False,
        level_overrides_types: Mapping[str, type[WorldOverrides]] | None = None,
        world_overrides_types: Mapping[str, type[WorldOverrides]] | None = None,
    ) -> None:
        self.cluster_path = cluster_path
        self.install_path = install_path.resolve()
        self.config = config
        self.update_mods = update_mods
        self.telemetry = telemetry
        self.log_handler = log_handler
        self.consume_game_events = consume_game_events
        self.level_overrides_types = level_overrides_types
        self.world_overrides_types = world_overrides_types
        self._servers: dict[str, Server] = {}
        self._shutdown = asyncio.Event()
        self._ready = asyncio.Event()
        self._task: asyncio.Task[int] | None = None

    @property
    def servers(self) -> Mapping[str, Server]:
        return MappingProxyType(self._servers)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def __getitem__(self, shard: str) -> Server:
        if not self._servers:
            msg = "DST cluster has not been started"
            raise RuntimeError(msg)
        return self._servers[shard]

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._task is not None:
            msg = "DST cluster runtime objects are single-use"
            raise RuntimeError(msg)
        if self.config is None:
            self.config = ClusterConfig.load(
                self.cluster_path,
                level_overrides_types=self.level_overrides_types,
                world_overrides_types=self.world_overrides_types,
            )
        else:
            self.config.save(self.cluster_path)

        shards, servers = await service.prepare(
            self.install_path,
            self.cluster_path,
            update_mods=self.update_mods,
            telemetry=self.telemetry,
            shard_log_handler=self.log_handler,
        )
        self._servers.update((server.config.shard, server) for server in servers)
        self._task = asyncio.create_task(
            service.serve(
                shards,
                servers,
                self._shutdown,
                ready=self._ready,
                consume_game_events=self.consume_game_events,
            ),
            name=f"dst-cluster-{self.cluster_path.name}",
        )
        ready = asyncio.create_task(self._ready.wait())
        try:
            await _wait_until_ready(self._task, ready)
        except BaseException as primary:
            self._shutdown.set()
            failures = [primary]
            try:
                await asyncio.shield(self._task)
            except BaseException as cleanup:
                if cleanup is not primary:
                    failures.append(cleanup)
            service.raise_failures(failures)
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)

    async def execute_all(
        self,
        command: str,
        completion_timeout: float = 30,
    ) -> dict[str, str]:
        if not self.running:
            msg = "DST cluster is not running"
            raise RuntimeError(msg)
        async with asyncio.TaskGroup() as tasks:
            executions = {
                name: tasks.create_task(
                    server.execute(command, completion_timeout=completion_timeout)
                )
                for name, server in self._servers.items()
            }
        return {name: task.result() for name, task in executions.items()}

    async def save(self, completion_timeout: float = 30) -> dict[str, SavedEvent]:
        if not self.running:
            msg = "DST cluster is not running"
            raise RuntimeError(msg)
        if self.config is None:
            msg = "DST cluster configuration is unavailable"
            raise RuntimeError(msg)
        master = next(
            name
            for name, shard in self.config.shards.items()
            if shard.settings.is_master
        )
        status = RequestStatus()
        states = {name: RequestState(status) for name in self._servers}
        all_armed = asyncio.Barrier(len(self._servers))
        request_completed = asyncio.Event()

        async def request(name: str) -> None:
            await all_armed.wait()
            if name == master:
                await self._servers[master].game.world.request_save()
                request_completed.set()
            else:
                await request_completed.wait()

        async with asyncio.TaskGroup() as tasks:
            saves = {
                name: tasks.create_task(
                    server._save(  # ruff: ignore[private-member-access]
                        lambda name=name: request(name),
                        completion_timeout,
                        states[name],
                    )
                )
                for name, server in self._servers.items()
            }
        return {name: task.result() for name, task in saves.items()}

    async def wait(self) -> int:
        if self._task is None:
            msg = "DST cluster has not been started"
            raise RuntimeError(msg)
        return await asyncio.shield(self._task)

    async def close(self) -> int:
        if self._task is None:
            return 0
        self._shutdown.set()
        try:
            return await asyncio.shield(self._task)
        except asyncio.CancelledError:
            await asyncio.shield(self._task)
            raise


__all__ = ["Cluster", "ClusterLogHandler"]
