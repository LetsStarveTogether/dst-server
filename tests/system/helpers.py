import asyncio
import os
import shlex
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal, cast
from zipfile import ZipFile

import pytest
from pydantic import JsonValue, SecretStr
from ulid import ULID

from dst_server import commands as c
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
from dst_server.errors import (
    DisconnectedError,
)
from dst_server.events import server as server_events
from dst_server.lua_codec import lua_string
from dst_server.models.cluster import (
    ClusterStatus,
    LifecycleRecord,
    ShardRuntimeStatus,
)
from dst_server.rpc import ClusterClient, Subscription
from dst_server.runtime import Server, ServerConfig
from dst_server.telemetry import TelemetrySettings

REPOSITORY_ROOT = Path(__file__).parents[2]
IMAGE = os.environ.get("DST_SERVER_IMAGE", "")
VOLUME_IDMAP = "uids=0-1000-1;gids=0-1000-1"
SYSTEM_MARKS = [
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
SHARDS = ("cave", "forest")
MASTER = "forest"


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
    script_bundle: Path | None = None,
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
        f"{cluster}:{cluster}:idmap={VOLUME_IDMAP}",
        "--volume",
        f"{cluster / 'mods'}:/install/mods:idmap={VOLUME_IDMAP}",
    ]
    if script_bundle is not None:
        command.extend((
            "--volume",
            f"{script_bundle}:/install/data/databundles/scripts.zip:ro,idmap={VOLUME_IDMAP}",
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
            persistent_storage_root=cluster.parent,
            conf_dir=".",
            cluster=cluster.name,
            ugc_directory=cluster / "mods" / "ugc",
            extra_args=("-skip_update_server_mods", "-offline"),
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
            "health=data.data.health.health,"
            "hunger=data.data.hunger and data.data.hunger.hunger,"
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


async def copy_image_bundle(path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "podman",
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--entrypoint=/bin/cat",
        IMAGE,
        "/install/data/databundles/scripts.zip",
        stdout=asyncio.subprocess.PIPE,
    )
    content = await communicate(process, CLEANUP_TIMEOUT)
    assert process.returncode == 0
    await asyncio.to_thread(path.write_bytes, content)


def replace_bundle_script(bundle: Path, name: str, source: bytes) -> None:
    # Deliberately corrupt this test-only bundle without changing the image.
    temporary = bundle.with_suffix(".tmp")
    with ZipFile(bundle) as original, ZipFile(temporary, "w") as modified:
        assert name in original.namelist()
        for entry in original.infolist():
            modified.writestr(
                entry, source if entry.filename == name else original.read(entry)
            )
    temporary.replace(bundle)


def shard_status(status: ClusterStatus, name: str) -> ShardRuntimeStatus:
    return next(shard for shard in status.shards if shard.name == name)


def startup_phase(
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
