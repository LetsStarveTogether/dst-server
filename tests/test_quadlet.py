from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest
from pydantic import SecretStr, ValidationError

from dst_server.cluster.config import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.cluster.quadlet import (
    CLUSTER_ENVIRONMENT,
    ContainerUnit,
    PodUnit,
    PortMapping,
    QuadletApplication,
    RoomPortAllocation,
    VolumeMount,
)


def make_cluster(*, caves: bool = True) -> ClusterConfig:
    shards = {
        "Master": ShardConfig(settings=ShardSettings(is_master=True, id=1)),
    }
    if caves:
        shards["Caves"] = ShardConfig(
            settings=ShardSettings(
                is_master=False,
                name="Caves",
                id=2,
                master_server_port=27017,
                server_port=11000,
            ),
        )
    return ClusterConfig(
        settings=ClusterSettings(
            master_ip="127.0.0.1" if caves else None,
            cluster_key=SecretStr("test-key") if caves else None,
        ),
        shards=shards,
    )


@pytest.fixture
def cluster() -> ClusterConfig:
    return make_cluster()


@pytest.fixture
def application(tmp_path: Path, cluster: ClusterConfig) -> QuadletApplication:
    return QuadletApplication.for_cluster(
        cluster,
        tmp_path / "007",
        allocation=RoomPortAllocation(number=7),
        telemetry_environment={"DST_SERVER_TELEMETRY_PROFILE": "test"},
    )


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        pytest.param(
            PortMapping(host=2000, container=10999),
            "2000:10999/udp",
            id="udp-port",
        ),
        pytest.param(
            PortMapping(host=2001, container=10999, protocol="tcp"),
            "2001:10999/tcp",
            id="tcp-port",
        ),
        pytest.param(
            VolumeMount(
                source=Path("/srv/dst data"),
                target=PurePosixPath("/cluster"),
                read_only=True,
            ),
            "/srv/dst data:/cluster:ro",
            id="read-only-volume",
        ),
    ],
)
def test_quadlet_values_round_trip(
    value: PortMapping | VolumeMount,
    rendered: str,
) -> None:
    assert value.render() == rendered
    loaded = (
        PortMapping.parse(rendered)
        if isinstance(value, PortMapping)
        else VolumeMount.parse(rendered)
    )
    assert loaded == value


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        pytest.param(
            lambda: PortMapping(host=1023, container=10999),
            "greater than or equal to 1024",
            id="port-below-range",
        ),
        pytest.param(
            lambda: PortMapping(host=65536, container=10999),
            "less than or equal to 65535",
            id="port-above-range",
        ),
        pytest.param(
            lambda: VolumeMount(
                source=Path("relative"),
                target=PurePosixPath("/cluster"),
            ),
            "unsafe Quadlet volume source",
            id="relative-volume",
        ),
        pytest.param(
            lambda: VolumeMount.parse("/srv/dst:/cluster:cached"),
            "volume options",
            id="unknown-volume-option",
        ),
        pytest.param(
            lambda: PodUnit(name="room@invalid"),
            "unsafe Quadlet unit name",
            id="unsafe-unit-name",
        ),
        pytest.param(
            lambda: ContainerUnit(
                name="worker",
                image="image",
                pod="room.pod",
                networks=("host",),
            ),
            "cannot configure its own Network",
            id="pod-network",
        ),
        pytest.param(
            lambda: PodUnit(
                name="room",
                publish_ports=(
                    PortMapping(host=2000, container=10999),
                    PortMapping(host=2000, container=11000),
                ),
            ),
            "conflicting Quadlet port mapping",
            id="conflicting-port",
        ),
        pytest.param(
            lambda: ContainerUnit(
                name="worker",
                image="image",
                volumes=(
                    VolumeMount(
                        source=Path("/one"),
                        target=PurePosixPath("/cluster"),
                    ),
                    VolumeMount(
                        source=Path("/two"),
                        target=PurePosixPath("/cluster"),
                    ),
                ),
            ),
            "duplicate Quadlet Volume target",
            id="duplicate-volume-target",
        ),
    ],
)
def test_quadlet_values_reject_unsafe_or_conflicting_input(
    factory: Callable[[], object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: PodUnit(
                name="room",
                description="Room %n ${HOME}",
                requires=("network-online.target",),
                after=("network-online.target",),
                pod_name="room%n",
                exit_policy="continue",
                networks=("dst-server.network", "bridge"),
                publish_ports=(
                    PortMapping(host=30000, container=10999),
                    PortMapping(host=30001, container=27016),
                ),
                wanted_by=("default.target",),
            ),
            id="pod",
        ),
        pytest.param(
            lambda: ContainerUnit(
                name="worker",
                image="example.invalid/dst:$tag%n",
                description="Worker %n ${HOME}",
                requires=("database.service",),
                wants=("cache.service",),
                binds_to=("master.service",),
                after=("database.service",),
                pod="room.pod",
                exec=("/app/dst-server", "serve", "Cave World %n ${HOME}"),
                environment={"A": "1", "B": "two words %n ${HOME}"},
                volumes=(
                    VolumeMount(
                        source=Path("/srv/dst data"),
                        target=PurePosixPath("/cluster"),
                        read_only=True,
                    ),
                ),
                notify=True,
                watchdog_sec=300,
                kill_mode="control-group",
                watchdog_signal="SIGKILL",
                stop_timeout=40,
                restart="on-failure",
                timeout_stop_sec=50,
                wanted_by=("default.target",),
            ),
            id="container",
        ),
    ],
)
def test_unit_save_load_round_trip(
    tmp_path: Path,
    factory: Callable[[], PodUnit | ContainerUnit],
) -> None:
    unit = factory()
    (path,) = unit.save(tmp_path)
    loaded = (
        PodUnit.load(path) if isinstance(unit, PodUnit) else ContainerUnit.load(path)
    )

    assert loaded == unit
    assert "%%n" in path.read_text(encoding="utf-8")
    assert "$${HOME}" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("seconds", [1, 300])
def test_container_watchdog_uses_native_notify_and_service_seconds(
    tmp_path: Path, seconds: int
) -> None:
    unit = ContainerUnit(
        name="watchdog",
        image="image",
        notify=True,
        watchdog_sec=seconds,
        kill_mode="control-group",
        watchdog_signal="SIGKILL",
        restart="on-failure",
    )
    (path,) = unit.save(tmp_path)
    rendered = path.read_text(encoding="utf-8")

    assert "\nNotify=true\n" in rendered
    assert f"\nWatchdogSec={seconds}\n" in rendered
    assert "\nKillMode=control-group\n" in rendered
    assert "\nWatchdogSignal=SIGKILL\n" in rendered
    assert "Restart=on-failure" in rendered
    assert "Health" not in rendered
    assert ContainerUnit.load(path) == unit


@pytest.mark.parametrize("notify", [None, False])
def test_enabled_watchdog_requires_container_notifications(notify: bool | None) -> None:
    with pytest.raises(ValidationError, match="Notify=true"):
        ContainerUnit(name="watchdog", image="image", notify=notify, watchdog_sec=300)


@pytest.mark.parametrize("notify", [None, False, True])
def test_zero_watchdog_retains_native_disable_semantics(
    tmp_path: Path, notify: bool | None
) -> None:
    unit = ContainerUnit(name="worker", image="image", notify=notify, watchdog_sec=0)
    (path,) = unit.save(tmp_path)

    assert "WatchdogSec=0" in path.read_text(encoding="utf-8")
    assert ContainerUnit.load(path) == unit


def test_default_container_does_not_add_unsolicited_notifications() -> None:
    unit = ContainerUnit(name="worker", image="image")

    assert unit.notify is None
    assert unit.watchdog_sec is None
    assert unit.kill_mode is None
    assert unit.watchdog_signal is None
    assert "Notify=" not in unit.render()
    assert "WatchdogSec=" not in unit.render()
    assert "KillMode=" not in unit.render()
    assert "WatchdogSignal=" not in unit.render()


@pytest.mark.parametrize("value", ["control-group", "mixed", "process", "none"])
def test_container_kill_mode_preserves_native_values(
    tmp_path: Path, value: str
) -> None:
    unit = ContainerUnit.model_validate({
        "name": "worker",
        "image": "image",
        "kill_mode": value,
    })
    (path,) = unit.save(tmp_path)

    assert f"\nKillMode={value}\n" in path.read_text(encoding="utf-8")
    assert ContainerUnit.load(path) == unit


@pytest.mark.parametrize(
    ("field", "value"),
    [("kill_mode", "unknown"), ("watchdog_signal", "SIGKILL\nExecStart=oops")],
)
def test_container_rejects_invalid_native_cleanup_values(
    field: str, value: str
) -> None:
    with pytest.raises(ValidationError, match=field):
        ContainerUnit.model_validate({"name": "worker", "image": "image", field: value})


@pytest.mark.parametrize("value", [-1, True, "300", 1.5])
def test_watchdog_seconds_require_a_nonnegative_integer(value: object) -> None:
    with pytest.raises(ValidationError, match="watchdog_sec"):
        ContainerUnit.model_validate({
            "name": "watchdog",
            "image": "image",
            "notify": True,
            "watchdog_sec": value,
        })


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("yes", True),
        ("1", True),
        ("on", True),
        ("false", False),
        ("no", False),
        ("0", False),
        ("off", False),
    ],
)
def test_notify_loader_uses_existing_systemd_boolean_semantics(
    tmp_path: Path, value: str, expected: bool
) -> None:
    path = tmp_path / "worker.container"
    path.write_text(f"[Container]\nImage=image\nNotify={value}\n", encoding="utf-8")

    assert ContainerUnit.load(path).notify is expected


@pytest.mark.parametrize(
    "setting",
    [
        "HealthCmd=echo ok",
        "HealthInterval=5s",
        "HealthTimeout=3s",
        "HealthRetries=2",
        "HealthStartPeriod=15s",
        "HealthOnFailure=kill",
    ],
)
def test_obsolete_healthcheck_keys_are_rejected(tmp_path: Path, setting: str) -> None:
    path = tmp_path / "worker.container"
    path.write_text(f"[Container]\nImage=image\n{setting}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown Quadlet key"):
        ContainerUnit.load(path)


def test_obsolete_healthcheck_model_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="health"):
        ContainerUnit.model_validate({
            "name": "worker",
            "image": "image",
            "health": {"command": ("echo", "ok")},
        })


@pytest.mark.parametrize(
    ("suffix", "content", "message"),
    [
        pytest.param(
            ".pod",
            "[Pod]\nExitPolicy=stop\nExitPolicy=continue\n",
            "duplicate Quadlet singleton",
            id="duplicate-singleton",
        ),
        pytest.param(
            ".container",
            "[Container]\nImage=image\nPrivileged=true\n",
            "unknown Quadlet key",
            id="unknown-key",
        ),
        pytest.param(
            ".container",
            "[Container]\nImage=image\nExec=echo %n\n",
            "dynamic systemd expansion",
            id="dynamic-expansion",
        ),
    ],
)
def test_unit_load_rejects_invalid_input(
    tmp_path: Path,
    suffix: str,
    content: str,
    message: str,
) -> None:
    path = tmp_path / f"invalid{suffix}"
    path.write_text(content, encoding="utf-8")
    loader = PodUnit.load if suffix == ".pod" else ContainerUnit.load

    with pytest.raises(ValueError, match=message):
        loader(path)


@pytest.mark.parametrize(
    ("number", "offset", "base"),
    [
        pytest.param(0, 0, 30000, id="first-room"),
        pytest.param(7, 10, 30170, id="offset-room"),
        pytest.param(300, -1, 32990, id="last-room"),
    ],
)
def test_room_port_allocation_uses_ten_port_slots(
    cluster: ClusterConfig,
    number: int,
    offset: int,
    base: int,
) -> None:
    mappings = RoomPortAllocation(number=number, offset=offset).mappings(cluster)

    assert tuple(mapping.host for mapping in mappings) == tuple(range(base, base + 4))
    assert tuple(mapping.container for mapping in mappings) == (
        10999,
        27016,
        11000,
        27017,
    )
    assert {mapping.protocol for mapping in mappings} == {"udp"}


@pytest.mark.parametrize(("number", "offset"), [(-1, 0), (299, 1)])
def test_room_port_allocation_rejects_out_of_range_slots(
    number: int,
    offset: int,
) -> None:
    with pytest.raises(ValidationError, match="room port slot"):
        RoomPortAllocation(number=number, offset=offset)


def test_room_port_allocation_supports_at_most_four_shards(
    cluster: ClusterConfig,
) -> None:
    def expanded_cluster(total: int) -> ClusterConfig:
        return cluster.replace(
            shards={
                **cluster.shards,
                **{
                    f"Shard{index}": ShardConfig(
                        settings=ShardSettings(
                            is_master=False,
                            name=f"Shard{index}",
                            id=index + 1,
                            master_server_port=27016 + index,
                            server_port=10999 + index,
                        ),
                    )
                    for index in range(2, total)
                },
            },
        )

    assert len(RoomPortAllocation(number=299).mappings(expanded_cluster(4))) == 8
    with pytest.raises(ValueError, match="at most 4 shards"):
        RoomPortAllocation(number=0).mappings(expanded_cluster(5))


def test_application_builds_master_secondary_lifecycle(
    application: QuadletApplication,
) -> None:
    (secondary,) = application.secondaries
    master_source = f"{application.master.name}.container"

    assert application.pod.pod_name == application.pod.name == "dst-007"
    assert application.master.container_name == application.master.name
    assert secondary.container_name == secondary.name
    assert application.master.wants == (f"{secondary.name}.container",)
    assert application.master.requires == application.master.after == ()
    assert application.master.binds_to == ()
    assert secondary.requires == secondary.wants == ()
    assert secondary.after == secondary.binds_to == (master_source,)
    assert application.master.exec == (
        "/app/.venv/bin/dst-server",
        "master",
        "--external-port",
        "30070",
    )
    assert secondary.exec == (
        "/app/.venv/bin/dst-server",
        "serve",
        "--external-port",
        "30072",
        "--",
        "Caves",
    )
    assert application.master.environment == secondary.environment
    assert application.master.environment[CLUSTER_ENVIRONMENT] == "dst-007"
    assert application.master.environment["DST_SERVER_TELEMETRY_PROFILE"] == "test"
    assert application.master.volumes == secondary.volumes
    assert {
        (
            unit.restart,
            unit.stop_timeout,
            unit.timeout_stop_sec,
            unit.notify,
            unit.watchdog_sec,
            unit.kill_mode,
            unit.watchdog_signal,
        )
        for unit in (application.master, secondary)
    } == {("on-failure", 40, 50, True, 300, "control-group", "SIGKILL")}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        pytest.param(
            lambda app: {"pod": app.pod.replace(networks=("host",))},
            "host network",
            id="host-network",
        ),
        pytest.param(
            lambda app: {"master": app.master.replace(wants=())},
            "invalid master unit",
            id="master-wants",
        ),
        pytest.param(
            lambda app: {
                "secondaries": (app.secondaries[0].replace(after=()),),
            },
            "invalid master binding",
            id="secondary-after",
        ),
        pytest.param(
            lambda app: {
                "secondaries": (app.secondaries[0].replace(binds_to=()),),
            },
            "invalid master binding",
            id="secondary-binds-to",
        ),
    ],
)
def test_application_rejects_invalid_topology(
    application: QuadletApplication,
    change: Callable[[QuadletApplication], dict[str, object]],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        application.replace(**change(application))


@pytest.mark.parametrize(
    ("caves", "allocation", "name"),
    [
        pytest.param(False, None, "solo", id="master-only"),
        pytest.param(
            True,
            RoomPortAllocation(number=7),
            "a@b %n ${HOME}",
            id="master-secondary",
        ),
    ],
)
def test_application_save_load_round_trip(
    tmp_path: Path,
    caves: bool,
    allocation: RoomPortAllocation | None,
    name: str,
) -> None:
    application = QuadletApplication.for_cluster(
        make_cluster(caves=caves),
        tmp_path / "cluster",
        name=name,
        allocation=allocation,
    )
    output = tmp_path / "quadlet"

    written = application.save(output)

    assert set(written) == {output / path for path in application.files()}
    assert QuadletApplication.load(output) == application
    assert QuadletApplication.load(output, name=name) == application
    assert len(application.secondaries) == int(caves)
    assert len(application.pod.publish_ports) == (4 if allocation else 0)


def test_application_rejects_reserved_environment(
    tmp_path: Path,
    cluster: ClusterConfig,
) -> None:
    with pytest.raises(ValueError, match=CLUSTER_ENVIRONMENT):
        QuadletApplication.for_cluster(
            cluster,
            tmp_path / "cluster",
            telemetry_environment={CLUSTER_ENVIRONMENT: "wrong"},
        )


def test_application_save_rejects_stale_member(
    tmp_path: Path,
    application: QuadletApplication,
) -> None:
    output = tmp_path / "quadlet"
    application.save(output)
    (output / f"{application.pod.name}-stale.container").write_text(
        f"[Container]\nImage=image\nPod={application.pod.name}.pod\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unmanaged Quadlet units"):
        application.save(output)


def _external_port(unit: ContainerUnit) -> int | None:
    if "--external-port" not in unit.exec:
        return None
    return int(unit.exec[unit.exec.index("--external-port") + 1])


@pytest.mark.parametrize(
    ("mapping_index", "host", "expected"),
    [
        pytest.param(0, 32000, (32000, 30072), id="change-master"),
        pytest.param(2, 32002, (30070, 32002), id="change-secondary"),
        pytest.param(0, None, (None, 30072), id="unpublish-master"),
        pytest.param(2, None, (30070, None), id="unpublish-secondary"),
    ],
)
def test_application_syncs_changed_player_ports(
    application: QuadletApplication,
    mapping_index: int,
    host: int | None,
    expected: tuple[int | None, int | None],
) -> None:
    mappings = list(application.pod.publish_ports)
    if host is None:
        del mappings[mapping_index]
    else:
        mappings[mapping_index] = mappings[mapping_index].replace(host=host)

    updated = application.replace(
        pod=application.pod.replace(publish_ports=tuple(mappings)),
    )

    assert (
        tuple(_external_port(unit) for unit in (updated.master, *updated.secondaries))
        == expected
    )


def test_application_syncs_new_ports_and_rejects_partial_allocation(
    tmp_path: Path,
    cluster: ClusterConfig,
) -> None:
    application = QuadletApplication.for_cluster(cluster, tmp_path / "cluster")
    mappings = RoomPortAllocation(number=7).mappings(cluster)

    published = application.replace(
        pod=application.pod.replace(publish_ports=mappings),
    )

    assert tuple(
        _external_port(unit) for unit in (published.master, *published.secondaries)
    ) == (30070, 30072)
    with pytest.raises(ValueError, match="partial new mappings"):
        application.replace(
            pod=application.pod.replace(publish_ports=mappings[:1]),
        )


def test_application_does_not_sync_an_explicit_unit(
    application: QuadletApplication,
) -> None:
    first, *remaining = application.pod.publish_ports
    pod = application.pod.replace(
        publish_ports=(first.replace(host=32000), *remaining),
    )

    updated = application.replace(pod=pod, master=application.master)

    assert _external_port(updated.master) == 30070
