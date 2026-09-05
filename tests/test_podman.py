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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import SecretStr
from ulid import ULID

sys.path.insert(0, str(Path(__file__).parents[1]))

from dst_server.cluster.config import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.cluster.overrides import WorldgenOverride
from dst_server.cluster.quadlet import QuadletApplication, RoomPortAllocation
from dst_server.cluster.supervisor import MAX_ATTEMPTS
from dst_server.cluster.world import ForestOverrides
from dst_server.events import player
from dst_server.events import server as server_events
from dst_server.game.rpc import lua_string
from dst_server.netdata import NetdataLogQuery, NetdataLogs
from dst_server.rpc import (
    ClusterClient,
    ClusterStatus,
    ConfigurationSnapshot,
    DisconnectedError,
    ErrorCode,
    GameEventRecord,
    LifecycleRecord,
    LogRecord,
    RemoteError,
    ShardRuntimeStatus,
    Subscription,
    rpc_runtime,
)
from dst_server.runtime import Server, ServerConfig
from dst_server.telemetry import TelemetrySettings
from scripts.generate_rooms import NETDATA_ENVIRONMENT, build

REPOSITORY_ROOT = Path(__file__).parents[1]
IMAGE = os.environ.get("DST_SERVER_IMAGE", "")

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
]

GAME_EXECUTABLE = "/install/bin64/dontstarve_dedicated_server_nullrenderer_x64"
STARTUP_TIMEOUT = 300
OPERATION_TIMEOUT = 60
CLEANUP_TIMEOUT = 30
WATCHDOG_TEST_TIMEOUT = 90
SYSTEM_QUADLET_ROOT = Path("/run/containers/systemd")
QUADLET_GENERATOR = Path("/usr/lib/systemd/system-generators/podman-system-generator")
SHARDS = ("cave", "forest")
MASTER = "forest"


@pytest.fixture(scope="session", autouse=True)
def image_matches_expected_build() -> None:
    expected_revision = os.environ.get("DST_SERVER_EXPECTED_REVISION")
    expected_version = os.environ.get("DST_SERVER_EXPECTED_VERSION")
    if expected_revision is None and expected_version is None:
        return
    podman = shutil.which("podman")
    assert podman is not None
    inspected = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true]
        (
            podman,
            "image",
            "inspect",
            "--format",
            (
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
    revision, version = inspected.stdout.strip().split("|", maxsplit=1)
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
        stderr=asyncio.subprocess.STDOUT,
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


def write_cluster(root: Path, *, configured: bool = False) -> Path:
    cluster = root / "cluster"
    ClusterConfig(
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
                ),
                world=(
                    WorldgenOverride(overrides=ForestOverrides(day="onlyday"))
                    if configured
                    else WorldgenOverride.forest()
                ),
            )
        },
    ).save(cluster)
    return cluster


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
        f"{cluster}:/cluster",
        "--volume",
        f"{cluster / 'mods'}:/install/mods",
    ]
    lua_directory = lua_directory or ServerConfig(shard="forest").lua_directory
    command.extend(("--volume", f"{lua_directory}:/dst-server-lua:ro"))
    command.extend(("--entrypoint", GAME_EXECUTABLE, IMAGE))
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


async def test_image_entrypoint_runs_single_shard_and_handles_sigterm(
    tmp_path: Path,
    container_name: str,
) -> None:
    cluster = write_cluster(tmp_path)
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
            f"{cluster}:/cluster",
            IMAGE,
            "dst-server",
            "master",
        )
        async with rpc_runtime():
            client, status = await wait_for_client(
                cluster / ".dst-server.sock",
                lambda value: value.phase == "running",
            )
            assert len(status.shards) == 1
            assert status.shards[0].ready
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
            assert (await server.game.world.room()).is_dedicated is True
            assert await server.game.world.execute(
                "return {"
                "day=TheWorld.topology.overrides.day,"
                "game_mode=TheNet:GetDefaultGameMode(),"
                "max_players=TheNet:GetDefaultMaxPlayers(),"
                "encode_user_path=TheNet:GetDefaultEncodeUserPath()"
                "}"
            ) == {
                "day": "onlyday",
                "encode_user_path": False,
                "game_mode": "survival",
                "max_players": 16,
            }
            runtime = await server.game.world.runtime()
            assert runtime.session_id == server.session_id
            assert await server.game.players.list() == ()
            text = "before\u0085middle\u2028after\u2029"
            assert await server.game.world.execute(f"return {lua_string(text)}") == text
            with pytest.raises(
                RuntimeError, match=r"^DST Lua request failed: lua_error$"
            ):
                await server.game.world.execute(
                    'error("injected SDK request failure", 0)'
                )
            assert (await server.game.world.room()).is_dedicated is True
            saved = await server.save(completion_timeout=OPERATION_TIMEOUT)
            assert saved.snapshot is None or saved.snapshot >= runtime.snapshot
            generation = server.lifecycle.session_generation
            await server.game.world.reset(completion_timeout=OPERATION_TIMEOUT)
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
            health = await server.game.get_health()
            assert health.generation == 3
            assert health.telemetry_status == "active"
            assert (await server.game.world.room()).is_dedicated is True
            assert (await server.game.world.runtime()).session_id == server.session_id
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
                assert (await server.game.world.room()).is_dedicated is True
                await server.save(completion_timeout=OPERATION_TIMEOUT)
            else:
                assert server.driver_error is not None
                assert server.driver_error == "DST Lua request failed: lua_error"
                with pytest.raises(RuntimeError, match="has not been installed"):
                    await server.game.world.room()
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
                assert (await server.game.world.room()).is_dedicated is True
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
                reservation.bind(
                    ("0.0.0.0", mapping.host)  # ruff: ignore[hardcoded-bind-all-interfaces]
                )
                reservations.append(reservation)
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
            try:
                client = await ClusterClient.connect(socket_path)
                status = await client.status()
            except OSError, DisconnectedError:
                if client is not None:
                    client.close()
            else:
                if predicate(status):
                    return client, status
                client.close()
            await asyncio.sleep(0.5)


async def next_matching[RecordT](
    subscription: Subscription[RecordT],
    predicate: Callable[[RecordT], bool],
) -> RecordT:
    async with asyncio.timeout(OPERATION_TIMEOUT):
        while True:
            for record in await subscription.next():
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
    cluster: ClusterConfig
    cluster_dir: Path
    quadlet_dir: Path
    application: QuadletApplication
    installed: list[Path] = field(default_factory=list)

    @classmethod
    def create(cls, root: Path) -> QuadletSystem:
        prefix = f"dst-sdk-test-{str(ULID()).lower()}"
        cluster_dir = root / "cluster"
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
                pause_when_empty=False,
            )
        )
        cluster.save(cluster_dir)
        environment = (
            NETDATA_ENVIRONMENT
            if os.environ.get("DST_SERVER_NETDATA_TEST") == "1"
            else {"OTEL_SDK_DISABLED": "true"}
        ) | {"DST_SERVER_TELEMETRY_PROFILE": "history"}
        application = QuadletApplication.for_cluster(
            cluster,
            cluster_dir,
            name=prefix,
            image=IMAGE,
            allocation=available_room_allocation(cluster),
            telemetry_environment=environment,
        )
        quadlet_dir = root / "quadlet"
        application.save(quadlet_dir)
        for unit in (application.master, *application.secondaries):
            path = quadlet_dir / f"{unit.name}.container"
            contents = path.read_text(encoding="utf-8")
            for directive in (
                "Notify=true",
                "WatchdogSec=300",
                "KillMode=control-group",
                "WatchdogSignal=SIGKILL",
            ):
                assert directive in contents.splitlines()
            assert "HealthCmd=" not in contents
            # Keep the real 60-second notification cadence; only shorten recovery.
            unit.replace(watchdog_sec=WATCHDOG_TEST_TIMEOUT).save(quadlet_dir)
        return cls(root, cluster, cluster_dir, quadlet_dir, application)

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

    async def install(self, *sources: str) -> None:
        await asyncio.to_thread(SYSTEM_QUADLET_ROOT.mkdir, parents=True, exist_ok=True)
        for source in sources:
            target = SYSTEM_QUADLET_ROOT / source
            if target not in self.installed:
                self.installed.append(target)
            await asyncio.to_thread(shutil.copy2, self.quadlet_dir / source, target)
        await run_command("systemctl", "daemon-reload")

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

    async def wait_for_games(self) -> None:
        async with asyncio.timeout(STARTUP_TIMEOUT):
            while True:
                found = []
                for shard in SHARDS:
                    code, processes = await run_command(
                        "podman",
                        "top",
                        self.container_name(shard),
                        "args",
                        check=False,
                    )
                    found.append(code == 0 and GAME_EXECUTABLE in processes)
                if all(found):
                    return
                await asyncio.sleep(0.5)

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
    system = QuadletSystem.create(tmp_path)
    try:
        yield system
    finally:
        await system.cleanup()


@pytest.mark.skipif(
    os.geteuid() != 0
    or not Path("/run/systemd/system").is_dir()
    or not QUADLET_GENERATOR.is_file(),
    reason="systemd Quadlet runtime is unavailable",
)
async def test_quadlet_cluster_lifecycle_and_faults(  # ruff: ignore[complex-structure, too-many-branches, too-many-locals, too-many-statements]
    quadlet_system: QuadletSystem,
) -> None:
    system = quadlet_system
    pod_source = f"{system.prefix}.pod"
    master_source = f"{system.application.master.name}.container"
    secondary_sources = tuple(
        f"{unit.name}.container" for unit in system.application.secondaries
    )
    client: ClusterClient | None = None
    logs: Subscription[LogRecord] | None = None
    events: Subscription[GameEventRecord] | None = None
    lifecycle: Subscription[LifecycleRecord] | None = None
    process_fds: list[int] = []
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        await system.install(pod_source, master_source, *secondary_sources)
        await run_command(
            "systemctl",
            "mask",
            "--runtime",
            *system.secondary_services,
        )
        await run_command(
            "systemctl",
            "start",
            system.master_service,
            seconds=STARTUP_TIMEOUT,
        )

        async with rpc_runtime():
            observer, waiting = await wait_for_client(
                system.socket_path,
                lambda status: status.phase == "waitingAgents",
            )
            try:
                assert waiting.missing_shards == ("cave",)
                _, processes = await run_command(
                    "podman",
                    "top",
                    system.container_name(MASTER),
                    "args",
                )
                assert GAME_EXECUTABLE not in processes
                await verify_watchdog_notifications(system.master_service)
                assert (await observer.status()).phase == "waitingAgents"
            finally:
                observer.close()

            await run_command(
                "systemctl",
                "unmask",
                "--runtime",
                *system.secondary_services,
            )
            await run_command("systemctl", "daemon-reload")
            await run_command(
                "systemctl",
                "start",
                *system.secondary_services,
                seconds=STARTUP_TIMEOUT,
            )
            await system.wait_for_games()

            client, status = await wait_for_client(
                system.socket_path,
                lambda value: value.phase == "running",
            )
            assert all(shard.ready for shard in status.shards)
            assert all(shard.telemetry_profile == "history" for shard in status.shards)
            assert {shard.external_port for shard in status.shards} == {
                mapping.host for mapping in system.application.pod.publish_ports[::2]
            }
            for shard in status.shards:
                _, processes = await run_command(
                    "podman",
                    "top",
                    system.container_name(shard.name),
                    "args",
                )
                game = [
                    line for line in processes.splitlines() if GAME_EXECUTABLE in line
                ]
                assert len(game) == 1
                assert f"-external_port {shard.external_port}" in game[0]
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

            configuration = await client.read_configuration()
            assert isinstance(configuration, ConfigurationSnapshot)
            with pytest.raises(RemoteError) as rejected:
                await client.save_configuration(
                    configuration.revision,
                    configuration.configuration,
                )
            assert rejected.value.error.code is ErrorCode.INVALID_STATE
            assert (await client.shard(MASTER).room()).is_dedicated is True
            assert await client.shard(MASTER).execute_json(
                "return {mode=TheNet:GetDefaultGameMode(),"
                "players=TheNet:GetDefaultMaxPlayers()}"
            ) == {"mode": "survival", "players": 9}
            saved = await client.save()
            assert {name for name, _ in saved.shards} == set(SHARDS)
            await client.stop()
            await wait_for_status(client, lambda value: value.phase == "stopped")
            configuration = await client.read_configuration()
            assert isinstance(configuration, ConfigurationSnapshot)
            desired = configuration.configuration.replace(
                settings=configuration.configuration.settings.replace(max_players=8)
            )
            updated = await client.save_configuration(
                configuration.revision,
                desired,
            )
            assert updated.revision != configuration.revision
            reread = await client.read_configuration()
            assert isinstance(reread, ConfigurationSnapshot)
            assert reread.revision == updated.revision
            assert reread.configuration.settings.max_players == 8
            await client.start()
            await wait_for_status(client, lambda value: value.phase == "running")
            assert (
                await client.shard(MASTER).execute_json(
                    "return TheNet:GetDefaultMaxPlayers()"
                )
                == 8
            )

            logs = await client.subscribe_logs()
            events = await client.subscribe_events()
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
            userid = str(ULID())
            await client.shard(MASTER).execute_json(
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
            assert event.shard == MASTER
            if os.environ.get("DST_SERVER_NETDATA_TEST") == "1":
                fields = await netdata_player_fields(userid)
                assert fields["body.player.userid"] == userid
                assert fields["attributes.dst.cluster.name"] == system.prefix
                assert fields["attributes.dst.shard.name"] == MASTER

            before = status = await client.status()
            master_before = _shard(before, MASTER)
            cave_before = _shard(before, "cave")
            container_ids = {
                shard: await system.container_id(shard) for shard in SHARDS
            }
            pod_id = await system.pod_id()
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
            retry_marker = f"DST_RETRY_{ULID()}"
            await client.shard(MASTER).execute_json(
                "TheWorld:DoTaskInTime(0,function()"
                f"print({lua_string(retry_marker)}) end);return true"
            )
            await next_matching(logs, lambda record: retry_marker in record.line)

            current = recovered
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
            current = await wait_for_status(
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
            current = await wait_for_status(
                client,
                lambda value: value.phase == "running",
            )
            assert all(shard.ready for shard in current.shards)

            container_ids = {
                shard: await system.container_id(shard) for shard in SHARDS
            }
            incarnations = {
                shard.name: shard.agent_incarnation for shard in current.shards
            }
            attempts = {shard.name: shard.game_attempt for shard in current.shards}
            cave_service = f"{system.container_name('cave')}.service"
            properties = await service_properties(cave_service, "NRestarts")
            restarts = int(properties["NRestarts"])
            _, processes = await run_command(
                "podman", "top", system.container_name("cave"), "hpid"
            )
            process_fds.extend(
                os.pidfd_open(int(process.strip()))
                for process in processes.splitlines()[1:]
            )
            assert len(process_fds) >= 2
            frozen_since = f"@{int(datetime.now(UTC).timestamp())}"
            await run_command(
                "podman",
                "kill",
                "--signal",
                "STOP",
                system.container_name("cave"),
            )
            current = await wait_for_status(
                client,
                lambda value: (
                    value.phase == "running"
                    and _shard(value, "cave").agent_incarnation != incarnations["cave"]
                ),
            )
            assert await system.container_id("cave") != container_ids["cave"]
            assert await system.container_id(MASTER) == container_ids[MASTER]
            assert await system.pod_id() == pod_id
            properties = await service_properties(cave_service, "NRestarts")
            assert int(properties["NRestarts"]) == restarts + 1
            assert all(select.select([fd], [], [], 0)[0] for fd in process_fds)
            for fd in process_fds:
                os.close(fd)
            process_fds.clear()
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
                _shard(current, shard).game_attempt != attempts[shard]
                for shard in SHARDS
            )
            cave_userid = str(ULID())
            await client.shard("cave").execute_json(
                "local value=SpawnPrefab('wilson');"
                f"value.userid={lua_string(cave_userid)};"
                "TheWorld:PushEvent('ms_playerjoined',value);"
                "value:Remove();return true"
            )
            replacement_event = await next_matching(
                events,
                lambda record: (
                    isinstance(record.event, player.ShardEnteredEvent)
                    and record.event.data.player.userid == cave_userid
                ),
            )
            assert replacement_event.shard == "cave"

            container_ids = {
                shard: await system.container_id(shard) for shard in SHARDS
            }
            epoch = current.epoch
            await run_command(
                "podman",
                "kill",
                "--signal",
                "KILL",
                system.container_name(MASTER),
            )
            async with asyncio.timeout(STARTUP_TIMEOUT):
                while True:
                    try:
                        await client.status()
                    except DisconnectedError:
                        break
                    await asyncio.sleep(0.5)
            client.close()
            client, current = await wait_for_client(
                system.socket_path,
                lambda value: value.phase == "running",
            )
            await wait_for_game_shards(client, len(SHARDS))
            assert current.epoch != epoch
            assert await system.pod_id() == pod_id
            recovered_ids = await asyncio.gather(
                *(system.container_id(shard) for shard in SHARDS)
            )
            assert all(
                recovered != container_ids[shard]
                for shard, recovered in zip(SHARDS, recovered_ids, strict=True)
            )

            lifecycle = await client.subscribe_lifecycle()
            stopping = asyncio.create_task(
                wait_for_stopping(lifecycle, frozenset(SHARDS))
            )
            try:
                await run_command(
                    "systemctl",
                    "stop",
                    system.pod_service,
                    seconds=OPERATION_TIMEOUT,
                )
                assert await stopping == set(SHARDS)
            finally:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
            with suppress(Exception):
                await lifecycle.close()
            lifecycle = None
            client.close()
            client = None
            for service in (system.master_service, *system.secondary_services):
                _, result = await run_command(
                    "systemctl",
                    "show",
                    "--property=Result",
                    "--property=ExecMainStatus",
                    service,
                )
                assert "Result=success" in result
                assert "ExecMainStatus=0" in result
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
    except BaseException as error:
        error.add_note(await system.diagnostics())
        raise
    finally:
        for fd in process_fds:
            os.close(fd)
        if logs is not None:
            with suppress(Exception):
                await logs.close()
        if events is not None:
            with suppress(Exception):
                await events.close()
        if lifecycle is not None:
            with suppress(Exception):
                await lifecycle.close()
        if client is not None:
            client.close()
