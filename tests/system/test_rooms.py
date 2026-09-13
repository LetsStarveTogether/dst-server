import asyncio
import os
import select
from contextlib import closing, suppress
from datetime import UTC, datetime
from typing import Literal

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
from dst_server.models.cluster import ClusterStatus, GameEventRecord, LogRecord
from dst_server.rooms import RoomStore
from dst_server.rpc import ClusterClient, Subscription, rpc_runtime
from tests.system.helpers import (
    GAME_EXECUTABLE,
    MASTER,
    OPERATION_TIMEOUT,
    SHARDS,
    STARTUP_TIMEOUT,
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
    RoomState,
    emit_player_event,
    netdata_player_fields,
    service_properties,
    verify_watchdog_notifications,
)

pytestmark = pytest.mark.system


def assert_recreated_shards(
    before: RoomState, after: RoomState, process_fds: tuple[int, ...]
) -> None:
    assert after.status.epoch != before.status.epoch
    assert after.status.phase == "running"
    assert not after.status.missing_shards
    assert {shard.name for shard in after.status.shards} == set(SHARDS)
    assert all(select.select([fd], [], [], 0)[0] for fd in process_fds)
    for shard in SHARDS:
        previous = shard_status(before.status, shard)
        current = shard_status(after.status, shard)
        assert current.ready
        assert current.agent_incarnation is not None
        assert current.agent_incarnation != previous.agent_incarnation
        assert current.game_attempt is not None
        assert current.game_attempt != previous.game_attempt
        assert after.containers[shard] != before.containers[shard]


async def verify_room_streams(
    client: ClusterClient,
    status: ClusterStatus,
    logs: Subscription[LogRecord],
    events: Subscription[GameEventRecord],
) -> None:
    for shard in SHARDS:
        marker = f"DST_RECOVERED_{ULID()}"
        await client.shard(shard).execute_json(
            "TheWorld:DoTaskInTime(0,function()"
            f"print({lua_string(marker)}) end);return true"
        )
        record = await next_matching(
            logs, lambda record, marker=marker: marker in record.line
        )
        assert record.shard == shard
        assert record.game_attempt == shard_status(status, shard).game_attempt
        await emit_player_event(client, events, shard)


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
        configuration = orjson.loads(pod)[0]
        assert configuration["SharedNamespaces"] == ["net"]
        bindings = configuration["InfraConfig"]["PortBindings"]
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


@pytest.mark.parametrize("fault", ["game", "container", "watchdog"])
@pytest.mark.parametrize("failed_shard", SHARDS)
async def test_quadlet_recovers_the_whole_room_after_failure(
    quadlet_system: QuadletSystem,
    failed_shard: str,
    fault: Literal["game", "container", "watchdog"],
) -> None:
    system = quadlet_system
    async with system.running() as client, system.track_processes() as process_fds:
        before = await system.state(client)
        failed_service = f"{system.container_name(failed_shard)}.service"
        failed_since = f"@{int(datetime.now(UTC).timestamp())}"
        if fault == "game":
            target = shard_status(before.status, failed_shard)
            assert target.pid is not None
            await run_command(
                "podman",
                "exec",
                system.container_name(failed_shard),
                "kill",
                "-KILL",
                str(target.pid),
            )
        else:
            await run_command(
                "podman",
                "kill",
                "--signal",
                "STOP" if fault == "watchdog" else "KILL",
                system.container_name(failed_shard),
            )
        replacement, _ = await wait_for_client(
            system.socket_path,
            lambda value: value.epoch != before.status.epoch and startup_phase(value),
        )
        with closing(replacement):
            with pytest.raises(DisconnectedError):
                await client.status()
            after = await system.state(replacement)
            assert_recreated_shards(before, after, process_fds)
            assert after.pod_id == before.pod_id
            assert after.restarts == before.restarts | {
                system.master_service: before.restarts[system.master_service] + 1
            }
            assert all(
                after.restarts[service] == 0 for service in system.secondary_services
            )
            await wait_for_game_shards(replacement, len(SHARDS))
            async with (
                await replacement.subscribe("logs") as logs,
                await replacement.subscribe("events") as events,
            ):
                await verify_room_streams(replacement, after.status, logs, events)
        if fault == "watchdog":
            _, journal = await run_command(
                "journalctl",
                "--unit",
                failed_service,
                "--since",
                failed_since,
                "--output=cat",
                "--no-pager",
            )
            assert "Watchdog timeout" in journal
            assert "left-over" not in journal
            assert "remains running" not in journal
            assert "stop-post' timed out" not in journal


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
    async with (
        system.running() as client,
        await client.subscribe("logs") as logs,
        await client.subscribe("events") as events,
    ):
        before = await system.state(client)
        await verify_room_streams(client, before.status, logs, events)
        await client.update_mods(restart=True, notice=None, timeout=STARTUP_TIMEOUT)
        after = await system.state(client)
        assert after.status.phase == "running"
        assert after.status.epoch == before.status.epoch
        assert not after.status.mod_update.pending
        assert after.pod_id == before.pod_id
        assert after.containers == before.containers
        assert after.restarts == before.restarts
        for shard in SHARDS:
            previous, updated = (
                shard_status(before.status, shard),
                shard_status(after.status, shard),
            )
            assert updated.ready
            assert updated.game_attempt != previous.game_attempt
            assert updated.agent_incarnation == previous.agent_incarnation
        await wait_for_game_shards(client, len(SHARDS))
        await verify_room_streams(client, after.status, logs, events)


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


async def test_quadlet_pod_restart_recreates_all_shards(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with system.running() as client, system.track_processes() as process_fds:
        before = await system.state(client)
        await run_command(
            "systemctl", "restart", system.pod_service, seconds=STARTUP_TIMEOUT
        )
        replacement, _ = await wait_for_client(
            system.socket_path,
            lambda value: value.epoch != before.status.epoch and startup_phase(value),
        )
        with closing(replacement):
            with pytest.raises(DisconnectedError):
                await client.status()
            after = await system.state(replacement)
            assert_recreated_shards(before, after, process_fds)
            assert after.pod_id != before.pod_id
            assert after.restarts == before.restarts
            await wait_for_game_shards(replacement, len(SHARDS))
            async with (
                await replacement.subscribe("logs") as logs,
                await replacement.subscribe("events") as events,
            ):
                await verify_room_streams(replacement, after.status, logs, events)


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
