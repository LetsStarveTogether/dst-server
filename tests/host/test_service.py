import asyncio
import shlex
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from pydantic import SecretStr, ValidationError
from ulid import ULID

from dst_server.announcements import MOD_UPDATE_NOTICE, Countdown
from dst_server.configuration.files import discover
from dst_server.configuration.models import ClusterConfig
from dst_server.host import service
from dst_server.host.service import Host
from dst_server.host.systemd import UnitStatus
from dst_server.logs import JournalResult
from dst_server.models.cluster import (
    ClusterStatus,
    ShardDesired,
    ShardPhase,
    ShardRuntimeStatus,
)
from dst_server.presets.lst import fleet_room
from dst_server.rooms import DailyWindow, read_control, write_control
from tests.helpers import wait_for_event


@pytest.fixture
async def host(tmp_path: Path) -> Host:
    manager = Mock()
    states: dict[str, str] = {}
    manager.list_units = AsyncMock(
        side_effect=lambda names: {
            name: UnitStatus(
                name, "loaded", states.get(name, "inactive"), "dead", 0, "", "/"
            )
            for name in names
        }
    )
    manager.start = AsyncMock(
        side_effect=lambda name: states.__setitem__(name, "active") or "start-job"
    )
    manager.restart = AsyncMock(
        side_effect=lambda name: states.__setitem__(name, "active") or "restart-job"
    )
    manager.stop = AsyncMock(
        side_effect=lambda name: states.__setitem__(name, "inactive") or f"stop-{name}"
    )
    manager.wait_idle = AsyncMock()
    manager.reload = AsyncMock()
    manager.reset_failed = AsyncMock()
    manager.aclose = AsyncMock()
    manager.states = states
    instance = Host(tmp_path / "rooms", tmp_path / "quadlets", systemd=manager)
    await instance.create(fleet_room(0, token=SecretStr("test-token")))

    async def exec_start(unit: str, *, pre: bool = False) -> tuple[str, ...]:  # ruff: ignore[unused-async]
        from dst_server.deployment.application import QuadletApplication

        application = QuadletApplication.load(instance.quadlet_dir, name="dst-000")
        if pre:
            assert unit == instance.unit(0)
            return (
                "/usr/bin/podman",
                "pod",
                "create",
                *(
                    ("--userns", application.pod.userns)
                    if application.pod.userns
                    else ()
                ),
            )
        assert unit == f"{application.master.name}.service"
        return (
            "/usr/bin/podman",
            "run",
            *(
                argument
                for volume in application.master.volumes
                for argument in ("--volume", volume.render())
            ),
            *(
                argument
                for key, value in application.master.environment.items()
                for argument in ("--env", f"{key}={value}")
            ),
            application.master.image,
            *application.master.exec,
        )

    manager.exec_start = AsyncMock(side_effect=exec_start)
    return instance


async def test_offline_edit_preserves_permissions_and_removes_unset_managed_override(
    host: Host,
) -> None:
    root = host.rooms.path(0)
    original = host.rooms.load(0)
    (root / "blocklist.txt").write_text("KU_newban\n")
    (root / "forest" / "user.lua").write_text("custom")
    updated = original.edit_many((
        ("/cluster/settings/max_players", 10),
        ("/cluster/shards/forest/world", None),
    ))
    await host.edit(updated)
    assert host.rooms.load(0).cluster.settings.max_players == 10
    assert not (root / "forest" / "worldgenoverride.lua").exists()
    assert (root / "forest" / "user.lua").read_text() == "custom"
    assert (root / "blocklist.txt").read_text() == "KU_newban\n"
    host.systemd.stop.assert_not_awaited()
    host.systemd.start.assert_not_awaited()


@pytest.mark.parametrize("state", ["running", "shard-only", "queued"])
@pytest.mark.parametrize("change", ["game", "deployment", "shards", "policy"])
async def test_edit_requires_all_units_stopped_without_mutating_files(
    host: Host, state: str, change: str
) -> None:
    original = host.rooms.load(0)
    updated = {
        "game": original.edit("/cluster/settings/max_players", 10),
        "deployment": original.edit("/deployment/image", "localhost/custom:latest"),
        "shards": original.edit("/cluster/shards/cave", unset=True),
        "policy": original.replace(recycle=False),
    }[change]
    active_unit = "dst-000-forest.service" if state == "shard-only" else host.unit(0)
    host.systemd.list_units.side_effect = lambda names: {
        name: UnitStatus(
            name,
            "loaded",
            "active" if name == active_unit and state != "queued" else "inactive",
            "dead",
            int(name == active_unit and state == "queued"),
            "start" if name == active_unit and state == "queued" else "",
            "/",
        )
        for name in names
    }
    root = host.rooms.path(0)

    def contents() -> dict[Path, bytes]:
        return {
            path: path.read_bytes()
            for directory in (root, host.quadlet_dir)
            for path in directory.rglob("*")
            if path.is_file() and path.suffix != ".lock"
        }

    before = contents()
    with pytest.raises(RuntimeError, match="stopped room"):
        await host.edit(updated)
    assert contents() == before
    assert host.rooms.load(0) == original
    for action in ("stop", "start", "restart", "wait_idle", "reload"):
        getattr(host.systemd, action).assert_not_awaited()


async def test_removed_shard_is_inactive_and_readding_it_reuses_the_untouched_save(
    host: Host,
) -> None:
    original = host.rooms.load(0)
    save = host.rooms.path(0) / "cave" / "save"
    save.mkdir()
    world = save / "world"
    world.write_text("keep this world")
    save.chmod(0o750)
    world.chmod(0o640)
    before = {path: path.stat() for path in (save, world)}
    await host.edit(original.edit("/cluster/shards/cave", unset=True))
    assert tuple(shard.name for shard in discover(host.rooms.path(0))) == ("forest",)
    assert world.read_text() == "keep this world"
    assert not (host.quadlet_dir / "dst-000-cave.container").exists()
    assert tuple(ClusterConfig.load(host.rooms.path(0)).shards) == ("forest",)
    await host.edit(original)
    assert {shard.name for shard in discover(host.rooms.path(0))} == {"forest", "cave"}
    assert (host.quadlet_dir / "dst-000-cave.container").exists()
    assert world.read_text() == "keep this world"
    for path, old in before.items():
        current = path.stat()
        assert (current.st_ino, current.st_mode, current.st_uid, current.st_gid) == (
            old.st_ino,
            old.st_mode,
            old.st_uid,
            old.st_gid,
        )
    with pytest.raises(ValidationError, match="unsafe DST shard"):
        original.edit_many((), unset=("/cluster/shards/cave",)).edit(
            "/cluster/shards/.hidden",
            original.cluster.shards["cave"].model_dump(mode="json"),
        )


async def test_schedule_edit_keeps_pause(host: Host) -> None:
    original = host.rooms.load(0)
    root = host.rooms.path(0)
    write_control(
        root,
        read_control(root).model_copy(update={"paused": True}),
    )
    await host.edit(
        original.replace(schedule=(DailyWindow(start=time(9), end=time(12)),)),
    )
    control = read_control(root)
    assert control.paused


@pytest.fixture
def ready_game() -> ClusterStatus:
    return ClusterStatus(
        epoch=ULID(),
        phase="running",
        master="forest",
        shards=tuple(
            ShardRuntimeStatus(
                name=name,
                is_master=name == "forest",
                desired=ShardDesired.RUNNING,
                phase=ShardPhase.RUNNING,
                ready=True,
                telemetry_profile="history",
            )
            for name in ("forest", "cave")
        ),
    )


@pytest.fixture
def agent(host: Host, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    client = SimpleNamespace(
        status=AsyncMock(return_value=SimpleNamespace(busy=False)),
        update_mods=AsyncMock(),
        list_players=AsyncMock(return_value=()),
        announce=AsyncMock(),
        opened=0,
        closed=0,
    )
    host.systemd.states[host.unit(0)] = "active"

    @asynccontextmanager
    async def connect(number: int) -> AsyncIterator[SimpleNamespace]:
        assert number == 0
        client.opened += 1
        try:
            yield client
        finally:
            client.closed += 1

    monkeypatch.setattr(host, "connect", connect)
    return client


async def test_wait_ready_requires_every_expected_shard_to_be_ready(
    host: Host, agent: SimpleNamespace, ready_game: ClusterStatus
) -> None:
    requested: asyncio.Queue[None] = asyncio.Queue()
    responses: asyncio.Queue[ClusterStatus] = asyncio.Queue()

    async def status() -> ClusterStatus:
        requested.put_nowait(None)
        return await responses.get()

    agent.status.side_effect = status
    async with asyncio.timeout(10), asyncio.TaskGroup() as tasks:
        waiting = tasks.create_task(host.wait_ready(0))
        for state in (
            ready_game.replace(
                missing_shards=("cave",), shards=(ready_game.shards[0],)
            ),
            ready_game.replace(
                shards=(ready_game.shards[0], ready_game.shards[1].replace(ready=False))
            ),
            ready_game,
        ):
            next_request = tasks.create_task(requested.get())
            completed, _ = await asyncio.wait(
                (next_request, waiting), return_when=asyncio.FIRST_COMPLETED
            )
            assert next_request in completed, "returned before every shard was ready"
            responses.put_nowait(state)
        result = await waiting
    assert result["game"] == ready_game
    assert agent.opened == agent.closed == 3


@pytest.mark.parametrize("failure", ["unit", "game", "unreachable"])
async def test_wait_ready_reports_failure_and_releases_agent(
    host: Host, agent: SimpleNamespace, ready_game: ClusterStatus, failure: str
) -> None:
    if failure == "unit":
        host.systemd.states[host.unit(0)] = "failed"
    elif failure == "game":
        agent.status.return_value = ready_game.replace(phase="failed", error="bad save")
    else:
        agent.status.side_effect = ConnectionError("agent disconnected")
    async with asyncio.timeout(5):
        if failure == "unreachable":
            with pytest.raises(TimeoutError):
                await host.wait_ready(0, timeout=0.1)
        else:
            with pytest.raises(
                RuntimeError,
                match="bad save" if failure == "game" else "failed to start",
            ):
                await host.wait_ready(0)
    assert agent.opened == agent.closed
    assert agent.opened == (0 if failure == "unit" else 1)


async def test_cancel_wait_ready_closes_inflight_connection(
    host: Host, agent: SimpleNamespace
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def status() -> None:
        entered.set()
        await release.wait()

    agent.status.side_effect = status
    waiting = asyncio.create_task(host.wait_ready(0))
    try:
        await wait_for_event(entered, waiting)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(waiting), 5)
        assert agent.opened == agent.closed == 1
    finally:
        release.set()
        waiting.cancel()
        await asyncio.wait_for(asyncio.gather(waiting, return_exceptions=True), 5)


async def test_closed_mod_update_never_starts_game_and_running_room_requires_restart(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(service, "run_process", runner)
    await host.update_mods(0)
    assert runner.await_count == 2
    host.systemd.start.assert_not_awaited()
    host.systemd.restart.assert_not_awaited()
    host.systemd.states["dst-000-forest.service"] = "active"
    with pytest.raises(RuntimeError, match="require --restart"):
        await host.update_mods(0)


@pytest.mark.parametrize("notice", [MOD_UPDATE_NOTICE, None])
async def test_running_mod_update_uses_controller_without_restarting_containers(
    host: Host,
    agent: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    notice: Countdown | None,
) -> None:
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(service, "run_process", runner)
    before = read_control(host.rooms.path(0))
    await host.update_mods(0, restart=True, notice=notice)
    agent.update_mods.assert_awaited_once_with(restart=True, notice=notice)
    assert agent.opened == agent.closed == 1
    assert read_control(host.rooms.path(0)) == before
    runner.assert_not_awaited()
    host.systemd.stop.assert_not_awaited()
    host.systemd.start.assert_not_awaited()
    host.systemd.restart.assert_not_awaited()


async def test_manual_stop_interrupts_service_without_waiting_for_running_mod_update(
    host: Host, agent: SimpleNamespace
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def update_mods(*, restart: bool, notice: Countdown | None) -> None:
        assert restart
        assert notice == MOD_UPDATE_NOTICE
        entered.set()
        await release.wait()

    agent.update_mods.side_effect = update_mods
    host.systemd.states.update(dict.fromkeys(host.units(0), "active"))
    updating = asyncio.create_task(host.update_mods(0, restart=True))
    try:
        async with asyncio.timeout(5):
            await wait_for_event(entered, updating)
            agent.status.return_value = SimpleNamespace(busy=True)
            with pytest.raises(RuntimeError, match="busy"):
                await host.start(0, wait=False)
            await host.stop(0, wait=False)
            assert read_control(host.rooms.path(0)).paused
            assert not updating.done()
            release.set()
            await updating
        agent.update_mods.assert_awaited_once_with(
            restart=True, notice=MOD_UPDATE_NOTICE
        )
        assert agent.opened == agent.closed == 2
        host.systemd.start.assert_not_awaited()
        host.systemd.restart.assert_not_awaited()
        await host.start(0, wait=False)
        host.systemd.start.assert_awaited_once_with(host.unit(0))
    finally:
        release.set()
        updating.cancel()
        await asyncio.wait_for(asyncio.gather(updating, return_exceptions=True), 5)


async def test_failed_running_mod_update_releases_host_lock(
    host: Host, agent: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(service, "run_process", runner)
    agent.update_mods.side_effect = RuntimeError("download failed")
    with pytest.raises(RuntimeError, match="download failed"):
        await host.update_mods(0, restart=True)
    assert agent.opened == agent.closed == 1
    runner.assert_not_awaited()
    host.systemd.stop.assert_not_awaited()
    host.systemd.start.assert_not_awaited()
    await host.start(0, wait=False)
    host.systemd.start.assert_awaited_once_with(host.unit(0))


async def test_cancel_mod_update_waits_for_container_cleanup_and_releases_lock(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloading, cleaning, release = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    commands: list[tuple[str, ...]] = []

    async def run(*command: str, **_: object) -> int:
        commands.append(command)
        if command[1] == "run":
            downloading.set()
            await asyncio.Event().wait()
        else:
            cleaning.set()
            await release.wait()
        return 0

    monkeypatch.setattr(service, "run_process", run)
    updating = asyncio.create_task(host.update_mods(0))
    try:
        async with asyncio.timeout(5):
            await wait_for_event(downloading, updating)
            updating.cancel()
            await wait_for_event(cleaning, updating)
            assert not updating.done()
            with pytest.raises(RuntimeError, match="busy"):
                await host.start(0, wait=False)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await updating
        assert commands[1] == ("podman", "rm", "--force", "--ignore", commands[0][4])
        host.systemd.start.assert_not_awaited()
        await host.start(0, wait=False)
        host.systemd.start.assert_awaited_once_with(host.unit(0))
    finally:
        release.set()
        updating.cancel()
        await asyncio.wait_for(asyncio.gather(updating, return_exceptions=True), 5)


@pytest.mark.parametrize(("run_code", "cleanup_code"), [(0, 0), (1, 0), (0, 2)])
async def test_mod_update_uses_deployed_container_identity_and_always_removes_it(
    host: Host, monkeypatch: pytest.MonkeyPatch, run_code: int, cleanup_code: int
) -> None:
    definition = host.rooms.load(0).edit_many((
        ("/deployment/image", "localhost/custom-game:release"),
        ("/deployment/userns", "keep-id:uid=1000,gid=1000"),
        ("/deployment/volume_idmap", "uids=1000-2000-1;gids=1000-3000-1"),
        (
            "/deployment/environment",
            {"DST_SERVER_TELEMETRY_PROFILE": "critical", "CUSTOM": "a value = b"},
        ),
    ))
    await host.edit(definition)
    runner = AsyncMock(side_effect=[run_code, cleanup_code])
    monkeypatch.setattr(service, "run_process", runner)
    async with asyncio.timeout(5):
        if run_code or cleanup_code:
            message = (
                "MOD preparation container cleanup failed with exit code 2"
                if cleanup_code
                else "MOD preparation failed with exit code 1"
            )
            with pytest.raises(RuntimeError, match=message):
                await host.update_mods(0)
        else:
            await host.update_mods(0)
    command = runner.call_args_list[0].args
    name = command[4]
    assert command == (
        "podman",
        "run",
        "--rm",
        "--name",
        name,
        "--pull=never",
        "--userns",
        "keep-id:uid=1000,gid=1000",
        "--volume",
        f"{host.rooms.path(0)}:/cluster:idmap=uids=1000-2000-1;gids=1000-3000-1",
        "--env",
        "CUSTOM=a value = b",
        "--env",
        "DST_SERVER_CLUSTER_NAME=dst-000",
        "--env",
        "DST_SERVER_TELEMETRY_PROFILE=critical",
        "localhost/custom-game:release",
        "/app/.venv/bin/dst-server",
        "agent",
        "prepare",
    )
    assert runner.call_args_list[1].args == (
        "podman",
        "rm",
        "--force",
        "--ignore",
        name,
    )
    host.systemd.start.assert_not_awaited()
    host.systemd.restart.assert_not_awaited()


async def test_online_ban_uses_live_agent_and_never_overwrites_blacklist(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = host.rooms.path(0)
    (root / "blocklist.txt").write_text("new concurrent ban\n")
    host.systemd.states[host.unit(0)] = "active"
    players = SimpleNamespace(
        ban=AsyncMock(), blocklist=AsyncMock(return_value=("KU_abcdefgh",))
    )
    client = SimpleNamespace(shard=lambda _: SimpleNamespace(players=players))

    @asynccontextmanager
    async def connect(_: int) -> AsyncIterator[SimpleNamespace]:
        yield client

    monkeypatch.setattr(host, "connect", connect)
    assert await host.permission(0, "ban", "KU_abcdefgh") == ("KU_abcdefgh",)
    players.ban.assert_awaited_once_with("KU_abcdefgh")
    assert (root / "blocklist.txt").read_text() == "new concurrent ban\n"


async def test_offline_permission_write_and_automation_paths_are_literal(
    host: Host, tmp_path: Path
) -> None:
    assert await host.permission(0, "admin", "KU_abcdefgh") == ("KU_abcdefgh",)
    assert await host.permission(0, "admin", "KU_abcdefgh", remove=True) == ()
    host.cluster_root = tmp_path / "rooms %n $value with space 'and\"quotes"
    target = tmp_path / "systemd"
    written = await host.install_automation(unit_dir=target)
    assert len(written) == 3
    command = next(
        line.removeprefix("ExecStart=")
        for line in (target / "dst-room-schedule.service").read_text().splitlines()
        if line.startswith("ExecStart=")
    )
    arguments = shlex.split(command)
    assert arguments[arguments.index("--cluster-root") + 1] == str(
        host.cluster_root
    ).replace("%", "%%").replace("$", "$$")
    assert arguments[1:3] == ["-m", "dst_server"]
    assert arguments[-2:] == ["schedule", "run"]
    recycle = (target / "dst-room-recycle.service").read_text()
    assert " -m dst_server " in recycle
    assert recycle.rstrip().endswith("maintenance recycle")


async def test_failed_native_edit_does_not_create_desired_state_or_rewrite_on_start(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dst_server.configuration import files

    original = host.rooms.load(0)
    root = host.rooms.path(0)
    before = (root / "cluster.ini").read_bytes()
    write = files.atomic_write

    def fail(path: Path, content: str, mode: int) -> None:
        if path.name == "cluster.ini":
            msg = "injected native write failure"
            raise OSError(msg)
        write(path, content, mode)

    monkeypatch.setattr(files, "atomic_write", fail)
    with pytest.raises(OSError, match="native write failure"):
        await host.edit(original.edit("/cluster/settings/max_players", 10))
    assert host.rooms.load(0) == original
    await host.start(0, wait=False)
    assert (root / "cluster.ini").read_bytes() == before
    assert not (root / ".dst-room.json").exists()
    host.systemd.start.assert_awaited_once_with(host.unit(0))


@pytest.mark.parametrize(
    ("phase", "wait"),
    [
        (phase, wait)
        for phase in ("reload", "submit", "idle", "ready")
        for wait in (True, False)
        if wait or phase not in {"idle", "ready"}
    ],
)
async def test_transition_timeout_covers_the_whole_operation(
    host: Host, phase: str, wait: bool
) -> None:
    stalled = asyncio.Event()

    async def block(*_: object, **__: object) -> None:
        await stalled.wait()

    if phase == "reload":
        host.systemd.reload.side_effect = block
    elif phase == "submit":
        host.systemd.start.side_effect = block
    elif phase == "idle":
        host.systemd.wait_idle.side_effect = block
    else:
        host.wait_ready = block  # ty: ignore[invalid-assignment]
    async with asyncio.timeout(2):
        with pytest.raises(TimeoutError):
            await host.start(0, wait=wait, timeout=0.02)
    async with asyncio.timeout(1), service.room_lock(host.rooms.path(0)):
        pass


async def test_diagnostics_remain_available_when_native_configuration_is_damaged(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = host.units(0)
    (host.quadlet_dir / "dst-001-forest.container").write_text("broken other room")
    (host.rooms.path(0) / "forest/server.ini").write_text(
        '{"token": "private-token", "number":'
    )
    host.systemd.states[host.unit(0)] = "failed"
    queried: list[tuple[str, ...]] = []

    async def history(  # ruff: ignore[unused-async]
        names: tuple[str, ...], *_: object, **__: object
    ) -> JournalResult:
        queried.append(names)
        return JournalResult(
            records=(),
            next_cursor=None,
            has_more=False,
            diagnostics="journal warning",
            diagnostics_truncated=False,
        )

    monkeypatch.setattr(host.journal_logs, "query", history)
    result = await host.diagnose(0)
    assert result["active"] == "failed"
    assert result["configuration_error"] == "room configuration could not be loaded"
    assert "private-token" not in str(result)
    assert set(result["units"]) == set(units)
    assert queried == [("dst-000-pod.service", "dst-000-*.service")]
    assert result["logs"].diagnostics == "journal warning"


async def test_status_keeps_live_rpc_diagnostics_with_a_damaged_native_configuration(
    host: Host, agent: SimpleNamespace, ready_game: ClusterStatus
) -> None:
    (host.rooms.path(0) / "forest/server.ini").write_text("broken")
    agent.status.return_value = ready_game
    result = await host.status(0)
    assert result["game"] == ready_game
    assert result["configuration_error"] == "room configuration could not be loaded"
    assert agent.opened == agent.closed == 1


async def test_stopped_policy_edit_preserves_native_files(
    host: Host,
) -> None:
    original = host.rooms.load(0)
    root = host.rooms.path(0)
    game = root / "cluster.ini"
    game.write_text(game.read_text() + "\n; native formatting\n")
    unit = host.quadlet_dir / "dst-000-forest.container"
    unit.write_text(unit.read_text() + "\n# native formatting\n")
    before = {path: path.read_bytes() for path in (game, unit)}
    await host.edit(original.replace(recycle=False))
    assert not host.rooms.policy(0).recycle
    assert {path: path.read_bytes() for path in before} == before
    host.systemd.stop.assert_not_awaited()
    host.systemd.start.assert_not_awaited()


async def test_deployment_regenerates_managed_units_and_preserves_drop_ins(
    host: Host,
) -> None:
    from dst_server.deployment.application import QuadletApplication

    application = QuadletApplication.load(host.quadlet_dir, name="dst-000")
    application = application.replace(master=application.master.replace(nice=8))
    application.save(host.quadlet_dir)
    pod_path = host.quadlet_dir / "dst-000.pod"
    pod_path.write_text(pod_path.read_text() + "\n# keep native pod formatting\n")
    pod_content = pod_path.read_bytes()
    drop_in = host.quadlet_dir / "dst-.container.d" / "10-proxy.conf"
    drop_in.parent.mkdir()
    drop_in.write_text(
        "[Container]\nEnvironment=DST_SERVER_MOD_PROXY=http://proxy.invalid\n"
    )
    original = host.rooms.load(0)
    before = {path: path.read_bytes() for path in host.quadlet_dir.glob("*.container")}
    await host.edit(original.edit("/cluster/settings/max_players", 10))
    assert {path: path.read_bytes() for path in before} == before
    original = host.rooms.load(0)
    await host.edit(original.edit("/deployment/image", "localhost/custom:latest"))
    updated = QuadletApplication.load(host.quadlet_dir, name="dst-000")
    assert updated.master.nice == original.application(host.rooms.path(0)).master.nice
    assert updated.master.image == "localhost/custom:latest"
    assert pod_path.read_bytes() != pod_content
    assert (
        drop_in.read_text()
        == "[Container]\nEnvironment=DST_SERVER_MOD_PROXY=http://proxy.invalid\n"
    )


async def test_native_dynamic_lua_allows_start_stop_and_mod_preparation(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = host.rooms.path(0) / "forest/worldgenoverride.lua"
    native = "return require('my_world_configuration')\n"
    path.write_text(native)
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(service, "run_process", runner)
    await host.start(0, wait=False)
    assert (await host.status(0, game=False))["configuration_error"] is None
    await host.stop(0)
    await host.update_mods(0)
    assert path.read_text() == native
    assert runner.await_count == 2


async def test_mod_preparation_uses_native_quadlet_drop_ins(
    host: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    generator = Path("/usr/lib/systemd/system-generators/podman-system-generator")
    if not await asyncio.to_thread(generator.exists):
        pytest.skip("native Quadlet generator is unavailable")
    container_drop_in = host.quadlet_dir / "dst-.container.d/10-inputs.conf"
    container_drop_in.parent.mkdir()
    container_drop_in.write_text(
        "[Container]\nImage=localhost/native:release\n"
        'Environment="DST_SERVER_MOD_PROXY=http://proxy.invalid"\n'
        "Volume=\nVolume=/native/cluster:/cluster:ro\n"
    )
    pod_drop_in = host.quadlet_dir / "dst-000.pod.d/10-userns.conf"
    pod_drop_in.parent.mkdir()
    pod_drop_in.write_text("[Pod]\nUserNS=keep-id:uid=1000,gid=1000\n")
    generated = tmp_path / "generated"
    generated.mkdir()
    process = await asyncio.create_subprocess_exec(
        str(generator),
        str(generated),
        env={**os.environ, "QUADLET_UNIT_DIRS": str(host.quadlet_dir)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(process.communicate(), 5)
    assert process.returncode == 0, stderr.decode()

    async def native_command(unit: str, *, pre: bool = False) -> tuple[str, ...]:  # ruff: ignore[unused-async]
        prefix = "ExecStartPre=" if pre else "ExecStart="
        command = next(
            line.removeprefix(prefix)
            for line in (generated / unit).read_text().splitlines()
            if line.startswith(prefix)
        )
        return tuple(shlex.split(command))

    host.systemd.exec_start.side_effect = native_command
    runner = AsyncMock(return_value=0)
    monkeypatch.setattr(service, "run_process", runner)
    await host.update_mods(0)
    command = runner.call_args_list[0].args
    assert "DST_SERVER_MOD_PROXY=http://proxy.invalid" in command
    assert "localhost/native:release" in command
    assert "/native/cluster:/cluster:ro" in command
    assert "keep-id:uid=1000,gid=1000" in command
    assert "--pod" not in command
    assert "--replace" not in command
    assert command[-3:] == ("/app/.venv/bin/dst-server", "agent", "prepare")


async def test_stopped_edit_preserves_world_and_permission_files(host: Host) -> None:
    root = host.rooms.path(0)
    world = root / "forest/worldgenoverride.lua"
    written = 'return { override_enabled = true, preset = "SURVIVAL_TOGETHER" }\n'
    world.write_text(written)
    (root / "blocklist.txt").write_text("KU_newban\n")
    save = root / "forest/save/world"
    save.parent.mkdir(exist_ok=True)
    save.write_bytes(b"native saved world")
    original = host.rooms.load(0)
    updated = await host.edit(
        original.edit("/cluster/settings/max_players", 10),
    )
    assert updated.cluster.settings.max_players == 10
    assert updated == host.rooms.load(0)
    assert world.read_text() == written
    assert (root / "blocklist.txt").read_text() == "KU_newban\n"
    assert save.read_bytes() == b"native saved world"
    host.systemd.stop.assert_not_awaited()
    host.systemd.wait_idle.assert_not_awaited()
    host.systemd.start.assert_not_awaited()


async def test_manual_stop_pauses_and_start_resumes_without_countdown(
    host: Host,
) -> None:
    await host.stop(0, wait=False)
    assert read_control(host.rooms.path(0)).paused
    await host.start(0, wait=False)
    assert not read_control(host.rooms.path(0)).paused
    assert host.systemd.reset_failed.await_args_list == [
        call(unit) for unit in host.units(0)
    ]
    assert not (host.quadlet_dir / ".dst-operation.lock").exists()


async def test_restart_submits_one_native_systemd_transaction(
    host: Host, agent: SimpleNamespace
) -> None:
    assert not agent.status.return_value.busy
    await host.restart(0, wait=False)
    host.systemd.restart.assert_awaited_once_with(host.unit(0))
    host.systemd.stop.assert_not_awaited()
    host.systemd.start.assert_not_awaited()


async def test_automatic_start_never_clears_start_limit(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "effective_state", lambda *_: True)
    await host.start(0, automatic=True, wait=False)
    host.systemd.start.assert_awaited_once()
    host.systemd.reset_failed.assert_not_awaited()


async def test_status_reports_failed_master_even_when_pod_is_active(host: Host) -> None:
    host.systemd.states[host.unit(0)] = "active"
    host.systemd.states["dst-000-forest.service"] = "failed"
    result = await host.status(0, game=False)
    assert result["active"] == "failed"


async def test_explicit_start_recovers_failed_master_with_active_pod(
    host: Host,
) -> None:
    host.systemd.states[host.unit(0)] = "active"
    host.systemd.states["dst-000-forest.service"] = "failed"
    await host.start(0, wait=False)
    host.systemd.restart.assert_awaited_once_with(host.unit(0))
    host.systemd.start.assert_not_awaited()
    assert host.systemd.reset_failed.await_count == len(host.units(0))


async def test_explicit_restart_recovers_unreachable_controller(
    host: Host, agent: SimpleNamespace
) -> None:
    agent.status.side_effect = ConnectionError("controller unavailable")
    await host.restart(0, wait=False)
    host.systemd.restart.assert_awaited_once_with(host.unit(0))
