import asyncio
from collections.abc import Awaitable, Callable

from logbook import Logger

from dst_server.events import GameEvent
from dst_server.game import DriverHealth

logger = Logger(__name__)


class Driver:
    def __init__(
        self,
        install: Callable[[int], Awaitable[DriverHealth]],
        cluster: str,
        shard: str,
    ) -> None:
        self.install_driver = install
        self.cluster = cluster
        self.shard = shard
        self.generation = 0
        self.installed_generation: int | None = None
        self._health: DriverHealth | None = None
        self.closed = False
        # Keep completed tasks so the same generation is never installed twice.
        self.task: asyncio.Task[None] | None = None

    async def install(self, generation: int) -> DriverHealth:
        self.session_started(generation)
        if self.task is None and not self.closed:
            self._schedule()
        await self.wait_ready()
        return self.health

    @property
    def health(self) -> DriverHealth:
        if self._health is None or not self.is_ready(self.generation):
            msg = "DST Lua driver has not been installed"
            raise RuntimeError(msg)
        return self._health

    def session_started(self, generation: int) -> None:
        if self.closed or generation <= self.generation:
            return
        self.generation = generation
        self._health = None
        if self.task is not None and self.task.done():
            self._schedule()

    async def wait_ready(self) -> int:
        while not self.is_ready(self.generation):
            if self.closed:
                msg = "DST Lua driver is closed"
                raise RuntimeError(msg)
            task = self.task
            if task is None or task.done():
                reason = (
                    "has not been installed"
                    if self.installed_generation is None
                    else "is unavailable"
                )
                msg = f"DST Lua driver {reason} for generation {self.generation}"
                raise RuntimeError(msg)
            await asyncio.wait((task,))
        return self.generation

    def is_ready(self, generation: int) -> bool:
        return (
            not self.closed
            and generation == self.generation == self.installed_generation
            and self._health is not None
        )

    def observe_event(self, record: GameEvent) -> None:
        if self.closed or record.generation != self.generation:
            return
        # Events can arrive on stdout before installation returns on the result pipe.
        health = self._health or DriverHealth(
            protocol=2,
            generation=record.generation,
            telemetry_status="active",
            last_error=None,
            events_emitted=0,
            errors=0,
        )
        health = health.replace(events_emitted=max(health.events_emitted, record.seq))
        if record.event == "dst.telemetry.error" and record.data.count >= health.errors:
            health = health.replace(
                telemetry_status=(
                    "degraded"
                    if health.telemetry_status == "active"
                    else health.telemetry_status
                ),
                last_error=record.data,
                errors=record.data.count,
            )
        self._health = health

    def observe_health(self, generation: int, health: DriverHealth) -> None:
        if generation == health.generation and self.is_ready(generation):
            self._health = self._merge_health(health)

    def _merge_health(self, health: DriverHealth) -> DriverHealth:
        previous = self._health
        if previous is None:
            return health
        if previous.errors > health.errors:
            health = health.replace(
                telemetry_status=(
                    "degraded"
                    if health.telemetry_status == "active"
                    else health.telemetry_status
                ),
                last_error=previous.last_error,
                errors=previous.errors,
            )
        return health.replace(
            events_emitted=max(previous.events_emitted, health.events_emitted)
        )

    def _schedule(self) -> None:
        self.task = asyncio.create_task(
            self._install(),
            name=f"dst-driver-{self.shard}",
        )

    async def _install(self) -> None:
        while not self.closed:
            generation = self.generation
            logger.info(
                "install DST Lua driver: {cluster}/{shard} ({generation})",
                cluster=self.cluster,
                shard=self.shard,
                generation=generation,
            )
            try:
                health = await self.install_driver(generation)
                if self._commit(generation, health):
                    return
            except Exception:
                if generation != self.generation:
                    continue
                logger.exception(
                    "failed to install DST Lua driver: {cluster}/{shard}",
                    cluster=self.cluster,
                    shard=self.shard,
                )
                return

    def _commit(self, generation: int, health: DriverHealth) -> bool:
        if self.closed or generation != self.generation:
            return False
        if health.generation != generation:
            msg = (
                f"DST Lua driver returned generation {health.generation}, "
                f"expected generation {generation}"
            )
            raise RuntimeError(msg)
        self._health = self._merge_health(health)
        self.installed_generation = generation
        return True

    def close(self) -> None:
        self.closed = True
        self._health = None
        if self.task is not None:
            self.task.cancel()
