import asyncio
import json
import os
import select
import shlex
import shutil
import socket
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import logbook
import pytest
from pydantic import JsonValue, SecretStr
from ulid import ULID

from dst_server import commands as c
from dst_server.activity import read_last_login
from dst_server.archive import export_cluster
from dst_server.cluster.agent import ShardAgent
from dst_server.cluster.controller import AgentEndpoint, ClusterController
from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.configuration.overrides import WorldgenOverride
from dst_server.configuration.presets import FOREST_CAVES
from dst_server.configuration.store import ConfigurationStore
from dst_server.configuration.world import ForestOverrides
from dst_server.deployment import ContainerUnit, QuadletApplication, RoomPortAllocation
from dst_server.errors import (
    DisconnectedError,
    IndeterminateCommandError,
)
from dst_server.events import player
from dst_server.events import server as server_events
from dst_server.game.rpc import LuaRequestError
from dst_server.klei_id import encode_klei_id
from dst_server.lua_codec import lua_string
from dst_server.models.cluster import (
    ClusterStatus,
    ConfigurationSnapshot,
    GameEventRecord,
    LifecycleRecord,
    ShardRuntimeStatus,
)
from dst_server.netdata import NetdataLogQuery, NetdataLogs
from dst_server.presets.lst import NETDATA_ENVIRONMENT, fleet_room
from dst_server.rooms import Room, RoomStore
from dst_server.rpc import ClusterClient, Subscription, rpc_runtime
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.supervisor import MAX_ATTEMPTS
from dst_server.telemetry import TelemetrySettings

REPOSITORY_ROOT = Path(__file__).parents[2]
IMAGE = os.environ.get("DST_SERVER_IMAGE", "")
VOLUME_IDMAP = "uids=0-1000-1;gids=0-1000-1"

pytestmark = [
    pytest.mark.system,
    pytest.mark.skipif(
        os.environ.get("DST_SERVER_PODMAN_TEST") != "1",
        reason="run just test-system IMAGE to test a local image",
    ),
    pytest.mark.skipif(
        not IMAGE,
        reason="set DST_SERVER_IMAGE to the exact local image ID or tag",
    ),
    pytest.mark.skipif(
        os.geteuid() != 0,
        reason="rootful Podman is required for the idmapped test volumes",
    ),
]

GAME_EXECUTABLE = "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"
STARTUP_TIMEOUT = 300
OPERATION_TIMEOUT = 60
CLEANUP_TIMEOUT = 30
WATCHDOG_TEST_TIMEOUT = 90
SYSTEM_QUADLET_ROOT = Path("/run/containers/systemd")
QUADLET_GENERATOR = Path("/usr/lib/systemd/system-generators/podman-system-generator")
HAS_QUADLET_RUNTIME = (
    Path("/run/systemd/system").is_dir() and QUADLET_GENERATOR.is_file()
)
SHARDS = ("cave", "forest")
MASTER = "forest"


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def capture_game_logs() -> Iterator[None]:
    with logbook.StderrHandler(level=logbook.INFO).applicationbound():
        yield


@pytest.fixture(scope="session", autouse=True)  # ruff: ignore[pytest-fixture-autouse]
def image_matches_expected_build() -> None:
    expected_revision = os.environ.get("DST_SERVER_EXPECTED_REVISION")
    expected_version = os.environ.get("DST_SERVER_EXPECTED_VERSION")
    podman = shutil.which("podman")
    assert podman is not None
    inspected = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        (
            podman,
            "image",
            "inspect",
            "--format",
            (
                "{{.Config.User}}|"
                '{{ index .Labels "org.opencontainers.image.revision" }}|'
                '{{ index .Labels "org.opencontainers.image.version" }}'
            ),
            IMAGE,
        ),
        capture_output=True,
        text=True,
        timeout=CLEANUP_TIMEOUT,
        check=False,
    )
    assert inspected.returncode == 0, inspected.stderr or inspected.stdout
    user, revision, version = inspected.stdout.strip().split("|", maxsplit=2)
    assert user == "steam"
    if expected_revision is not None:
        assert revision == expected_revision
    if expected_version is not None:
        assert version == expected_version


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


async def run_command(
    *command: str,
    seconds: float = CLEANUP_TIMEOUT,
    check: bool = True,
) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
    )
    output = (await communicate(process, seconds)).decode(errors="replace")
    returncode = process.returncode
    assert returncode is not None
    if check:
        assert returncode == 0, f"{shlex.join(command)}\n{output}"
    return returncode, output


async def remove_container(name: str) -> None:
    await run_command(
        "podman",
        "rm",
        "--force",
        "--time",
        "0",
        "--ignore",
        name,
    )


@pytest.fixture
async def container_name() -> AsyncIterator[str]:
    name = f"dst-sdk-test-{str(ULID()).lower()}"
    yield name
    await remove_container(name)


def single_shard_configuration(
    *, configured: bool = False, encode_user_path: bool = True
) -> ClusterConfig:
    return ClusterConfig(
        settings=ClusterSettings(
            cluster_name=str(ULID()),
            offline_cluster=True,
            lan_only_cluster=True,
            master_port=10888,
        ),
        shards={
            "forest": ShardConfig(
                settings=ShardSettings(
                    is_master=True,
                    id=1,
                    server_port=10999,
                    master_server_port=27018,
                    encode_user_path=encode_user_path,
                ),
                world=(
                    WorldgenOverride(overrides=ForestOverrides(day="onlyday"))
                    if configured
                    else WorldgenOverride.forest()
                ),
            )
        },
    )


def write_cluster(
    root: Path, *, configured: bool = False, encode_user_path: bool = True
) -> Path:
    cluster = root / "cluster"
    single_shard_configuration(
        configured=configured, encode_user_path=encode_user_path
    ).save(cluster)
    return cluster


def make_server(
    root: Path,
    cluster: Path,
    container_name: str,
    lua_directory: Path | None = None,
    *,
    log_handler: Callable[[str], None] | None = None,
    shard: str = "forest",
    pod: str | None = None,
) -> Server:
    wrapper = root / f"podman-dst-server-{shard}"
    command = [
        "podman",
        "run",
        "--pull=never",
        "--rm",
        "--name",
        container_name,
        "--preserve-fds=3",
        *(("--pod", pod) if pod is not None else ("--network", "none")),
        "--workdir",
        "/install/bin64",
        "--volume",
        f"{cluster}:/cluster:idmap={VOLUME_IDMAP}",
        "--volume",
        f"{cluster / 'mods'}:/install/mods:idmap={VOLUME_IDMAP}",
    ]
    lua_directory = lua_directory or ServerConfig(shard="forest").lua_directory
    command.extend((
        "--volume",
        f"{lua_directory}:/dst-server-lua:ro,idmap={VOLUME_IDMAP}",
    ))
    command.extend(("--entrypoint", GAME_EXECUTABLE, IMAGE))
    wrapper.write_text(
        "#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return Server(
        ServerConfig(
            shard=shard,
            executable=wrapper,
            persistent_storage_root=Path("/"),
            conf_dir="/",
            cluster="cluster",
            ugc_directory=Path("/cluster/mods/ugc"),
            extra_args=("-skip_update_server_mods", "-offline"),
            lua_directory=Path("/dst-server-lua"),
            telemetry=TelemetrySettings(profile="history"),
            monitor_parent_process=False,
        ),
        log_handler=log_handler,
    )


async def reap_server(server: Server, container_name: str) -> None:
    if server.child is None or server.closed:
        return
    async with asyncio.timeout(CLEANUP_TIMEOUT):
        await remove_container(container_name)
        await server.wait()


async def read_player(server: Server, userid: str) -> dict[str, JsonValue]:
    await server.game.invoke(
        c.ExecuteJson(
            source="DST_EXPORT_READ=nil;"
            "local file=TheNet:GetUserSessionFile("
            f"TheWorld.meta.session_identifier,{lua_string(userid)});"
            "assert(file~=nil,'player session file missing');"
            "TheNet:DeserializeUserSession(file,function(success,str)"
            "assert(success and str~=nil,'player session unreadable');"
            "local data,prefab=ParseUserSessionData(str);"
            "assert(data~=nil and prefab~='','invalid player save');"
            "DST_EXPORT_READ={file=file,prefab=prefab,x=data.x,z=data.z,"
            "health=data.data.health.health,hunger=data.data.hunger.hunger,"
            "inventory=data.data.inventory.items}"
            "end);return true"
        )
    )
    async with asyncio.timeout(OPERATION_TIMEOUT):
        while True:
            value = await server.game.invoke(
                c.ExecuteJson(source="return DST_EXPORT_READ")
            )
            if isinstance(value, dict):
                return value
            await asyncio.sleep(0.1)


async def shutdown_without_save(server: Server) -> None:
    await server.game.invoke(
        c.ExecuteJson(
            source=(
                "TheWorld:DoStaticTaskInTime(0,function() c_shutdown(false) end);"
                "return true"
            )
        )
    )
    async with asyncio.timeout(OPERATION_TIMEOUT):
        assert await server.wait() == 0


@asynccontextmanager
async def running_server(root: Path, cluster: Path) -> AsyncIterator[Server]:
    name = f"dst-export-{str(ULID()).lower()}"
    logs: deque[str] = deque(maxlen=100)
    server = make_server(root, cluster, name, log_handler=logs.append)
    try:
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        yield server
        await shutdown_without_save(server)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        await reap_server(server, name)


async def test_export_restores_world_and_encoded_player_save(tmp_path: Path) -> None:
    py7zr = pytest.importorskip("py7zr")
    source = write_cluster(tmp_path / "source", encode_user_path=False)
    userid = "KU_1234567_"
    async with running_server(tmp_path, source) as server:
        original = await server.game.invoke(c.Runtime())
        await server.game.invoke(
            c.ExecuteJson(
                source="local player=SpawnPrefab('wilson');"
                f"player.userid={lua_string(userid)};"
                "player.Physics:Teleport(4,0,5);"
                "player.components.health:SetCurrentHealth(63);"
                "player.components.hunger:SetCurrent(71);"
                "local item=SpawnPrefab('goldnugget');"
                "item.components.stackable:SetStackSize(7);"
                "player.components.inventory:GiveItem(item);"
                "SerializeUserSession(player,true);return true"
            )
        )
        await server.save(completion_timeout=OPERATION_TIMEOUT)
        before = await read_player(server, userid)
        assert f"/{userid}_/" in str(before["file"])
        assert before["health"] == 63
        assert before.pop("hunger") == pytest.approx(71, abs=1)

    with (
        export_cluster(source, room_id="cluster") as exported,
        py7zr.SevenZipFile(exported.stream) as archive,
    ):
        archive.extractall(tmp_path / "restored")
    restored = tmp_path / "restored" / "cluster"
    session = Path("forest/save/session") / original.session_id
    source_player = source / session / f"{userid}_"
    restored_player_directory = restored / session / encode_klei_id(userid)
    assert {path.name: path.read_bytes() for path in source_player.iterdir()} == {
        path.name: path.read_bytes() for path in restored_player_directory.iterdir()
    }
    (restored / "cluster_token.txt").write_text("", encoding="utf-8")
    async with running_server(tmp_path, restored) as server:
        runtime = await server.game.invoke(c.Runtime())
        assert runtime.session_id == original.session_id
        assert runtime.seed == original.seed
        assert await server.game.invoke(
            c.ExecuteJson(source="return TheNet:GetDefaultEncodeUserPath()")
        )
        after = await read_player(server, userid)
        assert f"/{encode_klei_id(userid)}/" in str(after.pop("file"))
        assert after.pop("hunger") == pytest.approx(71, abs=1)
        before.pop("file")
        assert after == before
        await server.game.invoke(
            c.ExecuteJson(
                source=f"RestoreSnapshotUserSession(TheWorld.meta.session_identifier,{lua_string(userid)});"
                "return true"
            )
        )
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while True:
                restored_player = await server.game.invoke(
                    c.ExecuteJson(
                        source="for _,player in pairs(Ents) do "
                        f"if player.userid=={lua_string(userid)} "
                        "and player.is_snapshot_user_session then "
                        "return {prefab=player.prefab,"
                        "health=player.components.health.currenthealth} "
                        "end end;return nil"
                    )
                )
                if restored_player is not None:
                    break
                await asyncio.sleep(0.1)
        assert restored_player == {"prefab": "wilson", "health": 63}


@asynccontextmanager
async def running_sharded_cluster(
    root: Path,
) -> AsyncIterator[tuple[ClusterController, dict[str, ShardAgent]]]:
    cluster = root / "cluster"
    FOREST_CAVES.build(
        token=SecretStr(""),
        cluster_key=SecretStr(str(ULID())),
        settings=ClusterSettings(
            cluster_name=str(ULID()),
            offline_cluster=True,
            lan_only_cluster=True,
            pause_when_empty=False,
            max_snapshots=20,
        ),
    ).save(cluster)
    configuration = ConfigurationStore(cluster)
    controller = ClusterController(configuration, install_path=root / "forest")
    names = {
        shard: f"dst-snapshot-{shard}-{str(ULID()).lower()}"
        for shard in ("forest", "cave")
    }
    pod = f"dst-snapshot-{str(ULID()).lower()}"
    agents: dict[str, ShardAgent] = {}
    try:
        await run_command(
            "podman",
            "pod",
            "create",
            "--name",
            pod,
            "--network",
            "none",
            "--share",
            "net",
        )
        for shard in ("forest", "cave"):
            config = make_server(
                root,
                cluster,
                names[shard],
                shard=shard,
                pod=pod,
            ).config
            install = root / shard
            executable = install / "bin64" / Path(GAME_EXECUTABLE).name
            executable.parent.mkdir(parents=True)
            executable.symlink_to(config.executable)
            agent = ShardAgent(
                next(item for item in configuration.shards if item.name == shard),
                install_path=install,
                cluster_path=cluster,
            )
            agent.config = config
            agents[shard] = agent
            await agent.activate()
            await agent.start()
            await controller.register(cast("AgentEndpoint", agent))
        await controller.start()
        yield controller, agents
    finally:
        try:
            await controller.aclose()
        finally:
            try:
                async with asyncio.TaskGroup() as tasks:
                    for agent in agents.values():
                        tasks.create_task(agent.aclose())
            finally:
                await run_command(
                    "podman", "pod", "rm", "--force", "--time", "0", "--ignore", pod
                )


async def test_player_loaded_login_survives_restart_but_not_regeneration(
    tmp_path: Path,
) -> None:
    async with running_sharded_cluster(tmp_path) as (controller, agents):
        sessions = {
            shard: (await agent.invoke(c.Runtime())).session_id
            for shard, agent in agents.items()
        }
        master = agents[MASTER]
        shard_directory = tmp_path / "cluster" / MASTER
        marker = shard_directory / "save/session" / sessions[MASTER] / ".last_login"
        snapshots = await master.invoke(c.Snapshots())
        assert snapshots.snapshots
        assert not marker.exists()
        userid = "KU_1234567_"
        with closing(controller.subscribe_events()) as events:

            async def observe(
                event_type: type[player.ShardEnteredEvent | player.PlayerLoadedEvent],
            ) -> GameEventRecord:
                async with asyncio.timeout(OPERATION_TIMEOUT):
                    while True:
                        record = (await events.next(1))[0]
                        if (
                            record.shard == MASTER
                            and isinstance(record.event, event_type)
                            and record.event.data.player.userid == userid
                        ):
                            return record

            # Exercise the native handshake on a real entity without a network client.
            await master.invoke(
                c.ExecuteJson(
                    source="DST_LOGIN_PLAYER=SpawnPrefab('wilson');"
                    f"DST_LOGIN_PLAYER.userid={lua_string(userid)};return true"
                )
            )
            await observe(player.ShardEnteredEvent)
            assert not marker.exists()
            await master.invoke(
                c.ExecuteJson(
                    source="DST_LOGIN_PLAYER:OnPostActivateHandshake_Server("
                    "POSTACTIVATEHANDSHAKE.READY);return true"
                )
            )
            loaded = await observe(player.PlayerLoadedEvent)
        assert loaded.event.session_id == sessions[MASTER]
        timestamp = datetime.fromtimestamp(loaded.observed_timestamp_ns / 1e9, UTC)
        assert read_last_login(shard_directory, sessions[MASTER]) == timestamp
        assert marker.read_text(encoding="utf-8") == f"{timestamp.isoformat()}\n"
        assert await master.invoke(c.Snapshots()) == snapshots
        await controller.restart()
        for shard, agent in agents.items():
            assert (await agent.invoke(c.Runtime())).session_id == sessions[shard]
        assert read_last_login(shard_directory, sessions[MASTER]) == timestamp
        await controller.regenerate(
            expected_session_id=sessions[MASTER],
            require_empty=True,
            timeout=STARTUP_TIMEOUT,
        )
        for shard, agent in agents.items():
            session = (await agent.invoke(c.Runtime())).session_id
            assert session != sessions[shard]
            assert read_last_login(tmp_path / "cluster" / shard, session) is None


async def test_rollback_to_day_restores_both_shards_and_player_saves(
    tmp_path: Path,
) -> None:
    users = {"forest": "KU_1234567_", "cave": "KU_7654321_"}
    health = {"forest": 63, "cave": 74}
    async with running_sharded_cluster(tmp_path) as (controller, agents):
        sessions = {
            shard: (await agent.invoke(c.Runtime())).session_id
            for shard, agent in agents.items()
        }
        for shard, agent in agents.items():
            await agent.invoke(
                c.ExecuteJson(
                    source="TheWorld:PushEvent('ms_setautosaveenabled',false);"
                    "local clock=TheWorld.net.components.clock;"
                    "local data=clock:OnSave();data.cycles=9;clock:OnLoad(data);"
                    "DST_SNAPSHOT_PLAYER=SpawnPrefab('wilson');"
                    "local player=DST_SNAPSHOT_PLAYER;"
                    f"player.userid={lua_string(users[shard])};"
                    f"player.components.health:SetCurrentHealth({health[shard]});"
                    "player.Physics:Teleport(4,0,5);"
                    "local item=SpawnPrefab('goldnugget');"
                    "item.components.stackable:SetStackSize(7);"
                    "player.components.inventory:GiveItem(item);return true"
                )
            )
        target = await controller.save()
        for shard, later_health in {"forest": 41, "cave": 42}.items():
            await agents[shard].invoke(
                c.ExecuteJson(
                    source="DST_SNAPSHOT_PLAYER.components.health:SetCurrentHealth("
                    f"{later_health});return true"
                )
            )
        later_same_day = await controller.save()
        for agent in agents.values():
            await agent.invoke(
                c.ExecuteJson(
                    source="DST_SNAPSHOT_PLAYER.components.health:SetCurrentHealth(17);"
                    "local clock=TheWorld.net.components.clock;"
                    "local data=clock:OnSave();data.cycles=19;"
                    "clock:OnLoad(data);return true"
                )
            )
        missing = await controller.save()
        latest = await controller.save()
        assert target.snapshot is not None
        assert later_same_day.snapshot is not None
        assert missing.snapshot is not None
        assert latest.snapshot is not None
        assert latest.snapshot > missing.snapshot > later_same_day.snapshot
        assert later_same_day.snapshot > target.snapshot
        for shard in agents:
            for path in (tmp_path / "cluster" / shard / "save").rglob(
                f"{missing.snapshot:010d}*"
            ):
                if path.is_file():
                    path.unlink()
        page = await controller.list_snapshots(limit=1)
        assert page.has_more
        assert page.snapshots[0].snapshot_id == latest.snapshot
        assert page.snapshots[0].metadata is not None
        assert page.snapshots[0].metadata.day == 20
        previous = await controller.list_snapshots(limit=1, before=latest.snapshot)
        assert previous.snapshots[0].snapshot_id == later_same_day.snapshot
        assert previous.snapshots[0].metadata is not None
        assert previous.snapshots[0].metadata.day == 10
        earliest = await controller.list_snapshots(
            limit=1, before=later_same_day.snapshot
        )
        assert earliest.snapshots[0].snapshot_id == target.snapshot
        assert earliest.snapshots[0].metadata is not None
        assert earliest.snapshots[0].metadata.day == 10
        # Schedule a native autosave at the exact point rollback begins truncating.
        await agents["forest"].invoke(
            c.ExecuteJson(
                source="local methods=getmetatable(TheNet).__index;"
                "local original=methods.TruncateSnapshots;"
                "methods.TruncateSnapshots=function(self,session,count)"
                "methods.TruncateSnapshots=original;"
                "TheWorld:PushEvent('ms_save');"
                "return original(self,session,count) end;return true"
            )
        )
        restored = await controller.rollback_to_day(10)
        assert restored.snapshot_id == target.snapshot
        for shard, agent in agents.items():
            assert (await agent.invoke(c.Runtime())).session_id == sessions[shard]
            assert (await agent.invoke(c.World())).day == 10
            saved_player = await read_player(agent.server, users[shard])
            assert saved_player["health"] == health[shard]
            assert saved_player["inventory"] == [
                {
                    "prefab": "goldnugget",
                    "x": 4,
                    "z": 5,
                    "data": {"stackable": {"stack": 7}},
                }
            ]


async def check_console_contract(client: ClusterClient) -> None:
    shard = client.shard(MASTER)
    methods = {method.name: method for method in await shard.describe()}
    assert methods["evaluate"].mutation
    assert methods["evaluate"].scope == "shard"
    evaluated = await shard.evaluate(
        'local x = 2\nprint("cli-console", x)\nreturn x + 1, nil, false'
    )
    assert "cli-console" in evaluated.output
    assert [(value.type, value.text) for value in evaluated.values] == [
        ("number", "3"),
        ("nil", "nil"),
        ("boolean", "false"),
    ]
    assert evaluated.error is None
    failed = await shard.evaluate(
        '(function() rawset(_G, "DST_CLI_COUNT", '
        '(rawget(_G, "DST_CLI_COUNT") or 0) + 1); '
        'error("console-failure") end)()'
    )
    assert failed.error is not None
    assert failed.error.kind == "runtime"
    assert "console-failure" in failed.error.message
    counted = await shard.call("evaluate", {"source": 'rawget(_G, "DST_CLI_COUNT")'})
    assert isinstance(counted, dict)
    assert counted["values"] == [{"type": "number", "text": "1"}]
    invalid = await shard.evaluate("local =")
    assert invalid.error is not None
    assert invalid.error.kind == "compile"
    for source in (
        'for i = 1, 8192 do print("x") end; return 42',
        'print(string.rep("x", 65536)); return 42',
    ):
        bounded = await shard.evaluate(source)
        assert bounded.error is None
        assert bounded.truncated
        assert bounded.output.startswith("x")
        assert len(bounded.output.encode()) <= 2048
        assert [(value.type, value.text) for value in bounded.values] == [
            ("number", "42")
        ]
        recovered = await shard.evaluate('print("after-limit"); return 1 + 2')
        assert recovered.error is None
        assert not recovered.truncated
        assert recovered.output == "after-limit"
        assert [(value.type, value.text) for value in recovered.values] == [
            ("number", "3")
        ]


async def test_image_entrypoint_runs_single_shard_and_handles_sigterm(
    tmp_path: Path,
    container_name: str,
) -> None:
    rooms = RoomStore(tmp_path)
    rooms.save(Room(number=299, cluster=single_shard_configuration()))
    cluster = rooms.path(299)
    assert (cluster / "cluster.ini").is_file()
    assert (cluster / MASTER / "server.ini").is_file()
    client: ClusterClient | None = None
    lifecycle: Subscription[LifecycleRecord] | None = None
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        await run_command(
            "podman",
            "run",
            "--pull=never",
            "--detach",
            "--name",
            container_name,
            "--network",
            "none",
            "--volume",
            f"{cluster}:/cluster:idmap={VOLUME_IDMAP}",
            IMAGE,
            "dst-server",
            "agent",
            "master",
        )
        async with rpc_runtime():
            client, status = await wait_for_client(
                cluster / ".dst-server.sock",
                _startup_phase,
            )
            assert [(shard.name, shard.ready) for shard in status.shards] == [
                (MASTER, True)
            ]
            await check_console_contract(client)
            assert not (cluster / "console").exists()
            _, output = await run_command(
                sys.executable,
                "-m",
                "dst_server",
                "--cluster-root",
                str(tmp_path),
                "--json",
                "console",
                "1 + 2",
                "--room",
                "299",
            )
            assert json.loads(output) == [
                {
                    "room": 299,
                    "ok": True,
                    "result": {
                        "output": "",
                        "values": [{"type": "number", "text": "3"}],
                        "error": None,
                        "truncated": False,
                    },
                }
            ]
            _, process_status = await run_command(
                "podman", "exec", container_name, "cat", "/proc/1/status"
            )
            for field in ("Uid:", "Gid:"):
                identity = next(
                    line
                    for line in process_status.splitlines()
                    if line.startswith(field)
                )
                assert identity.split()[1:] == ["1000"] * 4
            socket_metadata = (cluster / ".dst-server.sock").stat()
            assert (socket_metadata.st_uid, socket_metadata.st_gid) == (0, 0)
            _, processes = await run_command("podman", "top", container_name, "args")
            assert GAME_EXECUTABLE in processes
            lifecycle = await client.subscribe_lifecycle()
            stopping = asyncio.create_task(
                wait_for_stopping(lifecycle, frozenset({MASTER}))
            )
            try:
                await run_command(
                    "podman",
                    "stop",
                    "--time",
                    "40",
                    container_name,
                    seconds=OPERATION_TIMEOUT,
                )
                assert await stopping == {MASTER}
            finally:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
            with suppress(Exception):
                await lifecycle.close()
            lifecycle = None
            client.close()
            client = None
        _, exit_code = await run_command(
            "podman",
            "inspect",
            "--format={{.State.ExitCode}}",
            container_name,
        )
        assert exit_code.strip() == "0"
    except BaseException as error:
        _, output = await run_command("podman", "logs", container_name, check=False)
        error.add_note("recent container logs:\n" + output)
        raise
    finally:
        if lifecycle is not None:
            with suppress(Exception):
                await lifecycle.close()
        if client is not None:
            client.close()


async def test_sdk_real_game_core_contract(
    tmp_path: Path,
    container_name: str,
) -> None:
    cluster = write_cluster(tmp_path, configured=True)
    logs: deque[str] = deque(maxlen=200)
    server = make_server(tmp_path, cluster, container_name, log_handler=logs.append)
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            assert server.driver_health.telemetry_status == "active"
            assert (await server.game.invoke(c.Room())).is_dedicated is True
            assert await server.game.invoke(
                c.ExecuteJson(
                    source="return {"
                    "day=TheWorld.topology.overrides.day,"
                    "game_mode=TheNet:GetDefaultGameMode(),"
                    "max_players=TheNet:GetDefaultMaxPlayers(),"
                    "encode_user_path=TheNet:GetDefaultEncodeUserPath()"
                    "}"
                )
            ) == {
                "day": "onlyday",
                "encode_user_path": True,
                "game_mode": "survival",
                "max_players": 16,
            }
            runtime = await server.game.invoke(c.Runtime())
            assert runtime.session_id == server.session_id
            assert await server.game.players.list() == ()
            assert await server.game.invoke(
                c.ExecuteJson(
                    source="local action; "
                    'action=BufferedAction(TheWorld,nil,{id="CHOP",fn=function(self) '
                    "assert(self==action and self.doer==TheWorld); "
                    'return true,"native-action" end}); '
                    "local ok,reason=action:Do(); return {ok=ok,reason=reason}"
                )
            ) == {"ok": True, "reason": "native-action"}
            text = "before\u0085middle\u2028after\u2029"
            assert (
                await server.game.invoke(
                    c.ExecuteJson(source=f"return {lua_string(text)}")
                )
                == text
            )
            with pytest.raises(IndeterminateCommandError) as failure:
                await server.game.invoke(
                    c.ExecuteJson(source='error("injected SDK request failure", 0)')
                )
            assert isinstance(failure.value.__cause__, LuaRequestError)
            assert failure.value.__cause__.code == "lua_error"
            assert (await server.game.invoke(c.Room())).is_dedicated is True
            saved = await server.save(completion_timeout=OPERATION_TIMEOUT)
            assert saved.snapshot is None or saved.snapshot >= runtime.snapshot
            generation = server.lifecycle.session_generation
            await server.game.invoke(c.Reset(timeout=OPERATION_TIMEOUT))
            assert server.lifecycle.session_generation > generation
            assert server.returncode is None
        await server.stop(grace_period=OPERATION_TIMEOUT)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        await reap_server(server, container_name)


async def test_real_game_driver_recovers_after_delayed_session(
    tmp_path: Path,
    container_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = write_cluster(tmp_path)
    logs: deque[str] = deque(maxlen=200)
    server = make_server(tmp_path, cluster, container_name, log_handler=logs.append)
    original_session = server._session_started
    third_session = asyncio.Event()

    def session_started(generation: int) -> None:
        # Delay only the notification to Driver; keep reading the native FD5 pipe.
        if generation == 3:
            third_session.set()
        else:
            original_session(generation)

    monkeypatch.setattr(server, "_session_started", session_started)

    async def drain(reader: Callable[[], Awaitable[object | None]]) -> None:
        while await reader() is not None:
            pass

    consumers = [
        asyncio.create_task(drain(reader))
        for reader in (
            server.read_lifecycle_event,
            server.read_game_event,
            server.read_operational_event,
        )
    ]
    resets: list[asyncio.Task[str]] = []
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        assert server.driver_health.generation == 1
        async with asyncio.timeout(OPERATION_TIMEOUT):
            # Queue both raw resets before the first reload installs its driver.
            resets = [
                asyncio.create_task(
                    server.execute("c_reset()", completion_timeout=OPERATION_TIMEOUT)
                )
                for _ in range(2)
            ]
            await asyncio.gather(*resets)
            await third_session.wait()
            await server.driver.wait_ready()
            assert server.lifecycle.session_generation == 3
            assert server.driver.generation == 2
            assert server.driver_health.generation == 2

            original_session(3)
            assert await server.driver.wait_ready() == 3
            assert server.driver.installed_generation == 3
            assert server.driver_error is None
            health = await server.game.invoke(c.Health())
            assert health.generation == 3
            assert health.telemetry_status == "active"
            assert (await server.game.invoke(c.Room())).is_dedicated is True
            assert (
                await server.game.invoke(c.Runtime())
            ).session_id == server.session_id
            assert server.returncode is None
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        try:
            await reap_server(server, container_name)
        finally:
            for task in (*consumers, *resets):
                task.cancel()
            await asyncio.gather(*consumers, *resets, return_exceptions=True)


@pytest.mark.parametrize("fault", ["telemetry", "core"])
async def test_real_game_driver_degrades_safely(
    tmp_path: Path,
    container_name: str,
    fault: Literal["telemetry", "core"],
) -> None:
    cluster = write_cluster(tmp_path)
    lua_directory = tmp_path / f"{fault}-failure-lua"
    shutil.copytree(ServerConfig(shard="forest").lua_directory, lua_directory)
    if fault == "telemetry":
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
    else:
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
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        await server.start(startup_timeout=STARTUP_TIMEOUT)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            if fault == "telemetry":
                health = server.driver_health
                assert health.telemetry_status == "failed"
                assert health.last_error is not None
                assert health.last_error.stage == "install"
                assert health.last_error.message == "installation_failed"
                diagnostic = await server.read_game_event()
                assert diagnostic is not None
                assert diagnostic.record.event == "dst.telemetry.error"
                assert server.driver_health.telemetry_status == "failed"
                assert (await server.game.invoke(c.Room())).is_dedicated is True
                await server.save(completion_timeout=OPERATION_TIMEOUT)
            else:
                assert server.driver_error is not None
                assert server.driver_error == "DST Lua request failed: lua_error"
                with pytest.raises(RuntimeError, match="has not been installed"):
                    await server.game.invoke(c.Room())
                sentinel = str(ULID())
                assert sentinel in await server.execute(
                    f'print(TheWorld~=nil and {lua_string(sentinel)} or "missing")'
                )
                shutil.copyfile(
                    ServerConfig(shard="forest").lua_directory / "dst_server.lua",
                    lua_directory / "dst_server.lua",
                )
                generation = server.lifecycle.session_generation
                await server.execute("c_reset()")
                await server.game.wait_reload(
                    generation,
                    asyncio.get_running_loop().time() + OPERATION_TIMEOUT,
                )
                assert server.driver_error is None
                assert server.driver_health.telemetry_status == "active"
                assert (await server.game.invoke(c.Room())).is_dedicated is True
            assert server.returncode is None
        await server.stop(grace_period=OPERATION_TIMEOUT)
    except BaseException as error:
        error.add_note("recent game logs:\n" + "\n".join(logs))
        raise
    finally:
        await reap_server(server, container_name)


def available_room_allocation(cluster: ClusterConfig) -> RoomPortAllocation:
    for number in reversed(range(300)):
        allocation = RoomPortAllocation(number=number)
        reservations: list[socket.socket] = []
        try:
            for mapping in allocation.mappings(cluster):
                kind = (
                    socket.SOCK_DGRAM
                    if mapping.protocol == "udp"
                    else socket.SOCK_STREAM
                )
                reservation = socket.socket(socket.AF_INET, kind)
                reservations.append(reservation)
                reservation.bind(
                    ("0.0.0.0", mapping.host)  # ruff: ignore[hardcoded-bind-all-interfaces]
                )
        except OSError:
            continue
        finally:
            for reservation in reservations:
                reservation.close()
        return allocation
    pytest.fail("no free DST room port allocation is available")


async def netdata_player_fields(userid: str) -> dict[str, str]:
    logs = NetdataLogs(config=REPOSITORY_ROOT / "deploy/netdata/otel.yaml")
    async with asyncio.timeout(OPERATION_TIMEOUT):
        while True:
            result = await logs.query(
                NetdataLogQuery(
                    since=datetime.now(UTC) - timedelta(minutes=5),
                    filters=(
                        ("event_name", "dst.player.shard_entered"),
                        ("body.player.userid", userid),
                    ),
                    limit=20,
                ),
                completion_timeout=OPERATION_TIMEOUT,
            )
            if result.records:
                return dict(result.records[0].fields)
            await asyncio.sleep(0.5)


def _shard(status: ClusterStatus, name: str) -> ShardRuntimeStatus:
    return next(shard for shard in status.shards if shard.name == name)


def _startup_phase(
    status: ClusterStatus, expected: Literal["running", "waitingAgents"] = "running"
) -> bool:
    assert status.phase != "failed", (
        f"cluster startup failed: {status.error}\n{status.model_dump_json(indent=2)}"
    )
    return status.phase == expected


async def wait_for_status(
    client: ClusterClient,
    predicate: Callable[[ClusterStatus], bool],
) -> ClusterStatus:
    async with asyncio.timeout(STARTUP_TIMEOUT):
        while True:
            status = await client.status()
            if predicate(status):
                return status
            await asyncio.sleep(0.5)


async def wait_for_client(
    socket_path: Path,
    predicate: Callable[[ClusterStatus], bool],
) -> tuple[ClusterClient, ClusterStatus]:
    async with asyncio.timeout(STARTUP_TIMEOUT):
        while True:
            client: ClusterClient | None = None
            accepted = False
            try:
                client = await ClusterClient.connect(socket_path)
                status = await client.status()
            except OSError, DisconnectedError:
                pass
            else:
                if predicate(status):
                    accepted = True
                    return client, status
            finally:
                if client is not None and not accepted:
                    client.close()
            await asyncio.sleep(0.5)


async def next_matching[RecordT](
    subscription: Subscription[RecordT],
    predicate: Callable[[RecordT], bool],
) -> RecordT:
    async with asyncio.timeout(OPERATION_TIMEOUT):
        while True:
            records = await subscription.next()
            assert records, "subscription closed before the expected record"
            for record in records:
                if predicate(record):
                    return record


async def wait_for_stopping(
    subscription: Subscription[LifecycleRecord],
    expected: frozenset[str],
) -> set[str]:
    found: set[str] = set()
    async with asyncio.timeout(OPERATION_TIMEOUT):
        while not expected.issubset(found):
            records = await subscription.next()
            if not records:
                break
            found.update(
                record.shard
                for record in records
                if isinstance(record.event, server_events.StoppingEvent)
            )
    return found


async def wait_for_game_shards(client: ClusterClient, count: int) -> None:
    async with asyncio.timeout(STARTUP_TIMEOUT):
        while True:
            shards = await client.shard(MASTER).connected_shards()
            if len(shards) == count and all(shard.ready for shard in shards):
                return
            await asyncio.sleep(0.5)


async def emit_player_event(
    client: ClusterClient, events: Subscription[GameEventRecord], shard: str
) -> str:
    userid = str(ULID())
    await client.shard(shard).execute_json(
        "local value=SpawnPrefab('wilson');"
        f"value.userid={lua_string(userid)};"
        "TheWorld:PushEvent('ms_playerjoined',value);"
        "value:Remove();return true"
    )
    event = await next_matching(
        events,
        lambda record: (
            isinstance(record.event, player.ShardEnteredEvent)
            and record.event.data.player.userid == userid
        ),
    )
    assert event.shard == shard
    return userid


async def service_properties(service: str, *properties: str) -> dict[str, str]:
    _, output = await run_command(
        "systemctl",
        "show",
        service,
        *(f"--property={name}" for name in properties),
    )
    return dict(line.split("=", maxsplit=1) for line in output.splitlines())


async def verify_watchdog_notifications(service: str) -> None:
    properties = await service_properties(
        service, "ActiveState", "WatchdogTimestampMonotonic", "NRestarts"
    )
    assert properties["ActiveState"] == "active"
    assert properties["NRestarts"] == "0"
    previous = int(properties["WatchdogTimestampMonotonic"])
    assert previous > 0
    for _ in range(2):
        async with asyncio.timeout(WATCHDOG_TEST_TIMEOUT):
            while True:
                properties = await service_properties(
                    service, "WatchdogTimestampMonotonic", "NRestarts"
                )
                assert properties["NRestarts"] == "0"
                current = int(properties["WatchdogTimestampMonotonic"])
                if current > previous:
                    assert 55_000_000 <= current - previous <= 75_000_000
                    previous = current
                    break
                await asyncio.sleep(1)


@dataclass(slots=True)
class QuadletSystem:
    root: Path
    cluster_dir: Path
    quadlet_dir: Path
    application: QuadletApplication
    installed: list[Path] = field(default_factory=list)

    @classmethod
    def create(cls, root: Path) -> QuadletSystem:
        prefix = f"dst-sdk-test-{str(ULID()).lower()}"
        cluster = fleet_room(
            0,
            token=SecretStr(""),
            cluster_key=SecretStr("quadlet-system-test-key"),
        ).cluster
        cluster = cluster.replace(
            settings=cluster.settings.replace(
                offline_cluster=True,
                lan_only_cluster=True,
                internet_broadcasting_enabled=False,
                pause_when_empty=False,
            )
        )
        allocation = available_room_allocation(cluster)
        rooms = RoomStore(root)
        cluster_dir = rooms.path(allocation.number)
        rooms.save(Room(number=allocation.number, cluster=cluster))
        assert (cluster_dir / "cluster.ini").is_file()
        environment = NETDATA_ENVIRONMENT | {"DST_SERVER_TELEMETRY_PROFILE": "history"}
        if os.environ.get("DST_SERVER_NETDATA_TEST") != "1":
            environment["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] = "http://127.0.0.1:9"
        application = QuadletApplication.for_cluster(
            cluster,
            cluster_dir,
            name=prefix,
            image=IMAGE,
            allocation=allocation,
            telemetry_environment=environment,
            volume_idmap=VOLUME_IDMAP,
        )
        quadlet_dir = root / "quadlet"
        application.save(quadlet_dir)
        for unit in (application.master, *application.secondaries):
            path = quadlet_dir / f"{unit.name}.container"
            saved = ContainerUnit.load(path)
            assert saved.notify is True
            assert saved.watchdog_sec == 300
            assert saved.kill_mode == "control-group"
            assert saved.watchdog_signal == "SIGKILL"
            # Keep the real 60-second notification cadence; only shorten recovery.
            saved.replace(watchdog_sec=WATCHDOG_TEST_TIMEOUT, pull="never").save(
                quadlet_dir
            )
        return cls(root, cluster_dir, quadlet_dir, application)

    @property
    def prefix(self) -> str:
        return self.application.pod.name

    @property
    def pod_service(self) -> str:
        return f"{self.prefix}-pod.service"

    @property
    def master_service(self) -> str:
        return f"{self.application.master.name}.service"

    @property
    def secondary_services(self) -> tuple[str, ...]:
        return tuple(f"{unit.name}.service" for unit in self.application.secondaries)

    @property
    def services(self) -> tuple[str, ...]:
        return (self.master_service, *self.secondary_services, self.pod_service)

    @property
    def pod_name(self) -> str:
        return self.prefix

    def container_name(self, shard: str) -> str:
        return f"{self.prefix}-{shard}"

    @property
    def socket_path(self) -> Path:
        return self.cluster_dir / ".dst-server.sock"

    async def install(self) -> None:
        await asyncio.to_thread(SYSTEM_QUADLET_ROOT.mkdir, parents=True, exist_ok=True)
        for source in (
            f"{self.prefix}.pod",
            f"{self.application.master.name}.container",
            *(f"{unit.name}.container" for unit in self.application.secondaries),
        ):
            target = SYSTEM_QUADLET_ROOT / source
            if target not in self.installed:
                self.installed.append(target)
            await asyncio.to_thread(shutil.copy2, self.quadlet_dir / source, target)
        await run_command("systemctl", "daemon-reload")

    @asynccontextmanager
    async def running(self) -> AsyncIterator[ClusterClient]:
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            await run_command(
                "systemctl", "start", self.master_service, seconds=STARTUP_TIMEOUT
            )
            async with rpc_runtime():
                client, status = await wait_for_client(self.socket_path, _startup_phase)
                with closing(client):
                    assert not status.missing_shards
                    assert {shard.name for shard in status.shards} == set(SHARDS)
                    assert all(shard.ready for shard in status.shards)
                    await wait_for_game_shards(client, len(SHARDS))
                    yield client
        except BaseException as error:
            error.add_note(await self.diagnostics())
            raise

    async def container_id(self, shard: str) -> str:
        _, value = await run_command(
            "podman",
            "inspect",
            "--format={{.Id}}",
            self.container_name(shard),
        )
        return value.strip()

    async def pod_id(self) -> str:
        _, value = await run_command(
            "podman",
            "pod",
            "inspect",
            "--format={{.Id}}",
            self.pod_name,
        )
        return value.strip()

    async def diagnostics(self) -> str:
        _, units = await run_command(
            "systemctl",
            "status",
            "--no-pager",
            *self.services,
            check=False,
        )
        _, containers = await run_command(
            "podman",
            "ps",
            "--all",
            "--pod",
            "--filter",
            f"name={self.prefix}",
            check=False,
        )
        return f"systemd:\n{units}\nPodman:\n{containers}"

    async def cleanup(self) -> None:  # ruff: ignore[complex-structure]
        errors: list[Exception] = []

        async def attempt(*command: str) -> None:
            try:
                await run_command(*command, seconds=OPERATION_TIMEOUT, check=False)
            except Exception as error:  # pragma: no cover - cleanup diagnostics
                errors.append(error)

        await attempt("systemctl", "stop", *self.services)
        await attempt("systemctl", "unmask", "--runtime", *self.secondary_services)
        await attempt("podman", "pod", "rm", "--force", self.pod_name)
        for shard in SHARDS:
            await attempt(
                "podman",
                "rm",
                "--force",
                "--time",
                "0",
                "--ignore",
                self.container_name(shard),
            )
        for path in self.installed:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:  # pragma: no cover - cleanup diagnostics
                errors.append(error)
        await attempt("systemctl", "daemon-reload")
        await attempt("systemctl", "reset-failed", *self.services)
        for kind, name in (
            ("pod", self.pod_name),
            *(("container", self.container_name(shard)) for shard in SHARDS),
        ):
            code, _ = await run_command("podman", kind, "exists", name, check=False)
            if code != 1:
                errors.append(AssertionError(f"Podman {kind} remains: {name}"))
        for service in self.services:
            _, state = await run_command(
                "systemctl",
                "show",
                "--property=LoadState",
                "--value",
                service,
                check=False,
            )
            if state.strip() != "not-found":
                errors.append(AssertionError(f"systemd unit remains: {service}"))
        if errors:
            message = "Quadlet cleanup failed"
            raise ExceptionGroup(message, errors)


@pytest.fixture
async def quadlet_system(tmp_path: Path) -> AsyncIterator[QuadletSystem]:
    if not HAS_QUADLET_RUNTIME:
        pytest.skip("systemd Quadlet runtime is unavailable")
    system = QuadletSystem.create(tmp_path)
    try:
        await system.install()
        yield system
    finally:
        await system.cleanup()


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
            system.socket_path, lambda status: _startup_phase(status, "waitingAgents")
        )
        with closing(observer):
            assert waiting.missing_shards == ("cave",)
            _, processes = await run_command(
                "podman", "top", system.container_name(MASTER), "args"
            )
            assert GAME_EXECUTABLE not in processes
            await verify_watchdog_notifications(system.master_service)
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
            status = await wait_for_status(observer, _startup_phase)
            assert not status.missing_shards
            assert all(shard.ready for shard in status.shards)
            await wait_for_game_shards(observer, len(SHARDS))


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
        bindings = json.loads(pod)[0]["InfraConfig"]["PortBindings"]
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
        assert isinstance(configuration, ConfigurationSnapshot)
        assert configuration.configuration.settings.max_players == 9
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
        assert isinstance(reread, ConfigurationSnapshot)
        assert reread.configuration.settings.max_players == 8


async def test_quadlet_delivers_logs_to_rpc_and_journal_and_player_events(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with (
        system.running() as client,
        await client.subscribe_logs() as logs,
        await client.subscribe_events() as events,
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


async def test_quadlet_game_crash_retries_without_replacing_containers(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with system.running() as client, await client.subscribe_logs() as logs:
        before = await client.status()
        master_before = _shard(before, MASTER)
        cave_before = _shard(before, "cave")
        container_ids = {shard: await system.container_id(shard) for shard in SHARDS}
        assert master_before.pid is not None
        await run_command(
            "podman",
            "exec",
            system.container_name(MASTER),
            "kill",
            "-KILL",
            str(master_before.pid),
        )
        recovered = await wait_for_status(
            client,
            lambda value: (
                value.phase == "running"
                and _shard(value, MASTER).game_attempt != master_before.game_attempt
            ),
        )
        await wait_for_game_shards(client, len(SHARDS))
        assert _shard(recovered, "cave").game_attempt == cave_before.game_attempt
        assert {
            shard: await system.container_id(shard) for shard in SHARDS
        } == container_ids
        marker = f"DST_RETRY_{ULID()}"
        await client.shard(MASTER).execute_json(
            "TheWorld:DoTaskInTime(0,function()"
            f"print({lua_string(marker)}) end);return true"
        )
        await next_matching(logs, lambda record: marker in record.line)


async def test_quadlet_retry_exhaustion_stops_all_games_and_allows_manual_start(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with system.running() as client:
        current = await client.status()
        for _ in range(MAX_ATTEMPTS - _shard(current, MASTER).retry_attempt + 1):
            attempt = _shard(current, MASTER)
            assert attempt.pid is not None
            await run_command(
                "podman",
                "exec",
                system.container_name(MASTER),
                "kill",
                "-KILL",
                str(attempt.pid),
            )
            current = await wait_for_status(
                client,
                lambda value, previous=attempt.game_attempt: (
                    value.phase == "failed"
                    or (
                        value.phase == "running"
                        and _shard(value, MASTER).game_attempt != previous
                    )
                ),
            )
        await wait_for_status(
            client,
            lambda value: (
                value.phase == "failed"
                and value.error == "shard retry budget exhausted"
                and all(shard.pid is None for shard in value.shards)
            ),
        )
        for shard in SHARDS:
            _, processes = await run_command(
                "podman", "top", system.container_name(shard), "args"
            )
            assert GAME_EXECUTABLE not in processes
        await client.start()
        current = await wait_for_status(client, lambda value: value.phase == "running")
        assert all(shard.ready for shard in current.shards)
        await wait_for_game_shards(client, len(SHARDS))


async def test_quadlet_watchdog_reaps_frozen_agent_and_reconnects_event_stream(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    process_fds: list[int] = []
    async with system.running() as client, await client.subscribe_events() as events:
        before = await client.status()
        container_ids = {shard: await system.container_id(shard) for shard in SHARDS}
        pod_id = await system.pod_id()
        incarnations = {shard.name: shard.agent_incarnation for shard in before.shards}
        attempts = {shard.name: shard.game_attempt for shard in before.shards}
        cave_service = f"{system.container_name('cave')}.service"
        properties = await service_properties(cave_service, "NRestarts")
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
            current = await wait_for_status(
                client,
                lambda value: (
                    value.phase == "running"
                    and _shard(value, "cave").agent_incarnation != incarnations["cave"]
                ),
            )
            assert all(select.select([fd], [], [], 0)[0] for fd in process_fds)
        finally:
            for fd in process_fds:
                os.close(fd)
        assert await system.container_id("cave") != container_ids["cave"]
        assert await system.container_id(MASTER) == container_ids[MASTER]
        assert await system.pod_id() == pod_id
        properties = await service_properties(cave_service, "NRestarts")
        assert int(properties["NRestarts"]) == restarts + 1
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
        assert _shard(current, MASTER).agent_incarnation == incarnations[MASTER]
        assert all(
            _shard(current, shard).game_attempt != attempts[shard] for shard in SHARDS
        )
        await wait_for_game_shards(client, len(SHARDS))
        await emit_player_event(client, events, "cave")


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


async def test_quadlet_pod_stop_delivers_stopping_and_reaps_all_containers(
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    async with system.running() as client:
        lifecycle = await client.subscribe_lifecycle()
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
