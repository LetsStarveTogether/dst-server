from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sys
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from pathlib import Path, PurePosixPath

import pytest
from pydantic import SecretStr
from ulid import ULID

sys.path.insert(0, str(Path(__file__).parents[1]))

from dst_server.cluster import (
    ClusterConfig,
    ClusterSettings,
    ForestOverrides,
    ModOverride,
    ModOverrides,
    ModSettings,
    ShardConfig,
    ShardSettings,
    WorldgenOverride,
)
from dst_server.cluster.quadlet import (
    QuadletApplication,
    RoomPortAllocation,
    VolumeMount,
)
from dst_server.events import player, world
from dst_server.game.rpc import lua_string
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.lifecycle import read_line
from dst_server.runtime.server import SUBPROCESS_STREAM_LIMIT
from dst_server.telemetry import TelemetrySettings
from scripts.generate_rooms import NETDATA_ENVIRONMENT, build

IMAGE = os.environ.get("DST_SERVER_IMAGE", "")

pytestmark = [
    pytest.mark.system,
    pytest.mark.skipif(
        os.environ.get("DST_SERVER_PODMAN_TEST") != "1",
        reason="run `just test-system IMAGE` to test a local image",
    ),
    pytest.mark.skipif(
        not IMAGE,
        reason="set DST_SERVER_IMAGE to the exact local image ID or tag",
    ),
]

GAME_EXECUTABLE = "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"
STARTUP_TIMEOUT = 300
OPERATION_TIMEOUT = 60
CLEANUP_TIMEOUT = 30
QUADLET_PREFIX = "dst-sdk-pod-20260823"
QUADLET_ROOT = Path("/tmp/dst-sdk-pod-20260823")  # ruff: ignore[hardcoded-temp-file]
SYSTEM_QUADLET_ROOT = Path("/run/containers/systemd")
QUADLET_GENERATOR = Path("/usr/lib/systemd/system-generators/podman-system-generator")


async def netdata_player_fields(userid: str) -> dict[str, str]:
    async with asyncio.timeout(OPERATION_TIMEOUT):
        while True:
            query = await asyncio.create_subprocess_exec(
                "/usr/lib/netdata/plugins.d/otel-plugin",
                "logs",
                "--stock-config",
                "/usr/lib/netdata/conf.d/otel.yaml",
                "--config",
                "/etc/netdata/otel.yaml",
                "--since",
                "-5m",
                "--name",
                "dst-server",
                "--query",
                userid,
                "--limit",
                "20",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await query.communicate()
            diagnostic = stderr.decode(errors="replace")
            assert query.returncode == 0, diagnostic
            assert "WARN" not in diagnostic.upper(), diagnostic
            assert "ERROR" not in diagnostic.upper(), diagnostic
            for line in stdout.splitlines():
                fields = dict(json.loads(line)["fields"])
                if fields.get("event_name") == "dst.player.shard_entered":
                    return {str(key): str(value) for key, value in fields.items()}
            await asyncio.sleep(0.5)


def write_cluster(
    root: Path,
    *,
    caves: bool = False,
    tick_rate: int | None = None,
    verify_configuration: bool = False,
) -> Path:
    cluster = root / "cluster"
    settings = ClusterSettings(
        cluster_name=str(ULID()),
        offline_cluster=True,
        lan_only_cluster=True,
        master_ip="127.0.0.1" if caves else None,
        master_port=10889 if caves else 10888,
        cluster_key=SecretStr(str(ULID())) if caves else None,
    )
    if tick_rate is not None:
        settings = settings.replace(tick_rate=tick_rate)
    master = ShardConfig(
        settings=ShardSettings(
            is_master=True,
            id=1,
            server_port=10999,
            master_server_port=27018,
        ),
        world=(
            WorldgenOverride(overrides=ForestOverrides(day="onlyday"))
            if verify_configuration
            else WorldgenOverride.forest()
        ),
        mods=(
            ModOverrides(
                client_mods_disabled=False,
                entries={
                    "dst_config_verify": ModOverride(
                        configuration_options={"answer": 42},
                    )
                },
            )
            if verify_configuration
            else ModOverrides()
        ),
    )
    shards = {"forest": master}
    if caves:
        shards["cave"] = ShardConfig(
            settings=ShardSettings(
                is_master=False,
                name="Caves",
                id=2,
                server_port=11000,
                master_server_port=27019,
            ),
            world=WorldgenOverride.cave(),
        )
    ClusterConfig(
        settings=settings,
        shards=shards,
        whitelist="\nKU_SDK00001\n\n" if verify_configuration else "",
        blocklist="\nKU_SDK00003\n\n" if verify_configuration else "",
        mod_settings=(
            ModSettings(force_enabled=frozenset({"dst_config_verify"}))
            if verify_configuration
            else ModSettings()
        ),
    ).save(cluster)
    if verify_configuration:
        local_mod = cluster / "mods" / "dst_config_verify"
        local_mod.mkdir()
        (local_mod / "modinfo.lua").write_text(
            'name = "SDK configuration verification"\n'
            'description = "Local system-test mod"\n'
            'author = "dst-server"\n'
            'version = "1"\n'
            "api_version = 10\n"
            "dst_compatible = true\n"
            "server_only_mod = true\n"
            "all_clients_require_mod = false\n"
            "client_only_mod = false\n"
            "configuration_options = {\n"
            "    {\n"
            '        name = "answer",\n'
            '        label = "Answer",\n'
            '        options = { { description = "42", data = 42 } },\n'
            "        default = 0,\n"
            "    },\n"
            "}\n",
            encoding="utf-8",
        )
        (local_mod / "modmain.lua").write_text(
            'AddPrefabPostInit("world", function(world)\n'
            '    world.dst_config_verify = GetModConfigData("answer")\n'
            "end)\n",
            encoding="utf-8",
        )
    return cluster


async def kill_and_reap(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    async with asyncio.timeout(CLEANUP_TIMEOUT):
        await process.wait()


async def communicate(
    process: asyncio.subprocess.Process,
    seconds: float,
) -> bytes:
    try:
        async with asyncio.timeout(seconds):
            output, _ = await process.communicate()
    except BaseException:
        await kill_and_reap(process)
        raise
    return output or b""


async def remove_container(name: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "podman",
        "rm",
        "--force",
        "--time",
        "0",
        "--ignore",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = await communicate(process, CLEANUP_TIMEOUT)
    assert process.returncode == 0, output.decode(errors="replace")


@pytest.fixture(autouse=True)
async def image_matches_expected_build() -> None:
    expected_revision = os.environ.get("DST_SERVER_EXPECTED_REVISION")
    expected_version = os.environ.get("DST_SERVER_EXPECTED_VERSION")
    if expected_revision is None and expected_version is None:
        return

    process = await asyncio.create_subprocess_exec(
        "podman",
        "image",
        "inspect",
        "--format",
        (
            '{{ index .Labels "org.opencontainers.image.revision" }}|'
            '{{ index .Labels "org.opencontainers.image.version" }}'
        ),
        IMAGE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = await communicate(process, CLEANUP_TIMEOUT)
    assert process.returncode == 0, output.decode(errors="replace")
    revision, version = output.decode().strip().split("|", maxsplit=1)
    if expected_revision is not None:
        assert revision == expected_revision
    if expected_version is not None:
        assert version == expected_version


@pytest.fixture
async def container_name() -> AsyncIterator[str]:
    name = f"dst-server-system-{ULID()}"
    yield name
    await remove_container(name)


def make_server(
    root: Path,
    cluster: Path,
    container_name: str,
    lua_directory: Path | None = None,
    *,
    log_handler: Callable[[str], None] | None = None,
) -> Server:
    wrapper = root / "podman-dst-server"
    command = [
        "podman",
        "run",
        "--pull=never",
        "--rm",
        "--name",
        container_name,
        "--preserve-fds=3",
        "--network",
        "none",
        "--workdir",
        "/install/bin64",
        "--volume",
        f"{cluster}:/cluster:Z",
        "--volume",
        f"{cluster / 'mods'}:/install/mods:Z",
    ]
    container_lua = Path("/app/src/dst_server/lua")
    if lua_directory is not None:
        command.extend(("--volume", f"{lua_directory}:/dst-server-lua:ro,Z"))
        container_lua = Path("/dst-server-lua")
    command.extend([
        "--entrypoint",
        GAME_EXECUTABLE,
        IMAGE,
    ])
    wrapper.write_text(
        "#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return Server(
        ServerConfig(
            shard="forest",
            executable=wrapper,
            persistent_storage_root=Path("/"),
            conf_dir="/",
            cluster="cluster",
            ugc_directory=Path("/cluster/mods/ugc"),
            extra_args=("-skip_update_server_mods", "-offline"),
            lua_directory=container_lua,
            telemetry=TelemetrySettings(profile="history"),
            monitor_parent_process=False,
        ),
        log_handler=log_handler,
    )


async def reap_server(server: Server) -> None:
    if server.child is None or server.closed:
        return
    async with asyncio.timeout(CLEANUP_TIMEOUT):
        if server.returncode is None:
            await server.kill()
        else:
            await server.wait()


async def test_image_entrypoint_boots_and_handles_sigterm(
    tmp_path: Path,
    container_name: str,
) -> None:
    cluster = write_cluster(tmp_path, caves=True)
    process = await asyncio.create_subprocess_exec(
        "podman",
        "run",
        "--pull=never",
        "--rm",
        "--name",
        container_name,
        "--network",
        "none",
        "--volume",
        f"{cluster}:/cluster:Z",
        IMAGE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout = process.stdout
    assert stdout is not None
    output: deque[str] = deque(maxlen=200)
    ready = asyncio.Event()

    async def read_logs() -> None:
        while (line := await read_line(stdout)) is not None:
            value = line.decode(errors="replace").rstrip()
            output.append(value)
            if "All DST shards are ready." in value:
                ready.set()

    log_task = asyncio.create_task(read_logs())
    exit_task = asyncio.create_task(process.wait())
    ready_task = asyncio.create_task(ready.wait())
    try:  # ruff:ignore[too-many-statements-in-try-clause]
        async with asyncio.timeout(STARTUP_TIMEOUT):
            done, _ = await asyncio.wait(
                (ready_task, exit_task, log_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
        if log_task in done:
            await log_task
        assert ready_task in done, "\n".join(output)
        assert exit_task not in done, "\n".join(output)

        stop = await asyncio.create_subprocess_exec(
            "podman",
            "stop",
            "--time",
            "40",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stop_output = await communicate(stop, OPERATION_TIMEOUT)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            returncode = await exit_task
            await log_task
        assert stop.returncode == 0, stop_output.decode(errors="replace")
        assert returncode == 0, "\n".join(output)
    except BaseException as error:
        error.add_note("recent container logs:\n" + "\n".join(output))
        raise
    finally:
        for task in (ready_task, exit_task, log_task):
            task.cancel()
        await kill_and_reap(process)
        await asyncio.gather(ready_task, exit_task, log_task, return_exceptions=True)


async def test_sdk_real_game_contract_and_failure_isolation(  # ruff:ignore[too-many-locals,too-many-statements]
    tmp_path: Path,
    container_name: str,
) -> None:
    cluster = write_cluster(tmp_path, verify_configuration=True)
    logs: deque[str] = deque(maxlen=200)
    handler_failure = str(ULID())
    recovered_log = str(ULID())
    handler_failed = False
    partial_override_loaded = False
    recovered = asyncio.Event()

    def capture_log(line: str) -> None:
        nonlocal handler_failed, partial_override_loaded
        logs.append(line)
        if "Merging override file into savedata." in line:
            partial_override_loaded = True
        if handler_failure in line and not handler_failed:
            handler_failed = True
            msg = "injected log handler failure"
            raise RuntimeError(msg)
        if recovered_log in line:
            recovered.set()

    server = make_server(
        tmp_path,
        cluster,
        container_name,
        ServerConfig(shard="forest").lua_directory,
        log_handler=capture_log,
    )
    try:  # ruff:ignore[too-many-statements-in-try-clause]
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            assert server.driver_health.telemetry_status == "active"
            assert partial_override_loaded
            assert (await server.game.world.room()).is_dedicated is True
            assert await server.game.world.execute(
                "return {"
                "day=TheWorld.topology.overrides.day,"
                "mod_answer=TheWorld.dst_config_verify,"
                "game_mode=TheNet:GetDefaultGameMode(),"
                "max_players=TheNet:GetDefaultMaxPlayers(),"
                "pvp=TheNet:GetDefaultPvpSetting(),"
                "vote_enabled=TheNet:GetDefaultVoteEnabled(),"
                "autosaver_enabled=TheNet:GetAutosaverEnabled(),"
                "encode_user_path=TheNet:GetDefaultEncodeUserPath(),"
                "paused=TheNet:IsServerPaused(true),"
                "client_mods_disabled=KnownModIndex:AreClientModsDisabled(),"
                'whitelisted=TheNet:IsWhiteListed("KU_SDK00001")'
                "}"
            ) == {
                "day": "onlyday",
                "mod_answer": 42,
                "game_mode": "survival",
                "max_players": 16,
                "pvp": False,
                "vote_enabled": True,
                "autosaver_enabled": True,
                "encode_user_path": True,
                "paused": False,
                "client_mods_disabled": False,
                "whitelisted": True,
            }
            assert (await server.game.world.state()).cycles >= 0
            runtime = await server.game.world.runtime()
            assert runtime.session_id == server.session_id
            assert isinstance(await server.game.world.mods(), tuple)
            shards = await server.game.world.shards()
            assert any(shard.is_current and shard.ready for shard in shards)
            assert await server.game.players.list() == ()

            permission_userid = "KU_SDK00002"
            permissions = server.game.players
            try:
                assert await permissions.is_whitelisted("KU_SDK00001")
                assert await permissions.is_blocked("KU_SDK00003")
                assert "KU_SDK00003" in await permissions.blocklist()
                await permissions.unwhitelist(permission_userid)
                await permissions.unban(permission_userid)
                assert not await permissions.is_whitelisted(permission_userid)
                assert await permissions.whitelist(permission_userid)
                assert await permissions.is_whitelisted(permission_userid)
                assert await permissions.unwhitelist(permission_userid)
                assert not await permissions.is_whitelisted(permission_userid)

                await permissions.ban(permission_userid)
                assert await permissions.is_blocked(permission_userid)
                assert permission_userid in await permissions.blocklist()
                assert await permissions.unban(permission_userid)
                assert not await permissions.is_blocked(permission_userid)
                assert permission_userid not in await permissions.blocklist()
            finally:
                await permissions.unwhitelist(permission_userid)
                await permissions.unban(permission_userid)
            assert not await permissions.is_whitelisted(permission_userid)
            assert not await permissions.is_blocked(permission_userid)
            assert await permissions.is_whitelisted("KU_SDK00001")

            with pytest.raises(RuntimeError, match="injected SDK request failure"):
                await server.game.world.execute(
                    'error("injected SDK request failure", 0)'
                )
            assert (await server.game.world.room()).is_dedicated is True

            await server.game.world.execute(
                "TheWorld:DoTaskInTime(0,function()"
                f"print({lua_string(handler_failure)});"
                f'print(string.rep("x",{SUBPROCESS_STREAM_LIMIT + 1}));'
                f"print({lua_string(recovered_log)}) end);return true"
            )
            await recovered.wait()
            assert handler_failed

            errors_before = (await server.game.get_health()).errors
            fault = await server.game.world.execute(
                "local telemetry=require('dst_server.telemetry');"
                "local original=telemetry.emit;local sentinel=false;"
                "TheWorld:ListenForEvent('entity_death',function() sentinel=true end);"
                "telemetry.emit=function() error('injected telemetry failure',0) end;"
                "local rabbit=SpawnPrefab('rabbit');rabbit:AddTag('epic');"
                "local ok=pcall(function() TheWorld:PushEvent('entity_death',"
                "{inst=rabbit,corpsing=false}) end);telemetry.emit=original;"
                "local alive=rabbit:IsValid();rabbit:Remove();"
                "return {event_ok=ok,sentinel=sentinel,victim_alive=alive}"
            )
            assert fault == {
                "event_ok": True,
                "sentinel": True,
                "victim_alive": True,
            }
            assert (await server.game.get_health()).errors > errors_before

            assert await server.game.world.execute(
                "local rabbit=SpawnPrefab('rabbit');rabbit:AddTag('epic');"
                "TheWorld:PushEvent('entity_death',{inst=rabbit,corpsing=false});"
                "rabbit:Remove();return true"
            )
            userid = str(ULID())
            assert await server.game.world.execute(
                "local player=SpawnPrefab('wilson');"
                f"player.userid={lua_string(userid)};"
                "TheWorld:PushEvent('ms_playerjoined',player);"
                "player:PushEvent('startstarving');player:Remove();return true"
            )
            expected = {
                player.ConditionChangedEvent,
                player.ShardEnteredEvent,
                world.EntityDeathEvent,
            }
            while expected:
                observed = await server.read_game_event()
                assert observed is not None
                if isinstance(observed.record, world.EntityDeathEvent):
                    assert observed.record.data.victim.prefab == "rabbit"
                if isinstance(
                    observed.record,
                    (player.ConditionChangedEvent, player.ShardEnteredEvent),
                ):
                    assert observed.record.data.player.userid == userid
                expected.discard(type(observed.record))

            saved = await server.save(completion_timeout=OPERATION_TIMEOUT)
            assert saved.snapshot is None or saved.snapshot >= runtime.snapshot
            generation = server.lifecycle.session_generation
            await server.game.world.reset(completion_timeout=OPERATION_TIMEOUT)
            assert server.lifecycle.session_generation > generation
            assert server.driver_health.telemetry_status == "active"
            assert (await server.game.world.runtime()).session_id == server.session_id
            assert await server.game.world.execute(
                "return {"
                "day=TheWorld.topology.overrides.day,"
                "mod_answer=TheWorld.dst_config_verify,"
                "client_mods_disabled=KnownModIndex:AreClientModsDisabled()"
                "}"
            ) == {
                "day": "onlyday",
                "mod_answer": 42,
                "client_mods_disabled": False,
            }
            assert server.telemetry_invalid == 0
            assert all("DST_OTEL|" not in line for line in logs)
            assert server.returncode is None
        await server.stop(grace_period=OPERATION_TIMEOUT)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        await reap_server(server)


@pytest.mark.skipif(
    os.environ.get("DST_SERVER_NETDATA_TEST") != "1",
    reason="set DST_SERVER_NETDATA_TEST=1 to verify the local Netdata Agent",
)
async def test_real_game_event_is_persisted_by_netdata(  # ruff: ignore[too-many-locals, too-many-statements]
    tmp_path: Path,
    container_name: str,
) -> None:
    userid = str(ULID())
    cluster = write_cluster(tmp_path, tick_rate=1)
    source = Path(__file__).parents[1] / "src"
    process = await asyncio.create_subprocess_exec(
        "podman",
        "run",
        "--pull=never",
        "--rm",
        "--name",
        container_name,
        "--network",
        "podman",
        "--env",
        "PYTHONPATH=/workspace/src",
        "--env",
        "DST_SERVER_TELEMETRY_PROFILE=history",
        "--env",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://10.255.255.254:4317",
        "--env",
        "OTEL_METRICS_EXPORTER=none",
        "--env",
        "OTEL_TRACES_EXPORTER=none",
        "--volume",
        f"{cluster}:/cluster:Z",
        "--volume",
        f"{source}:/workspace/src:ro",
        IMAGE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout = process.stdout
    assert stdout is not None
    output: deque[str] = deque(maxlen=200)
    ready = asyncio.Event()
    telemetry_active = False
    tick_rate_active = False

    async def read_logs() -> None:
        nonlocal telemetry_active, tick_rate_active
        while (line := await read_line(stdout)) is not None:
            value = line.decode(errors="replace").rstrip()
            output.append(value)
            if '"telemetry_status":"active"' in value:
                telemetry_active = True
            if "Network tick rate: U=10" in value:
                tick_rate_active = True
            if "All DST shards are ready." in value:
                ready.set()

    log_task = asyncio.create_task(read_logs())
    exit_task = asyncio.create_task(process.wait())
    ready_task = asyncio.create_task(ready.wait())
    try:  # ruff:ignore[too-many-statements-in-try-clause]
        async with asyncio.timeout(STARTUP_TIMEOUT):
            done, _ = await asyncio.wait(
                (ready_task, exit_task, log_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
        if log_task in done:
            await log_task
        assert ready_task in done, "\n".join(output)
        assert exit_task not in done, "\n".join(output)
        assert telemetry_active
        assert tick_rate_active
        command = (
            "local player=SpawnPrefab('wilson');"
            f"player.userid={lua_string(userid)};"
            "TheWorld:PushEvent('ms_playerjoined',player);"
            "player:Remove();return true"
        )
        await asyncio.to_thread(
            (cluster / "console").write_text,
            command + "\n",
            encoding="utf-8",
        )

        fields = await netdata_player_fields(userid)

        assert fields["body.player.userid"] == userid
        assert fields["resource.attributes.service.name"] == "dst-server"
        assert fields["resource.attributes.dst.cluster.name"] == cluster.name
        assert fields["attributes.dst.cluster.name"] == "cluster"
        assert fields["attributes.dst.shard.name"] == "forest"
        assert int(fields["attributes.dst.event.sequence"]) > 0
        assert int(fields["attributes.dst.tick"]) >= 0
        assert all("DST_OTEL|" not in line for line in output)

        stop = await asyncio.create_subprocess_exec(
            "podman",
            "stop",
            "--time",
            "40",
            container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stop_output = await communicate(stop, OPERATION_TIMEOUT)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            returncode = await exit_task
            await log_task
        assert stop.returncode == 0, stop_output.decode(errors="replace")
        assert returncode == 0, "\n".join(output)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(output))
        raise
    finally:
        for task in (ready_task, exit_task, log_task):
            task.cancel()
        await kill_and_reap(process)
        await asyncio.gather(ready_task, exit_task, log_task, return_exceptions=True)


async def test_partial_telemetry_install_failure_keeps_game_running(
    tmp_path: Path,
    container_name: str,
) -> None:
    cluster = write_cluster(tmp_path)
    lua_directory = tmp_path / "faulty-lua"
    shutil.copytree(ServerConfig(shard="forest").lua_directory, lua_directory)
    world_events = lua_directory / "dst_server" / "world_events.lua"
    source = world_events.read_text(encoding="utf-8")
    needle = "    local state_names = {"
    assert needle in source
    world_events.write_text(
        source.replace(
            needle,
            '    error("injected telemetry install failure", 0)\n\n' + needle,
            1,
        ),
        encoding="utf-8",
    )
    logs: deque[str] = deque(maxlen=200)
    server = make_server(
        tmp_path,
        cluster,
        container_name,
        lua_directory,
        log_handler=logs.append,
    )
    try:  # ruff:ignore[too-many-statements-in-try-clause]
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            health = server.driver_health
            assert health.telemetry_status == "failed"
            assert health.telemetry_error is not None
            assert "injected telemetry install failure" in health.telemetry_error
            assert (await server.game.world.room()).is_dedicated is True
            assert await server.game.world.execute(
                "local sentinel=false;"
                "TheWorld:ListenForEvent('entity_death',function() sentinel=true end);"
                "local rabbit=SpawnPrefab('rabbit');rabbit:AddTag('epic');"
                "local ok=pcall(function() TheWorld:PushEvent('entity_death',"
                "{inst=rabbit,corpsing=false}) end);"
                "local alive=rabbit:IsValid();rabbit:Remove();"
                "return ok and sentinel and alive"
            )
            await server.save(completion_timeout=OPERATION_TIMEOUT)
            assert server.returncode is None
        await server.stop(grace_period=OPERATION_TIMEOUT)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        await reap_server(server)


async def test_core_lua_failure_keeps_game_and_raw_console_running(
    tmp_path: Path,
    container_name: str,
) -> None:
    cluster = write_cluster(tmp_path)
    lua_directory = tmp_path / "broken-core-lua"
    shutil.copytree(ServerConfig(shard="forest").lua_directory, lua_directory)
    (lua_directory / "dst_server.lua").write_text(
        'error("injected core driver failure", 0)\n',
        encoding="utf-8",
    )
    logs: deque[str] = deque(maxlen=200)
    server = make_server(
        tmp_path,
        cluster,
        container_name,
        lua_directory,
        log_handler=logs.append,
    )
    try:  # ruff:ignore[too-many-statements-in-try-clause]
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            assert server.driver_error is not None
            assert "injected core driver failure" in server.driver_error
            assert server.returncode is None

            with pytest.raises(RuntimeError, match="has not been installed"):
                await server.game.world.room()

            sentinel = str(ULID())
            assert sentinel in await server.execute(
                f'print(TheWorld~=nil and {lua_string(sentinel)} or "missing")'
            )
            save_count = server.lifecycle.save_count
            await server.execute("c_save()")
            while server.lifecycle.save_count == save_count:
                assert await server.read_event() is not None
            assert server.lifecycle.last_saved is not None
            assert server.returncode is None
        await server.stop(grace_period=OPERATION_TIMEOUT)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        await reap_server(server)


@pytest.mark.skipif(
    os.geteuid() != 0
    or not Path("/run/systemd/system").is_dir()
    or not QUADLET_GENERATOR.is_file(),
    reason="systemd Quadlet runtime is unavailable",
)
async def test_generated_quadlet_pod_runs_one_process_per_shard(  # ruff: ignore[complex-structure, too-many-branches, too-many-locals, too-many-statements]
) -> None:
    cluster_dir = QUADLET_ROOT / "cluster data"
    quadlet_dir = QUADLET_ROOT / "quadlet"
    unit_names = (
        f"{QUADLET_PREFIX}.pod",
        f"{QUADLET_PREFIX}-prepare.container",
        f"{QUADLET_PREFIX}-cave.container",
        f"{QUADLET_PREFIX}-forest.container",
    )
    services = tuple(
        f"{Path(name).stem}{'-pod' if name.endswith('.pod') else ''}.service"
        for name in unit_names
    )
    pod_service, prepare_service, *worker_services = services
    pod_name = f"systemd-{QUADLET_PREFIX}"
    worker_names = tuple(
        f"systemd-{QUADLET_PREFIX}-{shard}" for shard in ("cave", "forest")
    )

    async def invoke(
        *command: str,
        seconds: float = CLEANUP_TIMEOUT,
        check: bool = True,
    ) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output = (await communicate(process, seconds)).decode(errors="replace")
        if check:
            assert process.returncode == 0, f"{shlex.join(command)}\n{output}"
        return process.returncode or 0, output

    async def assert_podman_absent(kind: str, name: str) -> None:
        returncode, _ = await invoke("podman", kind, "exists", name, check=False)
        assert returncode == 1

    async def cleanup() -> None:  # ruff: ignore[complex-structure]
        errors: list[Exception] = []

        async def attempt(*command: str, seconds: float = CLEANUP_TIMEOUT) -> None:
            try:
                await invoke(*command, seconds=seconds, check=False)
            except Exception as error:  # pragma: no cover - cleanup diagnostics
                errors.append(error)

        await attempt("systemctl", "stop", pod_service, seconds=OPERATION_TIMEOUT)
        await attempt("podman", "pod", "rm", "--force", pod_name)
        for name in (f"systemd-{QUADLET_PREFIX}-prepare", *worker_names):
            await attempt(
                "podman",
                "rm",
                "--force",
                "--time",
                "0",
                name,
            )
        for name in unit_names:
            try:
                (SYSTEM_QUADLET_ROOT / name).unlink(missing_ok=True)
            except OSError as error:
                errors.append(error)
        await attempt("systemctl", "daemon-reload")
        try:
            await assert_podman_absent("pod", pod_name)
            for name in (
                f"systemd-{QUADLET_PREFIX}-prepare",
                f"{pod_name}-infra",
                *worker_names,
            ):
                await assert_podman_absent("container", name)
            for service in services:
                _, load_state = await invoke(
                    "systemctl",
                    "show",
                    "--property=LoadState",
                    "--value",
                    service,
                    check=False,
                )
                assert load_state.strip() == "not-found"
        except Exception as error:  # pragma: no cover - cleanup diagnostics
            errors.append(error)
        try:
            shutil.rmtree(QUADLET_ROOT)
        except FileNotFoundError:
            pass
        except OSError as error:
            errors.append(error)
        if errors:
            message = "Quadlet cleanup failed"
            raise ExceptionGroup(message, errors)

    async def wait_until_workers_are_ready(
        *,
        minimum_connections: int = 1,
    ) -> dict[str, str]:
        async with asyncio.timeout(STARTUP_TIMEOUT):
            while True:
                logs = {}
                game_logs = {}
                for name in worker_names:
                    returncode, output = await invoke(
                        "podman",
                        "logs",
                        "--tail",
                        "200",
                        name,
                        check=False,
                    )
                    if returncode == 0:
                        logs[name] = output
                for shard in ("cave", "forest"):
                    path = cluster_dir / shard / "server_log.txt"
                    if path.is_file():
                        game_logs[shard] = await asyncio.to_thread(
                            path.read_text,
                            encoding="utf-8",
                            errors="replace",
                        )
                sdk_ready = len(logs) == len(worker_names) and all(
                    "All DST shards are ready." in output for output in logs.values()
                )
                shards_connected = (
                    game_logs.get("forest", "").count(
                        "Secondary shard Caves(2) connected:"
                    )
                    >= minimum_connections
                    and "[Shard] secondary shard is now ready!"
                    in game_logs.get("cave", "")
                )
                if sdk_ready and shards_connected:
                    return game_logs
                await asyncio.sleep(0.5)

    try:  # ruff: ignore[too-many-statements-in-try-clause]
        await cleanup()
        _, network = await invoke("podman", "network", "inspect", "podman")
        assert json.loads(network)[0]["dns_enabled"] is True
        _, resolved = await invoke(
            "podman",
            "run",
            "--rm",
            IMAGE,
            "/usr/bin/getent",
            "ahostsv4",
            "lobby-v2-cdn.klei.com",
        )
        assert resolved.strip()
        cluster = build(
            0,
            token=SecretStr(""),
            cluster_key=SecretStr("quadlet-system-test-key"),
        )
        cluster = cluster.replace(
            settings=cluster.settings.replace(
                offline_cluster=True,
                lan_only_cluster=True,
                internet_broadcasting_enabled=False,
            ),
        )
        cluster.save(cluster_dir)
        source_mount = VolumeMount(
            source=Path(__file__).parents[1] / "src",
            target=PurePosixPath("/workspace/src"),
            read_only=True,
        )
        netdata_enabled = os.environ.get("DST_SERVER_NETDATA_TEST") == "1"
        environment = {"PYTHONPATH": "/workspace/src"} | (
            NETDATA_ENVIRONMENT if netdata_enabled else {"OTEL_SDK_DISABLED": "true"}
        )
        application = QuadletApplication.for_cluster(
            cluster,
            cluster_dir,
            name=QUADLET_PREFIX,
            image=IMAGE,
            allocation=RoomPortAllocation(number=297),
            telemetry_environment=environment,
        )
        application = application.replace(
            prepare=application.prepare.replace(
                environment=environment,
                volumes=(*application.prepare.volumes, source_mount),
            ),
            workers=tuple(
                worker.replace(volumes=(*worker.volumes, source_mount))
                for worker in application.workers
            ),
        )
        unit_paths = application.save(quadlet_dir)
        assert {path.name for path in unit_paths} == set(unit_names)

        SYSTEM_QUADLET_ROOT.mkdir(  # ruff: ignore[blocking-path-method-in-async-function]
            parents=True,
            exist_ok=True,
        )
        for path in unit_paths:
            shutil.copy2(path, SYSTEM_QUADLET_ROOT / path.name)
        await invoke("systemctl", "daemon-reload")
        await invoke(
            "systemctl",
            "start",
            pod_service,
            seconds=STARTUP_TIMEOUT,
        )

        game_logs = await wait_until_workers_are_ready()
        for service in (pod_service, *worker_services):
            _, state = await invoke("systemctl", "is-active", service)
            assert state.strip() == "active"
        _, first_prepare_started = await invoke(
            "systemctl",
            "show",
            "--property=ExecMainStartTimestampMonotonic",
            "--value",
            prepare_service,
        )
        assert int(first_prepare_started) > 0
        _, inspected = await invoke("podman", "pod", "inspect", pod_name)
        (pod,) = json.loads(inspected)
        pod_id = pod["Id"]
        assert {container["Name"] for container in pod["Containers"]} == {
            f"{pod_name}-infra",
            *worker_names,
        }
        bindings = pod["InfraConfig"]["PortBindings"]
        actual_ports = {
            (int(binding["HostPort"]), int(container.split("/", maxsplit=1)[0]))
            for container, values in bindings.items()
            for binding in values
        }
        assert actual_ports == {
            (mapping.host, mapping.container)
            for mapping in application.pod.publish_ports
        }

        for shard, name in zip(("cave", "forest"), worker_names, strict=True):
            _, processes = await invoke("podman", "top", name, "args")
            assert (
                sum(GAME_EXECUTABLE in line for line in processes.splitlines()) == 1
            ), processes
            assert f"-shard {shard}" in processes
            external_port = next(
                mapping.host
                for mapping in application.pod.publish_ports
                if mapping.container == cluster.shards[shard].settings.server_port
            )
            assert f"-external_port {external_port}" in processes
            settings = cluster.shards[shard].settings
            assert (
                f"SteamGameServer_Init({settings.server_port}, "
                f"{settings.master_server_port})" in game_logs[shard]
            )
            assert "SteamGameServer_Init success" in game_logs[shard]
        assert "missing is_master" not in "\n".join(game_logs.values())

        journal_marker = f"DST_QUADLET_JOURNAL_{ULID()}"
        await asyncio.to_thread(
            (cluster_dir / "console").write_text,
            f"print({lua_string(journal_marker)})\n",
            encoding="utf-8",
        )
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while True:
                _, journal = await invoke(
                    "journalctl",
                    "--unit",
                    worker_services[1],
                    "--grep",
                    journal_marker,
                    "--no-pager",
                    "--output=cat",
                    check=False,
                )
                if journal_marker in journal:
                    break
                await asyncio.sleep(0.5)

        if netdata_enabled:
            userid = str(ULID())
            command = (
                "local player=SpawnPrefab('wilson');"
                f"player.userid={lua_string(userid)};"
                "TheWorld:PushEvent('ms_playerjoined',player);"
                "player:Remove();return true"
            )
            await asyncio.to_thread(
                (cluster_dir / "console").write_text,
                command + "\n",
                encoding="utf-8",
            )
            fields = await netdata_player_fields(userid)
            assert fields["body.player.userid"] == userid
            assert fields["resource.attributes.service.name"] == "dst-server"
            assert fields["resource.attributes.dst.cluster.name"] == QUADLET_PREFIX
            assert fields["attributes.dst.cluster.name"] == QUADLET_PREFIX
            assert fields["attributes.dst.shard.name"] == "forest"

        worker_ids = {}
        for name in worker_names:
            _, worker_ids[name] = await invoke(
                "podman",
                "inspect",
                "--format={{.Id}}",
                name,
            )
            worker_ids[name] = worker_ids[name].strip()

        target_name, peer_name = worker_names
        target_service = worker_services[0]
        connection_count = game_logs["forest"].count(
            "Secondary shard Caves(2) connected:"
        )
        _, restart_count = await invoke(
            "systemctl",
            "show",
            "--property=NRestarts",
            "--value",
            target_service,
        )
        await invoke("podman", "kill", "--signal", "KILL", target_name)
        async with asyncio.timeout(STARTUP_TIMEOUT):
            while True:
                inspect_code, recovered_target_id = await invoke(
                    "podman",
                    "inspect",
                    "--format={{.Id}}",
                    target_name,
                    check=False,
                )
                _, current_restarts = await invoke(
                    "systemctl",
                    "show",
                    "--property=NRestarts",
                    "--value",
                    target_service,
                    check=False,
                )
                active_code, _ = await invoke(
                    "systemctl",
                    "is-active",
                    target_service,
                    check=False,
                )
                if (
                    inspect_code == 0
                    and recovered_target_id.strip() != worker_ids[target_name]
                    and current_restarts.strip().isdigit()
                    and int(current_restarts) > int(restart_count)
                    and active_code == 0
                ):
                    break
                await asyncio.sleep(0.5)

        recovered_game_logs = await wait_until_workers_are_ready(
            minimum_connections=connection_count + 1,
        )
        assert "missing is_master" not in "\n".join(recovered_game_logs.values())
        _, peer_id = await invoke(
            "podman",
            "inspect",
            "--format={{.Id}}",
            peer_name,
        )
        assert peer_id.strip() == worker_ids[peer_name]
        _, current_pod_id = await invoke(
            "podman",
            "pod",
            "inspect",
            "--format={{.Id}}",
            pod_name,
        )
        assert current_pod_id.strip() == pod_id
        worker_ids[target_name] = recovered_target_id.strip()

        for shard in ("cave", "forest"):
            await asyncio.to_thread(
                (cluster_dir / shard / "server_log.txt").unlink,
                missing_ok=True,
            )
        await invoke(
            "systemctl",
            "restart",
            pod_service,
            seconds=STARTUP_TIMEOUT,
        )
        restarted_game_logs = await wait_until_workers_are_ready()
        _, second_prepare_started = await invoke(
            "systemctl",
            "show",
            "--property=ExecMainStartTimestampMonotonic",
            "--value",
            prepare_service,
        )
        assert int(second_prepare_started) > int(first_prepare_started)
        for name in worker_names:
            _, worker_id = await invoke(
                "podman",
                "inspect",
                "--format={{.Id}}",
                name,
            )
            assert worker_id.strip() != worker_ids[name]
        assert "missing is_master" not in "\n".join(restarted_game_logs.values())
        returncode, state = await invoke(
            "systemctl",
            "is-active",
            prepare_service,
            check=False,
        )
        assert returncode != 0
        assert state.strip() == "inactive"
        await assert_podman_absent("container", f"systemd-{QUADLET_PREFIX}-prepare")

        await invoke(
            "systemctl",
            "stop",
            pod_service,
            seconds=OPERATION_TIMEOUT,
        )
        for service in worker_services:
            _, result = await invoke(
                "systemctl",
                "show",
                "--property=Result",
                "--property=ExecMainStatus",
                service,
            )
            assert "Result=success" in result
            assert "ExecMainStatus=0" in result
        for name in worker_names:
            await assert_podman_absent("container", name)
        await assert_podman_absent("pod", pod_name)
    except BaseException as error:
        _, diagnostic = await invoke(
            "systemctl",
            "status",
            "--no-pager",
            *services,
            check=False,
        )
        error.add_note("Quadlet service status:\n" + diagnostic)
        raise
    finally:
        await cleanup()
