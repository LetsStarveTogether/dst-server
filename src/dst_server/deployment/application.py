import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Self

from pydantic import model_validator

from dst_server.configuration.files import write_files
from dst_server.configuration.models import ClusterConfig, ShardConfig
from dst_server.models.base import RevalidatedFrozenModel

from .models import (
    ContainerUnit,
    PodUnit,
    PortMapping,
    VolumeMount,
    _validate_unit_name,
)
from .quadlet import references_pod

DEFAULT_IMAGE = "quay.io/wh2099/dst-server:latest"
DEFAULT_TARGET = "default.target"
MAX_ROOM_SLOT = 299
MAX_ROOM_SHARDS = 4
ROOM_PORTS_PER_SLOT = 10
SERVE_COMMAND = ("/app/.venv/bin/dst-server", "serve")
MASTER_COMMAND = ("/app/.venv/bin/dst-server", "master")
CLUSTER_ENVIRONMENT = "DST_SERVER_CLUSTER_NAME"


def _escape_unit_name(value: str) -> str:
    if not value or any(character in value for character in "\0\r\n"):
        msg = f"unsafe Quadlet application name: {value!r}"
        raise ValueError(msg)
    encoded = value.encode("utf-8")
    escaped = "".join(
        chr(byte)
        if (chr(byte).isascii() and (chr(byte).isalnum() or chr(byte) in ":_.-"))
        else f"\\x{byte:02x}"
        for byte in encoded
    )
    if escaped.startswith("."):
        escaped = f"\\x2e{escaped[1:]}"
    return _validate_unit_name(escaped)


def _podman_name(value: str) -> str | None:
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) else None


def _ordered_shards(
    cluster: ClusterConfig,
) -> tuple[tuple[str, ShardConfig], ...]:
    return tuple(
        sorted(
            cluster.shards.items(),
            key=lambda item: (
                not item[1].settings.is_master,
                item[0].casefold(),
            ),
        )
    )


class RoomPortAllocation(RevalidatedFrozenModel):
    number: int
    offset: int = 0

    @model_validator(mode="after")
    def _validate_slot(self) -> Self:
        if not 0 <= self.number + self.offset <= MAX_ROOM_SLOT:
            msg = f"room port slot must be between 0 and {MAX_ROOM_SLOT}"
            raise ValueError(msg)
        return self

    def mappings(self, cluster: ClusterConfig) -> tuple[PortMapping, ...]:
        validated = ClusterConfig.model_validate(cluster)
        shards = _ordered_shards(validated)
        if len(shards) > MAX_ROOM_SHARDS:
            msg = f"room port allocation supports at most {MAX_ROOM_SHARDS} shards"
            raise ValueError(msg)
        slot = self.number + self.offset
        base = 30000 + ROOM_PORTS_PER_SLOT * slot
        return tuple(
            PortMapping(
                host=base + 2 * ordinal + port_offset,
                container=container,
            )
            for ordinal, (_, shard) in enumerate(shards)
            for port_offset, container in enumerate((
                shard.settings.server_port,
                shard.settings.master_server_port,
            ))
        )


def _sync_external_port(
    unit: ContainerUnit,
    replacements: Mapping[int, int | None],
    addition: int | None,
) -> ContainerUnit:
    command = list(unit.exec)
    try:
        index = command.index("--external-port") + 1
    except ValueError:
        if addition is None:
            return unit
        index = command.index("--") if "--" in command else len(command)
        command[index:index] = ("--external-port", str(addition))
    else:
        try:
            previous = int(command[index])
        except ValueError, IndexError:
            return unit
        if previous not in replacements and addition is None:
            return unit
        replacement = replacements.get(previous, addition)
        if replacement is None:
            del command[index - 1 : index + 1]
        else:
            command[index] = str(replacement)
    return unit.replace(exec=tuple(command))


class QuadletApplication(RevalidatedFrozenModel):
    pod: PodUnit
    master: ContainerUnit
    secondaries: tuple[ContainerUnit, ...] = ()

    @model_validator(mode="after")
    def _validate_topology(self) -> Self:
        pod_source = f"{self.pod.name}.pod"
        if "host" in self.pod.networks:
            msg = "Quadlet application cannot use host network"
            raise ValueError(msg)
        secondary_names = tuple(unit.name for unit in self.secondaries)
        if secondary_names != tuple(sorted(secondary_names)):
            msg = "Quadlet secondaries must use canonical unit-name order"
            raise ValueError(msg)
        if f"{self.pod.name}-pod" in {self.master.name, *secondary_names}:
            msg = "Quadlet container unit conflicts with the pod service"
            raise ValueError(msg)
        master_source = f"{self.master.name}.container"
        secondary_sources = tuple(f"{name}.container" for name in secondary_names)
        if any((
            self.master.pod != pod_source,
            self.master.exec[: len(MASTER_COMMAND)] != MASTER_COMMAND,
            bool(self.master.requires),
            bool(self.master.binds_to),
            bool(self.master.after),
            self.master.wants != secondary_sources,
        )):
            msg = "Quadlet application has an invalid master unit"
            raise ValueError(msg)
        names = {self.master.name}
        for secondary in self.secondaries:
            if secondary.name in names:
                msg = f"duplicate Quadlet container unit: {secondary.name}"
                raise ValueError(msg)
            names.add(secondary.name)
            if secondary.pod != pod_source:
                msg = (
                    f"Quadlet secondary is not a member of {pod_source}: "
                    f"{secondary.name}"
                )
                raise ValueError(msg)
            if any((
                secondary.exec[: len(SERVE_COMMAND)] != SERVE_COMMAND,
                bool(secondary.requires),
                bool(secondary.wants),
                secondary.binds_to != (master_source,),
                secondary.after != (master_source,),
            )):
                msg = f"Quadlet secondary has invalid master binding: {secondary.name}"
                raise ValueError(msg)
        return self

    def replace(self, **changes: object) -> Self:
        if "pod" in changes:
            pod = PodUnit.model_validate(changes["pod"])
            units = (self.master, *self.secondaries)
            old_udp = tuple(
                mapping
                for mapping in self.pod.publish_ports
                if mapping.protocol == "udp"
            )
            new_udp = tuple(
                mapping for mapping in pod.publish_ports if mapping.protocol == "udp"
            )
            new_hosts = {mapping.container: mapping.host for mapping in new_udp}
            replacements = {
                mapping.host: new_hosts.get(mapping.container)
                for mapping in old_udp
                if mapping.host != new_hosts.get(mapping.container)
            }
            additions: dict[str, int] = {}
            if not old_udp:
                implicit_units = (() if "master" in changes else (self.master,)) + (
                    () if "secondaries" in changes else self.secondaries
                )
                if new_udp and len(new_udp) != 2 * len(units) and implicit_units:
                    msg = "cannot infer player ports from partial new mappings"
                    raise ValueError(msg)
                if len(new_udp) == 2 * len(units):
                    additions = {
                        unit.name: mapping.host
                        for unit, mapping in zip(units, new_udp[::2], strict=True)
                    }
            if replacements or additions:
                if "master" not in changes:
                    changes["master"] = _sync_external_port(
                        self.master,
                        replacements,
                        additions.get(self.master.name),
                    )
                if "secondaries" not in changes:
                    changes["secondaries"] = tuple(
                        _sync_external_port(
                            unit,
                            replacements,
                            additions.get(unit.name),
                        )
                        for unit in self.secondaries
                    )
        return super().replace(**changes)

    @classmethod
    def for_cluster(
        cls,
        cluster: ClusterConfig,
        cluster_path: Path,
        *,
        name: str | None = None,
        image: str = DEFAULT_IMAGE,
        allocation: RoomPortAllocation | None = None,
        telemetry_environment: Mapping[str, str] | None = None,
        volume_idmap: str | None = None,
        userns: str | None = None,
    ) -> Self:
        validated = ClusterConfig.model_validate(cluster)
        logical_name = name or f"dst-{cluster_path.name.removeprefix('dst-')}"
        base = _escape_unit_name(logical_name)
        pod_source = f"{base}.pod"
        volume = VolumeMount(
            source=cluster_path.absolute(),
            target=PurePosixPath("/cluster"),
            idmap=volume_idmap,
        )
        publish_ports = allocation.mappings(validated) if allocation else ()
        published_hosts = {mapping.container: mapping.host for mapping in publish_ports}
        pod = PodUnit(
            name=base,
            description=f"Don't Starve Together {logical_name}",
            pod_name=_podman_name(base),
            userns=userns,
            exit_policy="continue",
            publish_ports=publish_ports,
            wanted_by=(DEFAULT_TARGET,),
        )
        environment = dict(telemetry_environment or {})
        if environment.get(CLUSTER_ENVIRONMENT, logical_name) != logical_name:
            msg = f"{CLUSTER_ENVIRONMENT} is managed by QuadletApplication"
            raise ValueError(msg)
        environment[CLUSTER_ENVIRONMENT] = logical_name
        master_name = next(
            name for name, shard in validated.shards.items() if shard.settings.is_master
        )
        secondary_names = sorted(
            (name for name in validated.shards if name != master_name),
            key=_escape_unit_name,
        )
        master_unit_name = f"{base}-{_escape_unit_name(master_name)}"
        master = ContainerUnit(
            name=master_unit_name,
            description=f"Don't Starve Together master shard {master_name}",
            exec=(
                *MASTER_COMMAND,
                *(
                    (
                        "--external-port",
                        str(
                            published_hosts[
                                validated.shards[master_name].settings.server_port
                            ]
                        ),
                    )
                    if published_hosts
                    else ()
                ),
            ),
            environment=environment,
            image=image,
            pull="always",
            pod=pod_source,
            volumes=(volume,),
            container_name=_podman_name(master_unit_name),
            wants=tuple(
                f"{base}-{_escape_unit_name(name)}.container"
                for name in secondary_names
            ),
            stop_timeout=360,
            notify=True,
            restart="on-failure",
            kill_mode="control-group",
            watchdog_sec=300,
            watchdog_signal="SIGKILL",
            timeout_start_sec=1800,
            timeout_stop_sec=420,
        )
        secondaries = tuple(
            master.replace(
                name=f"{base}-{_escape_unit_name(shard_name)}",
                description=f"Don't Starve Together shard {shard_name}",
                exec=(
                    *SERVE_COMMAND,
                    *(
                        (
                            "--external-port",
                            str(
                                published_hosts[
                                    validated.shards[shard_name].settings.server_port
                                ]
                            ),
                        )
                        if published_hosts
                        else ()
                    ),
                    "--",
                    shard_name,
                ),
                after=(f"{master.name}.container",),
                binds_to=(f"{master.name}.container",),
                wants=(),
                container_name=_podman_name(f"{base}-{_escape_unit_name(shard_name)}"),
            )
            for shard_name in secondary_names
        )
        return cls(pod=pod, master=master, secondaries=secondaries)

    @classmethod
    def load(cls, directory: Path, *, name: str | None = None) -> Self:
        if directory.is_symlink():
            msg = f"Quadlet directory cannot be a symlink: {directory}"
            raise ValueError(msg)
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        if name is None:
            pods = tuple(sorted(directory.glob("*.pod")))
            if len(pods) != 1:
                msg = f"expected exactly one Quadlet pod, found {len(pods)}"
                raise ValueError(msg)
            pod_path = pods[0]
        else:
            pod_path = directory / f"{_escape_unit_name(name)}.pod"
        pod = PodUnit.load(pod_path)
        pod_source = pod_path.name
        units = tuple(
            ContainerUnit.load(path)
            for path in sorted(directory.glob("*.container"))
            if references_pod(path, pod_source)
        )
        masters = tuple(
            unit for unit in units if unit.exec[: len(MASTER_COMMAND)] == MASTER_COMMAND
        )
        if len(masters) != 1:
            msg = f"expected exactly one Quadlet master, found {len(masters)}"
            raise ValueError(msg)
        master = masters[0]
        return cls(
            pod=pod,
            master=master,
            secondaries=tuple(unit for unit in units if unit is not master),
        )

    def files(self) -> dict[Path, str]:
        validated = type(self).model_validate(self)
        files = {Path(f"{validated.pod.name}.pod"): validated.pod.render()}
        files[Path(f"{validated.master.name}.container")] = validated.master.render()
        files.update({
            Path(f"{unit.name}.container"): unit.render()
            for unit in validated.secondaries
        })
        return files

    def save(self, directory: Path) -> tuple[Path, ...]:
        files = self.files()
        if directory.is_dir() and not directory.is_symlink():
            pod_source = f"{self.pod.name}.pod"
            unexpected = sorted(
                path.name
                for path in directory.glob("*.container")
                if Path(path.name) not in files and references_pod(path, pod_source)
            )
            if unexpected:
                msg = f"unmanaged Quadlet units would remain active: {unexpected}"
                raise ValueError(msg)
        return write_files(directory, files)
