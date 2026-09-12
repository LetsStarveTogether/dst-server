import asyncio
import os
import select
from contextlib import closing, suppress
from datetime import UTC, datetime

import orjson
import pytest
from ulid import ULID

from dst_server.configuration.models import (
    ClusterConfig,
)
from dst_server.configuration.world import ForestOverrides
from dst_server.errors import (
    DisconnectedError,
)
from dst_server.lua_codec import lua_string
from dst_server.rooms import RoomStore
from dst_server.rpc import rpc_runtime
from tests.system.helpers import (
    GAME_EXECUTABLE,
    MASTER,
    OPERATION_TIMEOUT,
    SHARDS,
    STARTUP_TIMEOUT,
    SYSTEM_MARKS,
    next_matching,
    run_command,
    shard_status,
    startup_phase,
    wait_for_client,
    wait_for_game_shards,
    wait_for_status,
    wait_for_stopping,
)
from tests.system.quadlet import (
    QuadletSystem,
    emit_player_event,
    netdata_player_fields,
    service_properties,
    verify_watchdog_notifications,
)

pytestmark = SYSTEM_MARKS


async def test_quadlet_waits_for_all_agents_and_keeps_watchdog_alive(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    await run_command("systemctl", "mask", "--runtime", *system.secondary_services)
    await run_command(
        "systemctl", "start", system.master_service, seconds=STARTUP_TIMEOUT
    )
    async with rpc_runtime():
        observer, waiting = await wait_for_client(
            system.socket_path, lambda status: startup_phase(status, "waitingAgents")
        )
        with closing(observer):
            assert waiting.missing_shards == ("cave",)
            _, processes = await run_command(
                "podman", "top", system.container_name(MASTER), "args"
            )
            assert GAME_EXECUTABLE not in processes
            properties = await service_properties(
                system.master_service, "WatchdogTimestampMonotonic"
            )
            assert int(properties["WatchdogTimestampMonotonic"]) > 0
            assert (await observer.status()).phase == "waitingAgents"
            await run_command(
                "systemctl", "unmask", "--runtime", *system.secondary_services
            )
            await run_command("systemctl", "daemon-reload")
            await run_command(
                "systemctl",
                "start",
                *system.secondary_services,
                seconds=STARTUP_TIMEOUT,
            )
            status = await wait_for_status(observer, startup_phase)
            assert not status.missing_shards
            assert all(shard.ready for shard in status.shards)
            await wait_for_game_shards(observer, len(SHARDS))
            await verify_watchdog_notifications(system.master_service)


async def test_quadlet_publishes_each_shard_port(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with system.running() as client:
        status = await client.status()
        assert all(shard.telemetry_profile == "history" for shard in status.shards)
        assert {shard.external_port for shard in status.shards} == {
            mapping.host for mapping in system.application.pod.publish_ports[::2]
        }
        for shard in status.shards:
            _, processes = await run_command(
                "podman", "top", system.container_name(shard.name), "args"
            )
            games = [line for line in processes.splitlines() if GAME_EXECUTABLE in line]
            assert len(games) == 1
            assert f"-external_port {shard.external_port}" in games[0]
        _, pod = await run_command("podman", "pod", "inspect", system.pod_name)
        bindings = orjson.loads(pod)[0]["InfraConfig"]["PortBindings"]
        assert {
            (
                int(value["HostPort"]),
                int(container.rsplit("/", 1)[0]),
                container.rsplit("/", 1)[1],
            )
            for container, values in bindings.items()
            for value in values
        } == {
            (mapping.host, mapping.container, mapping.protocol)
            for mapping in system.application.pod.publish_ports
        }


async def test_quadlet_native_configuration_preserves_game_writes_and_stopped_edits(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    rooms = RoomStore(system.root)
    number = int(system.cluster_dir.name)
    assert rooms.load(number).cluster.settings.max_players == 9
    async with system.running() as client:
        configuration = await client.read_configuration()
        assert isinstance(configuration, ClusterConfig)
        assert configuration.settings.max_players == 9
        master = client.shard(MASTER)
        assert (await master.room()).is_dedicated is True
        assert await master.execute_json(
            "return {mode=TheNet:GetDefaultGameMode(),"
            "players=TheNet:GetDefaultMaxPlayers()}"
        ) == {"mode": "survival", "players": 9}
        sessions = {
            shard: (await client.shard(shard).runtime()).session_id for shard in SHARDS
        }
        await master.execute_json(
            "TheWorld.topology.overrides.day='onlynight';return true"
        )
        saved = await client.save()
        assert {name for name, _ in saved.shards} == set(SHARDS)
        world = rooms.load(number).cluster.shards[MASTER].world
        assert world is not None
        assert isinstance(world.overrides, ForestOverrides)
        assert world.overrides.day == "onlynight"

    await run_command("systemctl", "stop", *system.services)
    rooms.save(rooms.load(number).edit("/cluster/settings/max_players", 8))
    assert ClusterConfig.load(system.cluster_dir).settings.max_players == 8
    async with system.running() as client:
        assert (
            await client.shard(MASTER).execute_json(
                "return TheNet:GetDefaultMaxPlayers()"
            )
            == 8
        )
        assert {
            shard: (await client.shard(shard).runtime()).session_id for shard in SHARDS
        } == sessions
        reread = await client.read_configuration()
        assert isinstance(reread, ClusterConfig)
        assert reread.settings.max_players == 8


async def test_quadlet_delivers_logs_to_rpc_and_journal_and_player_events(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with (
        system.running() as client,
        await client.subscribe("logs") as logs,
        await client.subscribe("events") as events,
    ):
        marker = f"DST_RPC_{ULID()}"
        await client.shard(MASTER).execute_json(
            "TheWorld:DoTaskInTime(0,function()"
            f"print({lua_string(marker)}) end);return true"
        )
        await next_matching(logs, lambda record: marker in record.line)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while True:
                _, journal = await run_command(
                    "journalctl",
                    "--unit",
                    system.master_service,
                    "--grep",
                    marker,
                    "--no-pager",
                    "--output=cat",
                    check=False,
                )
                if marker in journal:
                    break
                await asyncio.sleep(0.5)
        userid = await emit_player_event(client, events, MASTER)
        if os.environ.get("DST_SERVER_NETDATA_TEST") == "1":
            fields = await netdata_player_fields(userid)
            assert fields["body.player.userid"] == userid
            assert fields["attributes.dst.cluster.name"] == system.prefix
            assert fields["attributes.dst.shard.name"] == MASTER
        assert (await client.status()).phase == "running"


@pytest.mark.parametrize("failed_shard", SHARDS)
async def test_one_game_crash_restarts_the_whole_room(
    quadlet_system: QuadletSystem, failed_shard: str
) -> None:
    system = quadlet_system
    process_fds: list[int] = []
    try:
        async with system.running() as client:
            before = await client.status()
            container_ids = {
                shard: await system.container_id(shard) for shard in SHARDS
            }
            pod_id = await system.pod_id()
            restarts = await service_properties(system.master_service, "NRestarts")
            for shard in SHARDS:
                _, processes = await run_command(
                    "podman", "top", system.container_name(shard), "hpid"
                )
                process_fds.extend(
                    os.pidfd_open(int(pid.strip()))
                    for pid in processes.splitlines()[1:]
                )
            target = shard_status(before, failed_shard)
            assert target.pid is not None
            await run_command(
                "podman",
                "exec",
                system.container_name(failed_shard),
                "kill",
                "-KILL",
                str(target.pid),
            )
            replacement, recovered = await wait_for_client(
                system.socket_path,
                lambda value: value.epoch != before.epoch and value.phase == "running",
            )
            with closing(replacement):
                with pytest.raises(DisconnectedError):
                    await client.status()
                assert all(select.select([fd], [], [], 0)[0] for fd in process_fds)
                assert await system.pod_id() == pod_id
                for shard in SHARDS:
                    previous, current = (
                        shard_status(before, shard),
                        shard_status(recovered, shard),
                    )
                    assert current.ready
                    assert current.agent_incarnation != previous.agent_incarnation
                    assert current.game_attempt != previous.game_attempt
                    assert await system.container_id(shard) != container_ids[shard]
                assert await service_properties(system.master_service, "NRestarts") == {
                    "NRestarts": str(int(restarts["NRestarts"]) + 1)
                }
                for service in system.secondary_services:
                    assert await service_properties(service, "NRestarts") == {
                        "NRestarts": "0"
                    }
                await wait_for_game_shards(replacement, len(SHARDS))
                async with await replacement.subscribe("events") as events:
                    await emit_player_event(replacement, events, failed_shard)
    finally:
        for fd in process_fds:
            os.close(fd)


async def test_planned_mod_update_keeps_management_and_restarts_both_games(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    # The test room has no Workshop dependencies; this checks the real lifecycle.
    assert all(
        not shard.mods.entries
        for shard in RoomStore(system.root)
        .load(int(system.cluster_dir.name))
        .cluster.shards.values()
    )
    async with system.running() as client:
        before = await client.status()
        containers = {shard: await system.container_id(shard) for shard in SHARDS}
        restarts = await service_properties(system.master_service, "NRestarts")
        await client.update_mods(restart=True, notice=None)
        current = await client.status()
        assert current.phase == "running"
        assert current.epoch == before.epoch
        assert not current.mod_update.pending
        assert await service_properties(system.master_service, "NRestarts") == restarts
        for shard in SHARDS:
            previous, updated = (
                shard_status(before, shard),
                shard_status(current, shard),
            )
            assert updated.ready
            assert updated.game_attempt != previous.game_attempt
            assert updated.agent_incarnation == previous.agent_incarnation
            assert await system.container_id(shard) == containers[shard]
        await wait_for_game_shards(client, len(SHARDS))


async def test_explicit_game_stop_stays_stopped_with_management_available(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with system.running() as client:
        before = await client.status()
        containers = {shard: await system.container_id(shard) for shard in SHARDS}
        restarts = await service_properties(system.master_service, "NRestarts")
        await client.stop(notice=None)
        restart_delay = system.application.master.restart_sec
        assert restart_delay is not None
        await asyncio.sleep(restart_delay + 1)
        stopped = await client.status()
        assert stopped.epoch == before.epoch
        assert stopped.phase == "stopped"
        assert all(
            shard.phase == "stopped" and shard.pid is None for shard in stopped.shards
        )
        assert await service_properties(system.master_service, "NRestarts") == restarts
        assert {
            shard: await system.container_id(shard) for shard in SHARDS
        } == containers


async def test_quadlet_watchdog_reaps_frozen_agent_and_recovers_the_room(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    process_fds: list[int] = []
    async with system.running() as client:
        before = await client.status()
        container_ids = {shard: await system.container_id(shard) for shard in SHARDS}
        pod_id = await system.pod_id()
        cave_service = f"{system.container_name('cave')}.service"
        properties = await service_properties(system.master_service, "NRestarts")
        restarts = int(properties["NRestarts"])
        _, processes = await run_command(
            "podman", "top", system.container_name("cave"), "hpid"
        )
        try:
            process_fds.extend(
                os.pidfd_open(int(process.strip()))
                for process in processes.splitlines()[1:]
            )
            assert len(process_fds) >= 2
            frozen_since = f"@{int(datetime.now(UTC).timestamp())}"
            await run_command(
                "podman", "kill", "--signal", "STOP", system.container_name("cave")
            )
            replacement, current = await wait_for_client(
                system.socket_path,
                lambda value: value.epoch != before.epoch and value.phase == "running",
            )
            with closing(replacement):
                assert all(select.select([fd], [], [], 0)[0] for fd in process_fds)
                for shard in SHARDS:
                    assert await system.container_id(shard) != container_ids[shard]
                    assert (
                        shard_status(current, shard).agent_incarnation
                        != shard_status(before, shard).agent_incarnation
                    )
                assert await system.pod_id() == pod_id
                properties = await service_properties(
                    system.master_service, "NRestarts"
                )
                assert int(properties["NRestarts"]) == restarts + 1
                assert await service_properties(cave_service, "NRestarts") == {
                    "NRestarts": "0"
                }
                await wait_for_game_shards(replacement, len(SHARDS))
                async with await replacement.subscribe("events") as events:
                    await emit_player_event(replacement, events, "cave")
        finally:
            for fd in process_fds:
                os.close(fd)
        _, journal = await run_command(
            "journalctl",
            "--unit",
            cave_service,
            "--since",
            frozen_since,
            "--output=cat",
            "--no-pager",
        )
        assert "Watchdog timeout" in journal
        assert "left-over" not in journal
        assert "remains running" not in journal
        assert "stop-post' timed out" not in journal


async def test_quadlet_master_container_crash_recreates_shards_in_existing_pod(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with system.running() as client:
        before = await client.status()
        container_ids = {shard: await system.container_id(shard) for shard in SHARDS}
        pod_id = await system.pod_id()
        await run_command(
            "podman", "kill", "--signal", "KILL", system.container_name(MASTER)
        )
        async with asyncio.timeout(STARTUP_TIMEOUT):
            while True:
                try:
                    await client.status()
                except DisconnectedError:
                    break
                await asyncio.sleep(0.5)
        replacement, current = await wait_for_client(
            system.socket_path, lambda value: value.phase == "running"
        )
        with closing(replacement):
            await wait_for_game_shards(replacement, len(SHARDS))
            assert current.epoch != before.epoch
            assert await system.pod_id() == pod_id
            for shard in SHARDS:
                assert await system.container_id(shard) != container_ids[shard]


async def test_quadlet_pod_restart_recreates_all_shards(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with system.running() as client:
        before = await client.status()
        pod_id = await system.pod_id()
        container_ids = {shard: await system.container_id(shard) for shard in SHARDS}
        await run_command(
            "systemctl", "restart", system.pod_service, seconds=STARTUP_TIMEOUT
        )
        replacement, _ = await wait_for_client(
            system.socket_path,
            lambda value: value.epoch != before.epoch and value.phase == "running",
        )
        with closing(replacement):
            assert await system.pod_id() != pod_id
            for shard in SHARDS:
                assert await system.container_id(shard) != container_ids[shard]
            await wait_for_game_shards(replacement, len(SHARDS))


async def test_quadlet_pod_stop_delivers_stopping_and_reaps_all_containers(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with system.running() as client:
        lifecycle = await client.subscribe("lifecycle")
        stopping = asyncio.create_task(wait_for_stopping(lifecycle, frozenset(SHARDS)))
        try:
            await run_command(
                "systemctl", "stop", system.pod_service, seconds=OPERATION_TIMEOUT
            )
            assert await stopping == set(SHARDS)
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
            with suppress(DisconnectedError):
                await lifecycle.close()
        for service in (system.master_service, *system.secondary_services):
            assert await service_properties(service, "Result", "ExecMainStatus") == {
                "Result": "success",
                "ExecMainStatus": "0",
            }
        for shard in SHARDS:
            code, _ = await run_command(
                "podman",
                "container",
                "exists",
                system.container_name(shard),
                check=False,
            )
            assert code == 1
        code, _ = await run_command(
            "podman", "pod", "exists", system.pod_name, check=False
        )
        assert code == 1
