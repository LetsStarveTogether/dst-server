from __future__ import annotations

import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]
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

QUADLET_GENERATOR = Path("/usr/lib/systemd/system-generators/podman-system-generator")
SYSTEMD_ANALYZE = Path("/usr/bin/systemd-analyze")


def make_cluster(*, caves: bool = True) -> ClusterConfig:
    shards = {
        "Master": ShardConfig(
            settings=ShardSettings(is_master=True, id=1),
        )
    }
    if caves:
        shards["Caves"] = ShardConfig(
            settings=ShardSettings(
                is_master=False,
                name="Caves",
                id=2,
                master_server_port=27017,
                server_port=11000,
            )
        )
    return ClusterConfig(
        settings=ClusterSettings(
            master_ip="127.0.0.1" if caves else None,
            cluster_key=SecretStr("test-key") if caves else None,
        ),
        shards=shards,
    )


def test_port_and_volume_values_are_typed_and_canonical() -> None:
    mapping = PortMapping(host=2000, container=10999)
    assert mapping.render() == "2000:10999/udp"
    assert PortMapping.parse(mapping.render()) == mapping

    volume = VolumeMount(
        source=Path("/srv/dst/0"),
        target=PurePosixPath("/cluster"),
        read_only=True,
        relabel="z",
    )
    assert volume.render() == "/srv/dst/0:/cluster:ro,z"
    assert VolumeMount.parse(volume.render()) == volume

    with pytest.raises(ValidationError):
        PortMapping(host=1, container=10999)
    with pytest.raises(ValidationError, match="unsafe Quadlet volume source"):
        VolumeMount(source=Path("relative"), target=PurePosixPath("/cluster"))
    with pytest.raises(ValueError, match="volume options"):
        VolumeMount.parse("/srv/dst:/cluster:cached")


def test_pod_unit_repeats_round_trip_and_singletons_do_not(tmp_path: Path) -> None:
    pod = PodUnit(
        name="dst-7",
        description="DST room 7",
        requires=("z.target", "a.target"),
        after=("network-online.target",),
        exit_policy="continue",
        networks=("dst-server.network",),
        publish_ports=(
            PortMapping(host=12007, container=8766),
            PortMapping(host=2007, container=10999),
        ),
        wanted_by=("default.target",),
    )
    written = pod.save(tmp_path)
    assert written == (tmp_path / "dst-7.pod",)
    assert PodUnit.load(written[0]) == pod
    assert pod.render() == (
        "[Unit]\n"
        "Description=DST room 7\n"
        "Requires=z.target\n"
        "Requires=a.target\n"
        "After=network-online.target\n\n"
        "[Pod]\n"
        "ExitPolicy=continue\n"
        "Network=dst-server.network\n"
        "PublishPort=12007:8766/udp\n"
        "PublishPort=2007:10999/udp\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )

    path = tmp_path / "duplicate.pod"
    path.write_text("[Pod]\nExitPolicy=stop\nExitPolicy=continue\n")
    with pytest.raises(ValueError, match="duplicate Quadlet singleton"):
        PodUnit.load(path)


def test_default_units_render_and_round_trip(tmp_path: Path) -> None:
    pod = PodUnit(name="empty")
    assert pod.render() == "[Pod]\n"
    assert PodUnit.load(pod.save(tmp_path)[0]) == pod

    container = ContainerUnit(name="minimal", image="image")
    assert container.render() == "[Container]\nImage=image\n"
    assert ContainerUnit.load(container.save(tmp_path)[0]) == container

    with pytest.raises(ValidationError):
        ContainerUnit(name="invalid", image="two images")
    with pytest.raises(ValidationError):
        ContainerUnit(name="invalid", image="image", pod="two pods.pod")
    with pytest.raises(ValidationError):
        PodUnit(name="invalid", description=" padded ")

    invalid_mapping = PortMapping(host=2000, container=10999).model_copy(
        update={"host": 1},
    )
    with pytest.raises(ValidationError):
        invalid_mapping.render()


def test_container_repeats_and_supported_boolean_spellings_round_trip(
    tmp_path: Path,
) -> None:
    path = tmp_path / "standalone.container"
    path.write_text(
        "[Container]\n"
        "Image=image\n"
        "Network=alpha.network\n"
        "Network=beta.network\n\n"
        "[Service]\n"
        "RemainAfterExit=on\n",
        encoding="utf-8",
    )
    standalone = ContainerUnit.load(path)
    assert standalone.networks == ("alpha.network", "beta.network")
    assert standalone.remain_after_exit is True
    assert ContainerUnit.load(standalone.save(tmp_path)[0]) == standalone


def test_container_unit_loads_repeated_values_and_rejects_unknown_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worker.container"
    path.write_text(
        "[Unit]\n"
        "Requires=prepare.container\n"
        "After=prepare.container\n\n"
        "[Container]\n"
        "Image=example.invalid/dst:latest\n"
        'Pod="room.pod"\n'
        "Exec=/app/.venv/bin/dst-server run 'Cave World'\n"
        "Environment=A=1\n"
        "Environment='B=two words'\n"
        "Volume=/srv/dst:/cluster:z\n"
        "StopTimeout=40\n\n"
        "[Service]\n"
        "Restart=on-failure\n"
        "TimeoutStopSec=50\n\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        encoding="utf-8",
    )

    worker = ContainerUnit.load(path)
    assert worker.pod == "room.pod"
    assert worker.exec[-1] == "Cave World"
    assert worker.environment == {"A": "1", "B": "two words"}
    assert ContainerUnit.load(worker.save(tmp_path)[0]) == worker

    path.write_text("[Container]\nImage=image\nPrivileged=true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown Quadlet key"):
        ContainerUnit.load(path)

    path.write_text("[Unknown]\nValue=1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown Quadlet section"):
        ContainerUnit.load(path)


def test_container_literals_escape_systemd_expansions_and_volume_spaces(
    tmp_path: Path,
) -> None:
    literal = r"value \\ $$ %% \x25n \x24{HOME} %n ${HOME} ' " + "\\"
    container = ContainerUnit(
        name="literal",
        image="registry/image:$tag%n",
        description=literal,
        exec=("command", literal),
        environment={"VALUE": literal},
        volumes=(
            VolumeMount(
                source=Path("/srv/a b%n${HOME}"),
                target=PurePosixPath("/cluster"),
            ),
        ),
        container_name="name$HOME%n",
        networks=("network$HOME%n",),
    )

    rendered = container.render()

    assert "%%n" in rendered
    assert "$${HOME}" in rendered
    assert ContainerUnit.load(container.save(tmp_path)[0]) == container

    path = tmp_path / "dynamic.container"
    path.write_text("[Container]\nImage=image\nExec=echo %n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="dynamic systemd expansion"):
        ContainerUnit.load(path)
    path.write_text(
        "[Container]\nImage=image\nExec=echo '\\x25n'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="dynamic systemd expansion"):
        ContainerUnit.load(path)


@pytest.mark.parametrize("suffix", ["", "\\", "\\\\", "$", "$$", "%", "%%"])
def test_pod_literal_special_character_counts_round_trip(
    tmp_path: Path,
    suffix: str,
) -> None:
    value = f"value{suffix}"
    pod = PodUnit(name="literal-count", pod_name=value)

    assert PodUnit.load(pod.save(tmp_path)[0]) == pod


def test_room_port_allocation_supports_three_hundred_memorable_slots() -> None:
    cluster = make_cluster()

    mappings = RoomPortAllocation(number=7).mappings(cluster)

    assert tuple((item.host, item.container) for item in mappings) == (
        (30007, 10999),
        (30307, 27016),
        (30607, 11000),
        (30907, 27017),
    )
    assert all(item.protocol == "udp" for item in mappings)
    assert RoomPortAllocation(number=7, offset=10).mappings(cluster)[0].host == 30017
    assert RoomPortAllocation(number=7, offset=-7).mappings(cluster)[0].host == 30000
    assert RoomPortAllocation(number=300, offset=-1).mappings(cluster)[0].host == 30299

    ports = [
        mapping.host
        for number in range(300)
        for mapping in RoomPortAllocation(number=number).mappings(cluster)
    ]
    assert len(ports) == len(set(ports)) == 1200

    with pytest.raises(ValidationError, match="room port slot"):
        RoomPortAllocation(number=299, offset=1)
    with pytest.raises(ValidationError):
        RoomPortAllocation(number=-1)


def test_room_port_allocation_rejects_more_than_four_shards() -> None:
    cluster = make_cluster()
    cluster = cluster.replace(
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
                    )
                )
                for index in range(2, 5)
            },
        },
    )

    supported = cluster.replace(
        shards={
            name: shard for name, shard in cluster.shards.items() if name != "Shard4"
        }
    )
    assert (
        max(
            mapping.host
            for mapping in RoomPortAllocation(number=299).mappings(supported)
        )
        == 32399
    )

    with pytest.raises(ValueError, match="at most 4 shards"):
        RoomPortAllocation(number=0).mappings(cluster)


def test_application_generates_one_prepare_and_one_worker_per_shard(
    tmp_path: Path,
) -> None:
    cluster = make_cluster()
    app = QuadletApplication.for_cluster(
        cluster,
        tmp_path / "cluster",
        name="dst-room-7",
        allocation=RoomPortAllocation(number=7),
        telemetry_environment={
            "DST_SERVER_TELEMETRY_PROFILE": "history",
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://host.containers.internal:4317",
        },
    )

    assert app.pod.exit_policy == "continue"
    assert app.pod.networks == ()
    assert len(app.pod.publish_ports) == 4
    assert app.prepare.exec == ("/app/.venv/bin/dst-server", "prepare")
    assert app.prepare.environment == {}
    assert app.prepare.networks == ()
    assert [worker.exec[-1] for worker in app.workers] == ["Caves", "Master"]
    assert {worker.exec[-1]: worker.exec[3] for worker in app.workers} == {
        "Caves": "30607",
        "Master": "30007",
    }
    for worker in app.workers:
        assert worker.exec[:3] == (
            "/app/.venv/bin/dst-server",
            "run",
            "--external-port",
        )
        assert worker.exec[-2] == "--"
        assert worker.environment[CLUSTER_ENVIRONMENT] == "dst-room-7"
        assert worker.environment["DST_SERVER_TELEMETRY_PROFILE"] == "history"
        assert worker.requires == worker.after == ()
        assert worker.stop_timeout == 40
        assert worker.timeout_stop_sec == 50
        assert worker.restart == "on-failure"
        assert worker.wanted_by == ()
        assert worker.networks == ()
        assert worker.volumes[0].target == PurePosixPath("/cluster")
        assert worker.volumes[0].relabel == "z"
    assert app.pod.wanted_by == ("default.target",)
    assert app.prepare.wanted_by == ()


def test_application_rejects_topology_that_cannot_gate_pod_start(
    tmp_path: Path,
) -> None:
    app = QuadletApplication.for_cluster(make_cluster(), tmp_path / "cluster")

    with pytest.raises(ValidationError, match="transient standalone oneshot"):
        app.replace(prepare=app.prepare.replace(pod=f"{app.pod.name}.pod"))
    with pytest.raises(ValidationError, match="Pod does not depend"):
        app.replace(pod=app.pod.replace(requires=()))
    for policy in ("always", "on-success"):
        with pytest.raises(ValidationError, match="oneshot services cannot restart"):
            ContainerUnit(
                name="invalid-prepare",
                image="image",
                service_type="oneshot",
                restart=policy,
            )


def test_application_replace_syncs_player_port_unless_workers_are_explicit(
    tmp_path: Path,
) -> None:
    application = QuadletApplication.for_cluster(
        make_cluster(),
        tmp_path / "cluster",
        allocation=RoomPortAllocation(number=7),
    )
    master_player, *unchanged_ports = application.pod.publish_ports
    pod = application.pod.replace(
        publish_ports=(master_player.replace(host=32_007), *unchanged_ports),
    )

    updated = application.replace(pod=pod)

    assert tuple(mapping.host for mapping in updated.pod.publish_ports) == (
        32_007,
        30_307,
        30_607,
        30_907,
    )
    assert {worker.exec[-1]: worker.exec[3] for worker in updated.workers} == {
        "Caves": "30607",
        "Master": "32007",
    }
    assert updated.workers[0] == application.workers[0]
    assert (
        application.replace(pod=pod, workers=application.workers).workers
        == application.workers
    )

    without_master_player = updated.replace(
        pod=updated.pod.replace(publish_ports=updated.pod.publish_ports[1:])
    )
    assert "--external-port" not in without_master_player.workers[1].exec
    assert without_master_player.workers[0] == updated.workers[0]

    tcp = PortMapping(host=32_007, container=40_000, protocol="tcp")
    with_tcp = updated.replace(
        pod=updated.pod.replace(publish_ports=(*updated.pod.publish_ports, tcp)),
        workers=updated.workers,
    )
    assert (
        with_tcp.replace(
            pod=with_tcp.pod.replace(
                publish_ports=(*updated.pod.publish_ports, tcp.replace(host=32_008))
            )
        ).workers
        == updated.workers
    )


@pytest.mark.skipif(
    not QUADLET_GENERATOR.is_file() or not SYSTEMD_ANALYZE.is_file(),
    reason="Podman Quadlet generator or systemd-analyze is unavailable",
)
def test_generator_preserves_literal_backslashes_and_expansions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    generated = tmp_path / "generated"
    generated.mkdir()
    value = r"\x25n \x24{HOME} \\ $$ %% %n ${HOME}"
    ContainerUnit(
        name="literal",
        image="quay.io/example/image",
        exec=("/bin/echo", value),
        environment={"VALUE": value},
    ).save(source)

    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        (str(QUADLET_GENERATOR), str(generated), str(generated), str(generated)),
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | {"QUADLET_UNIT_DIRS": str(source)},
    )
    service = generated / "literal.service"
    text = service.read_text(encoding="utf-8")
    exec_start = next(
        line for line in text.splitlines() if line.startswith("ExecStart=")
    )

    assert r"\\x25n\x20\\x24{HOME}" in exec_start
    assert r"%%n\x20$${HOME}" in exec_start
    assert r"--env VALUE=%n" not in exec_start
    assert r"--env VALUE=${HOME}" not in exec_start
    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        (str(SYSTEMD_ANALYZE), "verify", str(service)),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(
    not QUADLET_GENERATOR.is_file(),
    reason="Podman Quadlet generator is unavailable",
)
def test_podman_generator_orders_standalone_prepare_before_pod(
    tmp_path: Path,
) -> None:
    cluster = make_cluster(caves=False)
    shard = next(iter(cluster.shards.values()))
    cluster = cluster.replace(shards={"odd %n ${HOME}": shard})
    application = QuadletApplication.for_cluster(
        cluster,
        Path("/srv/dst/review %n ${HOME}"),
        name="dst-review",
    )
    application.save(tmp_path)

    generated = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        (str(QUADLET_GENERATOR), "--dryrun"),
        check=True,
        capture_output=True,
        text=True,
        env=os.environ | {"QUADLET_UNIT_DIRS": str(tmp_path)},
    ).stdout
    pod = generated.partition("---dst-review-pod.service---\n")[2].partition("\n---")[0]
    prepare = generated.partition("---dst-review-prepare.service---\n")[2].partition(
        "\n---"
    )[0]

    assert "dst-server-network" not in generated
    assert "Requires=dst-review-prepare.service\n" in pod
    assert "After=dst-review-prepare.service\n" in pod
    assert r"Before=dst-review-odd\x20\x25n\x20\x24\x7bHOME\x7d.service" in pod
    assert "BindsTo=dst-review-pod.service\n" not in prepare
    assert "Type=oneshot\n" in prepare
    assert "RemainAfterExit=" not in prepare
    assert "review\\x20%%n\\x20$${HOME}" in generated
    assert "odd\\x20%%n\\x20$${HOME}" in generated


def test_application_defaults_to_no_port_or_otel_exporter(tmp_path: Path) -> None:
    app = QuadletApplication.for_cluster(
        make_cluster(caves=False),
        tmp_path / "42",
    )

    assert app.pod.name == "dst-42"
    assert app.pod.publish_ports == ()
    assert app.workers[0].environment == {CLUSTER_ENVIRONMENT: "dst-42"}

    with pytest.raises(ValueError, match=CLUSTER_ENVIRONMENT):
        QuadletApplication.for_cluster(
            make_cluster(caves=False),
            tmp_path / "42",
            telemetry_environment={CLUSTER_ENVIRONMENT: "wrong"},
        )

    escaped = QuadletApplication.for_cluster(
        make_cluster(caves=False),
        tmp_path / "42",
        name="a@b %n ${HOME}",
    )
    assert escaped.pod.name == r"a\x40b\x20\x25n\x20\x24\x7bHOME\x7d"
    assert escaped.workers[0].environment[CLUSTER_ENVIRONMENT] == "a@b %n ${HOME}"
    escaped_output = tmp_path / "escaped"
    escaped.save(escaped_output)
    assert QuadletApplication.load(escaped_output, name="a@b %n ${HOME}") == escaped
    with pytest.raises(ValidationError, match="unsafe Quadlet unit name"):
        PodUnit(name="a@b")


def test_application_files_save_load_and_stale_unit_guard(tmp_path: Path) -> None:
    application = QuadletApplication.for_cluster(
        make_cluster(),
        tmp_path / "cluster",
        name="room",
        allocation=RoomPortAllocation(number=0),
    )
    output = tmp_path / "quadlet"

    written = application.save(output)

    assert set(written) == {output / path for path in application.files()}
    assert QuadletApplication.load(output) == application
    assert QuadletApplication.load(output, name="room") == application

    (output / "other.container").write_text(
        "[Container]\nImage=image\nUnknownFromFuture=true\n",
        encoding="utf-8",
    )
    assert QuadletApplication.load(output, name="room") == application
    (output / "unrelated.pod").write_text("[Pod]\n", encoding="utf-8")
    assert QuadletApplication.load(output, name="room") == application

    stale = output / "unrelated-name.container"
    stale.write_text(
        '[Container]\nImage=image\nPod="room.pod"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unmanaged Quadlet units"):
        application.save(output)


def test_unit_models_reject_conflicting_repeat_values() -> None:
    with pytest.raises(ValidationError, match="conflicting Quadlet port mapping"):
        PodUnit(
            name="room",
            publish_ports=(
                PortMapping(host=2000, container=10999),
                PortMapping(host=2000, container=11000),
            ),
        )
    with pytest.raises(ValidationError, match="duplicate Quadlet Volume target"):
        ContainerUnit(
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
        )
