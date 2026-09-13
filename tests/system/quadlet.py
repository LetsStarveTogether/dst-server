import asyncio
import os
import shutil
import socket
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager, closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from ulid import ULID

from dst_server.configuration.models import (
    ClusterConfig,
)
from dst_server.deployment import QuadletApplication, RoomPortAllocation
from dst_server.events import player
from dst_server.logs import NetdataLogQuery, NetdataLogs
from dst_server.lua_codec import lua_string
from dst_server.models.cluster import (
    ClusterStatus,
    GameEventRecord,
)
from dst_server.presets.lst import NETDATA_ENVIRONMENT, fleet_room
from dst_server.rooms import Room, RoomStore
from dst_server.rpc import ClusterClient, Subscription, rpc_runtime
from tests.system.helpers import (
    IMAGE,
    OPERATION_TIMEOUT,
    REPOSITORY_ROOT,
    SHARDS,
    STARTUP_TIMEOUT,
    VOLUME_IDMAP,
    next_matching,
    run_command,
    startup_phase,
    wait_for_client,
    wait_for_game_shards,
)

WATCHDOG_TEST_TIMEOUT = 90
SYSTEM_QUADLET_ROOT = Path("/run/containers/systemd")
QUADLET_GENERATOR = Path("/usr/lib/systemd/system-generators/podman-system-generator")
HAS_QUADLET_RUNTIME = (
    Path("/run/systemd/system").is_dir() and QUADLET_GENERATOR.is_file()
)


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


@dataclass(frozen=True, slots=True)
class RoomState:
    status: ClusterStatus
    pod_id: str
    containers: dict[str, str]
    restarts: dict[str, int]


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
            environment.update(
                OTEL_EXPORTER_OTLP_LOGS_ENDPOINT="http://127.0.0.1:9",
                OTEL_EXPORTER_OTLP_METRICS_ENDPOINT="http://127.0.0.1:9",
                OTEL_EXPORTER_OTLP_TIMEOUT="1",
            )
        application = QuadletApplication.for_cluster(
            cluster,
            cluster_dir,
            name=prefix,
            image=IMAGE,
            allocation=allocation,
            telemetry_environment=environment,
            volume_idmap=VOLUME_IDMAP,
        )
        # Keep the real 60-second notification cadence; only shorten recovery.
        application = application.replace(
            master=application.master.replace(
                restart_sec=1, watchdog_sec=WATCHDOG_TEST_TIMEOUT, pull="never"
            ),
            secondaries=tuple(
                unit.replace(watchdog_sec=WATCHDOG_TEST_TIMEOUT, pull="never")
                for unit in application.secondaries
            ),
        )
        quadlet_dir = root / "quadlet"
        application.save(quadlet_dir)
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
                client, status = await wait_for_client(self.socket_path, startup_phase)
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

    async def state(self, client: ClusterClient) -> RoomState:
        return RoomState(
            status=await client.status(),
            pod_id=await self.pod_id(),
            containers={shard: await self.container_id(shard) for shard in SHARDS},
            restarts={
                service: int(
                    (await service_properties(service, "NRestarts"))["NRestarts"]
                )
                for service in (self.master_service, *self.secondary_services)
            },
        )

    @asynccontextmanager
    async def track_processes(self) -> AsyncIterator[tuple[int, ...]]:
        with ExitStack() as stack:
            descriptors: list[int] = []
            for shard in SHARDS:
                _, processes = await run_command(
                    "podman", "top", self.container_name(shard), "hpid"
                )
                pids = [int(line.strip()) for line in processes.splitlines()[1:]]
                assert len(pids) >= 2, f"missing SDK or game process in {shard}"
                for pid in pids:
                    descriptor = os.pidfd_open(pid)
                    stack.callback(os.close, descriptor)
                    descriptors.append(descriptor)
            yield tuple(descriptors)

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
