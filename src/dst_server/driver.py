from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from logbook import Logger

from .events import DriverHealth

logger = Logger(__name__)


class DriverManager:
    def __init__(
        self,
        install: Callable[[], Awaitable[DriverHealth]],
        cluster: str,
        shard: str,
    ) -> None:
        self.install_driver = install
        self.cluster = cluster
        self.shard = shard
        self.generation = 0
        self.installed_generation = 0
        self.started = False
        self.closed = False
        self.task: asyncio.Task[None] | None = None

    async def install(self, generation: int) -> DriverHealth:
        health = await self.install_driver()
        self.started = True
        self.generation = max(self.generation, generation)
        self.installed_generation = generation
        self.schedule()
        return health

    def session_started(self, generation: int) -> None:
        self.generation = generation
        self.schedule()

    def schedule(self) -> None:
        if (
            self.closed
            or not self.started
            or self.installed_generation >= self.generation
            or self.task is not None
        ):
            return
        self.task = asyncio.create_task(
            self.refresh(),
            name=f"dst-driver-{self.shard}",
        )

    async def refresh(self) -> None:
        failed = False
        try:
            while self.installed_generation < self.generation:
                generation = self.generation
                logger.info(
                    "reinstall DST Lua driver: {cluster}/{shard} ({generation})",
                    cluster=self.cluster,
                    shard=self.shard,
                    generation=generation,
                )
                await self.install_driver()
                self.installed_generation = generation
        except asyncio.CancelledError:
            raise
        except Exception:
            failed = True
            logger.exception(
                "failed to reinstall DST Lua driver: {cluster}/{shard}",
                cluster=self.cluster,
                shard=self.shard,
            )
        finally:
            self.task = None
            if not failed:
                self.schedule()

    def close(self) -> None:
        self.closed = True
        if self.task is not None:
            self.task.cancel()


__all__ = ["DriverManager"]
