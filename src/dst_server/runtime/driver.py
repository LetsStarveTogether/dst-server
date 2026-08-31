import asyncio
from collections.abc import Awaitable, Callable

from logbook import Logger

from dst_server.game import DriverHealth

logger = Logger(__name__)


class Driver:
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
        self._health: DriverHealth | None = None
        self.started = False
        self.closed = False
        self.task: asyncio.Task[None] | None = None

    async def install(self, generation: int) -> DriverHealth:
        self.generation = max(self.generation, generation)
        while not self.closed:
            attempted_generation = self.generation
            try:
                health = await self.install_driver()
            except Exception:
                if attempted_generation == self.generation:
                    raise
                continue
            if self._commit(attempted_generation, health):
                self.started = True
                return health
        msg = "DST Lua driver was closed before installation completed"
        raise RuntimeError(msg)

    @property
    def health(self) -> DriverHealth:
        if self._health is None:
            msg = "DST Lua driver has not been installed"
            raise RuntimeError(msg)
        return self._health

    def session_started(self, generation: int) -> None:
        if self.task is not None and self.task.done():
            self.task = None
        self.generation = generation
        self._health = None
        self.schedule()

    async def wait_ready(self) -> int:
        if not self.started:
            msg = "DST Lua driver has not been installed"
            raise RuntimeError(msg)
        while self.installed_generation < self.generation:
            task = self.task
            if task is None or task.done():
                msg = f"DST Lua driver is unavailable for generation {self.generation}"
                raise RuntimeError(msg)
            await asyncio.wait((task,))
        return self.generation

    def is_ready(self, generation: int) -> bool:
        return (
            generation == self.generation == self.installed_generation
            and self._health is not None
        )

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
        attempted_generation = self.installed_generation
        try:
            while not self.closed and self.installed_generation < self.generation:
                attempted_generation = self.generation
                logger.info(
                    "reinstall DST Lua driver: {cluster}/{shard} ({generation})",
                    cluster=self.cluster,
                    shard=self.shard,
                    generation=attempted_generation,
                )
                try:
                    health = await self.install_driver()
                except Exception:
                    if attempted_generation != self.generation:
                        continue
                    failed = True
                    logger.exception(
                        "failed to reinstall DST Lua driver: {cluster}/{shard}",
                        cluster=self.cluster,
                        shard=self.shard,
                    )
                    return
                self._commit(attempted_generation, health)
        finally:
            if self.closed or not failed or attempted_generation < self.generation:
                self.task = None
                self.schedule()

    def _commit(self, generation: int, health: DriverHealth) -> bool:
        if self.closed or generation != self.generation:
            return False
        self.installed_generation = generation
        self._health = health
        return True

    def close(self) -> None:
        self.closed = True
        if self.task is not None:
            self.task.cancel()
