import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from dbus_fast.aio import MessageBus
from dbus_fast.annotations import DBusObjectPath, DBusSignature, DBusStr
from dbus_fast.service import ServiceInterface, dbus_method

from dst_server.host.systemd import Systemd, UnitStatus

JOB = "/org/freedesktop/systemd1/job/42"
UNIT = "dst-000-pod.service"
SHARD = "dst-000-forest.service"


@pytest.fixture
async def adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[Systemd, MagicMock, MagicMock]]:
    manager = MagicMock()
    for action in ("start", "stop", "restart"):
        setattr(manager, f"call_{action}_unit", AsyncMock(return_value=JOB))
    manager.call_reload = AsyncMock()
    manager.call_reset_failed_unit = AsyncMock()
    manager.call_list_units_by_names = AsyncMock(return_value=[])
    bus = MagicMock(connected=True)
    bus.connect = AsyncMock(return_value=bus)
    bus.introspect = AsyncMock()
    bus.get_proxy_object.return_value.get_interface.return_value = manager
    bus.wait_for_disconnect = AsyncMock()
    monkeypatch.setattr("dbus_fast.aio.MessageBus", MagicMock(return_value=bus))
    async with Systemd() as systemd:
        yield systemd, manager, bus


async def test_lazy_connection_shared_by_concurrent_operations(
    adapter: tuple[Systemd, MagicMock, MagicMock],
) -> None:
    systemd, manager, bus = adapter
    assert await systemd.list_units([]) == {}
    bus.connect.assert_not_awaited()
    manager.call_list_units_by_names.return_value = [
        [UNIT, "room", "loaded", "activating", "start", "", "/unit", 42, "start", JOB],
        ["missing.service", "", "not-found", "inactive", "dead", "", "/", 0, "", "/"],
    ]
    listed, job = await asyncio.gather(systemd.list_units([UNIT]), systemd.start(UNIT))
    assert listed[UNIT] == UnitStatus(
        UNIT, "loaded", "activating", "start", 42, "start", JOB
    )
    assert listed["missing.service"].load == "not-found"
    assert job == JOB
    bus.connect.assert_awaited_once()


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
async def test_lifecycle_submission_uses_systemd_replace_mode(
    adapter: tuple[Systemd, MagicMock, MagicMock], action: str
) -> None:
    systemd, manager, _ = adapter
    assert await getattr(systemd, action)(UNIT) == JOB
    getattr(manager, f"call_{action}_unit").assert_awaited_once_with(UNIT, "replace")


async def test_reload(
    adapter: tuple[Systemd, MagicMock, MagicMock],
) -> None:
    systemd, manager, _ = adapter
    await systemd.reload()
    manager.call_reload.assert_awaited_once()


def unit_row(name: str, active: str, job: int = 0) -> list[Any]:
    return [
        name,
        "room",
        "loaded",
        active,
        "",
        "",
        "/unit",
        job,
        "",
        JOB if job else "/",
    ]


class PrivateManager(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.freedesktop.systemd1.Manager")
        self.states = [unit_row(UNIT, "active")]
        self.observed: asyncio.Queue[list[str]] = asyncio.Queue()
        self.reply = asyncio.Event()
        self.reply.set()

    @dbus_method(name="ListUnitsByNames")
    async def list_units(
        self, names: Annotated[list[str], DBusSignature("as")]
    ) -> Annotated[list[Any], DBusSignature("a(ssssssouso)")]:
        rows = [row for row in self.states if row[0] in names]
        self.observed.put_nowait(names)
        await self.reply.wait()
        return rows

    @dbus_method(name="StartUnit")
    def start_unit(self, name: DBusStr, mode: DBusStr) -> DBusObjectPath:
        assert name == UNIT
        assert mode == "replace"
        return JOB


@pytest.fixture
async def private_manager(
    tmp_path: Path,
) -> AsyncIterator[tuple[str, PrivateManager]]:
    if (daemon := shutil.which("dbus-daemon")) is None:
        pytest.skip("dbus-daemon is unavailable")
    process = await asyncio.create_subprocess_exec(
        daemon,
        "--session",
        "--nofork",
        "--print-address=1",
        f"--address=unix:path={tmp_path}/bus",
        stdout=asyncio.subprocess.PIPE,
    )
    server = None
    try:
        assert process.stdout is not None
        async with asyncio.timeout(5):
            address = (await process.stdout.readline()).decode().strip()
            server = await MessageBus(bus_address=address).connect()
            manager = PrivateManager()
            server.export("/org/freedesktop/systemd1", manager)
            await server.request_name("org.freedesktop.systemd1")
        yield address, manager
    finally:
        try:
            if server is not None:
                server.disconnect()
                await asyncio.wait_for(server.wait_for_disconnect(), 2)
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), 2)


async def test_wait_idle_tracks_all_units_until_their_transitions_settle(
    private_manager: tuple[str, PrivateManager],
) -> None:
    address, manager = private_manager
    manager.states.append(unit_row(SHARD, "inactive", 42))
    names = [UNIT, SHARD, "missing.service"]
    async with Systemd(bus_address=address) as systemd:
        pending = asyncio.create_task(systemd.wait_idle(names, 5))
        try:
            # Each next request proves the previous response did not end the wait.
            for active in ("activating", "deactivating", "failed"):
                assert await asyncio.wait_for(manager.observed.get(), 1) == names
                manager.states = [unit_row(UNIT, "active"), unit_row(SHARD, active)]
            assert await asyncio.wait_for(manager.observed.get(), 1) == names
            await asyncio.wait_for(pending, 1)
            assert (await systemd.list_units([SHARD]))[SHARD].active == "failed"
        finally:
            pending.cancel()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 1)


@pytest.mark.parametrize("finish", ["timeout", "cancel"])
async def test_interrupted_wait_does_not_change_units_or_close_the_connection(
    private_manager: tuple[str, PrivateManager], finish: str
) -> None:
    address, manager = private_manager
    async with Systemd(bus_address=address) as systemd:
        await systemd.list_units([UNIT])
        manager.observed.get_nowait()
        manager.reply.clear()
        pending = asyncio.create_task(
            systemd.wait_idle([UNIT], 2 if finish == "timeout" else 60)
        )
        completed = asyncio.gather(pending, return_exceptions=True)
        try:
            await asyncio.wait_for(manager.observed.get(), 1)
            if finish == "cancel":
                pending.cancel()
            (outcome,) = await asyncio.wait_for(asyncio.shield(completed), 5)
            expected = TimeoutError if finish == "timeout" else asyncio.CancelledError
            assert isinstance(outcome, expected)
        finally:
            manager.reply.set()
            pending.cancel()
            await asyncio.wait_for(asyncio.shield(completed), 1)
        assert await systemd.start(UNIT) == JOB
        assert (await systemd.list_units([UNIT]))[UNIT].active == "active"


async def test_close_disconnects_private_bus_and_rejects_later_operations(
    private_manager: tuple[str, PrivateManager],
) -> None:
    address, _ = private_manager
    async with Systemd(bus_address=address) as systemd:
        await systemd.list_units([UNIT])
        await systemd.aclose()
        assert not systemd._bus.connected
        await systemd.aclose()
        with pytest.raises(ConnectionError, match="closed"):
            await systemd.start(UNIT)


@pytest.mark.parametrize("pre", [False, True])
async def test_exec_start_reads_native_generated_argv_from_a_loaded_unit(
    adapter: tuple[Systemd, MagicMock, MagicMock], pre: bool
) -> None:
    systemd, manager, _ = adapter
    manager.call_load_unit = AsyncMock(
        return_value="/org/freedesktop/systemd1/unit/test"
    )
    argv = ["/usr/bin/podman", "run", "--env", "CUSTOM=a value", "image:tag"]
    manager.call_get = AsyncMock(
        return_value=SimpleNamespace(value=[("/usr/bin/podman", argv)])
    )
    assert await systemd.exec_start(SHARD, pre=pre) == tuple(argv)
    manager.call_load_unit.assert_awaited_once_with(SHARD)
    manager.call_get.assert_awaited_once_with(
        "org.freedesktop.systemd1.Service", "ExecStartPre" if pre else "ExecStart"
    )


@pytest.mark.parametrize(
    "commands", [[], [("/bin/true", [])], [("/bin/true", ["/bin/true"])] * 2]
)
async def test_exec_start_rejects_missing_or_multiple_commands(
    adapter: tuple[Systemd, MagicMock, MagicMock], commands: list[object]
) -> None:
    systemd, manager, _ = adapter
    manager.call_load_unit = AsyncMock(
        return_value="/org/freedesktop/systemd1/unit/test"
    )
    manager.call_get = AsyncMock(return_value=SimpleNamespace(value=commands))
    with pytest.raises(ValueError, match="expected one ExecStart"):
        await systemd.exec_start(SHARD)


async def test_reset_failed_is_explicit_per_unit(
    adapter: tuple[Systemd, MagicMock, MagicMock],
) -> None:
    systemd, manager, _ = adapter
    await systemd.reset_failed(UNIT)
    manager.call_reset_failed_unit.assert_awaited_once_with(UNIT)
