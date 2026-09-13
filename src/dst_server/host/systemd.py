import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from dst_server.concurrency import complete
from dst_server.timeouts import positive_timeout

_DESTINATION = "org.freedesktop.systemd1"
_PATH = "/org/freedesktop/systemd1"
_MANAGER = f"{_DESTINATION}.Manager"


@dataclass(frozen=True, slots=True)
class UnitStatus:
    name: str
    load: str
    active: str
    sub: str
    job_id: int
    job_type: str
    job_path: str


class Systemd:
    """One lazy system-bus connection; close it after the host operation.

    Submissions return immediately; idle units need not be healthy or game-ready.
    """

    def __init__(self, *, bus_address: str | None = None) -> None:
        self._bus_address = bus_address
        self._bus: Any = None
        self._proxy: Any = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _manager(self) -> Any:
        async with self._lock:
            if self._closed:
                msg = "systemd connection is closed"
                raise ConnectionError(msg)
            if self._bus is not None:
                if not self._bus.connected:
                    msg = "systemd bus disconnected"
                    raise ConnectionError(msg)
                return self._proxy

            from dbus_fast import BusType
            from dbus_fast.aio import MessageBus

            bus = MessageBus(bus_address=self._bus_address, bus_type=BusType.SYSTEM)
            try:
                await bus.connect()
                node = await bus.introspect(_DESTINATION, _PATH)
                manager: Any = bus.get_proxy_object(
                    _DESTINATION, _PATH, node
                ).get_interface(_MANAGER)
            except BaseException:
                bus.disconnect()
                raise
            self._bus, self._proxy = bus, manager
            return manager

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            if self._bus is not None:
                self._bus.disconnect()
                with suppress(Exception):
                    await complete(self._bus.wait_for_disconnect())

    async def list_units(self, names: Sequence[str]) -> dict[str, UnitStatus]:
        if not names:
            return {}
        manager = await self._manager()
        rows = await manager.call_list_units_by_names(list(names))
        return {row[0]: UnitStatus(row[0], *row[2:5], *row[7:10]) for row in rows}

    async def start(self, unit: str) -> str:
        manager = await self._manager()
        return await manager.call_start_unit(unit, "replace")

    async def stop(self, unit: str) -> str:
        manager = await self._manager()
        return await manager.call_stop_unit(unit, "replace")

    async def restart(self, unit: str) -> str:
        manager = await self._manager()
        return await manager.call_restart_unit(unit, "replace")

    async def reload(self) -> None:
        manager = await self._manager()
        await manager.call_reload()

    async def wait_idle(self, names: Sequence[str], timeout: float) -> None:  # ruff: ignore[async-function-with-timeout]
        """Wait for transitions to settle; callers check the resulting unit state."""
        async with asyncio.timeout(positive_timeout(timeout)):
            while True:
                states = await self.list_units(names)
                if all(
                    not state.job_id
                    and state.active not in {"activating", "deactivating"}
                    for state in states.values()
                ):
                    return
                await asyncio.sleep(0.2)

    async def exec_start(self, unit: str, *, pre: bool = False) -> tuple[str, ...]:
        """Read systemd's generated command, including native Quadlet drop-ins."""
        manager = await self._manager()
        path = await manager.call_load_unit(unit)
        node = await self._bus.introspect(_DESTINATION, path)
        proxy = self._bus.get_proxy_object(_DESTINATION, path, node)
        properties = proxy.get_interface("org.freedesktop.DBus.Properties")
        property_name = "ExecStartPre" if pre else "ExecStart"
        commands = (
            await properties.call_get(f"{_DESTINATION}.Service", property_name)
        ).value
        if len(commands) != 1 or not commands[0][1]:
            msg = f"expected one {property_name} command for {unit}"
            raise ValueError(msg)
        return tuple(commands[0][1])

    async def reset_failed(self, unit: str) -> None:
        """Clear a failure and start limit for an explicit operator start."""
        manager = await self._manager()
        await manager.call_reset_failed_unit(unit)
