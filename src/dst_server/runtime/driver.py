import asyncio

from dst_server.events import GameEvent
from dst_server.models.driver import DriverHealth


class Driver:
    def __init__(self) -> None:
        self.generation = 0
        self.installed_generation: int | None = None
        self._health: DriverHealth | None = None
        self.error: str | None = None
        self.closed = False
        self.changed = asyncio.Event()

    @property
    def health(self) -> DriverHealth:
        if self._health is None or not self.is_ready(self.generation):
            msg = "DST Lua driver has not been installed"
            raise RuntimeError(msg)
        return self._health

    def starting(self, generation: int) -> None:
        if self.closed or generation <= self.generation:
            return
        self.generation = generation
        self._health = None
        self.error = None
        self._notify()

    def ready(self, health: DriverHealth) -> None:
        if self.closed or health.generation < self.generation:
            return
        self.starting(health.generation)
        self._health = self._merge_health(health)
        self.installed_generation = health.generation
        self.error = None
        self._notify()

    def failed(self, error: str) -> None:
        if not self.closed:
            self.error = error
            self.installed_generation = None
            self._notify()

    def _notify(self) -> None:
        changed = self.changed
        self.changed = asyncio.Event()
        changed.set()

    async def wait_ready(self) -> int:
        while not self.is_ready(self.generation):
            if self.closed:
                msg = "DST Lua driver is closed"
                raise RuntimeError(msg)
            if self.error is not None:
                msg = f"DST native Lua driver failed: {self.error}"
                raise RuntimeError(msg)
            await self.changed.wait()
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
        # Events may arrive before the world publishes driver readiness.
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

    def close(self) -> None:
        self.closed = True
        self._health = None
        self._notify()
