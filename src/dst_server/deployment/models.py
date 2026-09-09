import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, ClassVar, Literal, Self, cast

from pydantic import AfterValidator, Field, model_validator

from dst_server.configuration.files import write_files
from dst_server.configuration.overrides import FrozenMapping
from dst_server.models.base import RevalidatedFrozenModel

type Port = Annotated[int, Field(ge=1024, le=65535)]
type Seconds = Annotated[int, Field(ge=0)]
type UnitValue = Annotated[str, Field(pattern=r"^[^\x00\r\n]*$")]
type NonEmptyUnitValue = Annotated[
    str,
    Field(min_length=1, pattern=r"^[^\x00\r\n]*$"),
]
type UnitToken = Annotated[
    str,
    Field(min_length=1, pattern=r"^[^\s\x00\r\n]+$"),
]
type EnvironmentName = Annotated[
    str,
    Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]
type RestartPolicy = Literal[
    "no",
    "always",
    "on-success",
    "on-failure",
    "on-abnormal",
    "on-abort",
    "on-watchdog",
]
MAX_UNIT_NAME_BYTES = 240
_UNIT_NAME = re.compile(r"(?:[A-Za-z0-9:_.-]|\\x[0-9a-f]{2})+\Z")
_PORT_MAPPING = re.compile(r"([0-9]+):([0-9]+)/(udp|tcp)\Z")
_ID_MAP_RANGES = r"[0-9]+-[0-9]+-[1-9][0-9]*(?:#[0-9]+-[0-9]+-[1-9][0-9]*)*"
type IDMap = Annotated[
    str,
    Field(
        pattern=rf"\A(?:uids={_ID_MAP_RANGES}(?:;gids={_ID_MAP_RANGES})?"
        rf"|gids={_ID_MAP_RANGES}(?:;uids={_ID_MAP_RANGES})?)\z",
    ),
]


def _validate_unit_name(value: str) -> str:
    if (
        _UNIT_NAME.fullmatch(value) is None
        or value.startswith(".")
        or len(value.encode()) > MAX_UNIT_NAME_BYTES
        or "\\x00" in value.casefold()
    ):
        msg = f"unsafe Quadlet unit name: {value!r}"
        raise ValueError(msg)
    return value


type UnitName = Annotated[str, AfterValidator(_validate_unit_name)]


def _validate_bare_unit_value(value: str) -> str:
    if value != value.strip():
        msg = f"Quadlet value has leading or trailing whitespace: {value!r}"
        raise ValueError(msg)
    return value


type BareUnitValue = Annotated[UnitValue, AfterValidator(_validate_bare_unit_value)]


def _validate_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        msg = f"duplicate Quadlet {label}"
        raise ValueError(msg)


class PortMapping(RevalidatedFrozenModel):
    host: Port
    container: Port
    protocol: Literal["udp", "tcp"] = "udp"

    @classmethod
    def parse(cls, value: str) -> Self:
        match = _PORT_MAPPING.fullmatch(value)
        if match is None:
            msg = f"invalid Quadlet port mapping: {value!r}"
            raise ValueError(msg)
        host, container, protocol = match.groups()
        return cls(
            host=int(host),
            container=int(container),
            protocol=cast(Literal["udp", "tcp"], protocol),
        )

    def render(self) -> str:
        validated = type(self).model_validate(self)
        return f"{validated.host}:{validated.container}/{validated.protocol}"


class VolumeMount(RevalidatedFrozenModel):
    source: Path
    target: PurePosixPath
    read_only: bool = False
    idmap: IDMap | None = None

    @model_validator(mode="after")
    def _validate_paths(self) -> Self:
        for label, value in (("source", self.source), ("target", self.target)):
            text = str(value)
            if (
                not value.is_absolute()
                or ".." in value.parts
                or text != text.strip()
                or any(character in text for character in ":\0\r\n")
            ):
                msg = f"unsafe Quadlet volume {label}: {text!r}"
                raise ValueError(msg)
        return self

    @classmethod
    def parse(cls, value: str) -> Self:
        parts = value.split(":")
        if len(parts) not in {2, 3}:
            msg = f"invalid Quadlet volume mount: {value!r}"
            raise ValueError(msg)
        source, target, *option_parts = parts
        options = option_parts[0].split(",") if option_parts else []
        idmaps = [
            option.removeprefix("idmap=")
            for option in options
            if option.startswith("idmap=")
        ]
        unknown = {
            option
            for option in options
            if option not in {"ro", "rw"} and not option.startswith("idmap=")
        }
        if (
            unknown
            or len(options) != len(set(options))
            or {"ro", "rw"}.issubset(options)
            or len(idmaps) > 1
        ):
            msg = f"invalid Quadlet volume options: {value!r}"
            raise ValueError(msg)
        return cls(
            source=Path(source),
            target=PurePosixPath(target),
            read_only="ro" in options,
            idmap=idmaps[0] if idmaps else None,
        )

    def render(self) -> str:
        validated = type(self).model_validate(self)
        options = ["ro"] if validated.read_only else []
        if validated.idmap is not None:
            options.append(f"idmap={validated.idmap}")
        suffix = f":{','.join(options)}" if options else ""
        return f"{validated.source}:{validated.target}{suffix}"


type FieldKind = Literal[
    "literal",
    "text",
    "token",
    "reference",
    "image",
    "units",
    "networks",
    "ports",
    "volumes",
    "environment",
    "command",
    "integer",
    "boolean",
    "yesno",
]


@dataclass(frozen=True)
class UnitField:
    section: str
    key: str
    kind: FieldKind = "literal"

    @property
    def repeat(self) -> bool:
        return self.kind in {"units", "networks", "ports", "volumes", "environment"}


class QuadletUnit(RevalidatedFrozenModel):
    section: ClassVar[str]
    name: UnitName

    @model_validator(mode="after")
    def _validate_lists(self) -> Self:
        for name, field in type(self).model_fields.items():
            for metadata in field.metadata:
                if isinstance(metadata, UnitField) and metadata.kind in {
                    "units",
                    "networks",
                }:
                    _validate_unique(getattr(self, name), metadata.key)
        return self

    @classmethod
    def load(cls, path: Path) -> Self:
        from .quadlet import load

        return load(path, cls)

    def render(self) -> str:
        from .quadlet import render

        return render(type(self).model_validate(self))

    def save(self, directory: Path) -> tuple[Path, ...]:
        return write_files(
            directory, {Path(f"{self.name}.{self.section.lower()}"): self.render()}
        )


class PodUnit(QuadletUnit):
    section: ClassVar[str] = "Pod"
    description: Annotated[BareUnitValue, UnitField("Unit", "Description", "text")] = ""
    requires: Annotated[
        tuple[UnitToken, ...], UnitField("Unit", "Requires", "units")
    ] = ()
    after: Annotated[tuple[UnitToken, ...], UnitField("Unit", "After", "units")] = ()
    pod_name: Annotated[UnitToken | None, UnitField("Pod", "PodName", "token")] = None
    userns: Annotated[UnitToken | None, UnitField("Pod", "UserNS", "token")] = None
    exit_policy: Annotated[
        Literal["stop", "continue"], UnitField("Pod", "ExitPolicy")
    ] = "stop"
    networks: Annotated[
        tuple[UnitToken, ...], UnitField("Pod", "Network", "networks")
    ] = ()
    publish_ports: Annotated[
        tuple[PortMapping, ...], UnitField("Pod", "PublishPort", "ports")
    ] = ()
    wanted_by: Annotated[
        tuple[UnitToken, ...], UnitField("Install", "WantedBy", "units")
    ] = ()

    @model_validator(mode="after")
    def _validate_ports(self) -> Self:
        hosts: set[tuple[str, int]] = set()
        containers: set[tuple[str, int]] = set()
        for mapping in self.publish_ports:
            host = (mapping.protocol, mapping.host)
            container = (mapping.protocol, mapping.container)
            if host in hosts or container in containers:
                msg = f"conflicting Quadlet port mapping: {mapping.render()}"
                raise ValueError(msg)
            hosts.add(host)
            containers.add(container)
        return self


class ContainerUnit(QuadletUnit):
    section: ClassVar[str] = "Container"
    image: Annotated[UnitToken, UnitField("Container", "Image", "image")]
    pull: Annotated[
        Literal["always", "missing", "never", "newer"] | None,
        UnitField("Container", "Pull"),
    ] = None
    description: Annotated[BareUnitValue, UnitField("Unit", "Description", "text")] = ""
    requires: Annotated[
        tuple[UnitToken, ...], UnitField("Unit", "Requires", "units")
    ] = ()
    wants: Annotated[tuple[UnitToken, ...], UnitField("Unit", "Wants", "units")] = ()
    binds_to: Annotated[
        tuple[UnitToken, ...], UnitField("Unit", "BindsTo", "units")
    ] = ()
    after: Annotated[tuple[UnitToken, ...], UnitField("Unit", "After", "units")] = ()
    pod: Annotated[UnitToken | None, UnitField("Container", "Pod", "reference")] = None
    exec: Annotated[
        tuple[NonEmptyUnitValue, ...], UnitField("Container", "Exec", "command")
    ] = ()
    environment: Annotated[
        FrozenMapping[EnvironmentName, UnitValue],
        UnitField("Container", "Environment", "environment"),
    ] = Field(default_factory=dict)
    volumes: Annotated[
        tuple[VolumeMount, ...], UnitField("Container", "Volume", "volumes")
    ] = ()
    auto_update: Annotated[
        Literal["registry", "local"] | None, UnitField("Container", "AutoUpdate")
    ] = None
    container_name: Annotated[
        UnitToken | None, UnitField("Container", "ContainerName", "token")
    ] = None
    networks: Annotated[
        tuple[UnitToken, ...], UnitField("Container", "Network", "networks")
    ] = ()
    timezone: Annotated[
        UnitToken | None, UnitField("Container", "Timezone", "token")
    ] = None
    stop_timeout: Annotated[
        Seconds | None, UnitField("Container", "StopTimeout", "integer")
    ] = None
    notify: Annotated[bool | None, UnitField("Container", "Notify", "boolean")] = None
    service_type: Annotated[
        Literal["oneshot", "notify"] | None, UnitField("Service", "Type")
    ] = None
    remain_after_exit: Annotated[
        bool | None, UnitField("Service", "RemainAfterExit", "yesno")
    ] = None
    nice: Annotated[
        int | None, Field(ge=-20, le=19), UnitField("Service", "Nice", "integer")
    ] = None
    restart: Annotated[RestartPolicy | None, UnitField("Service", "Restart")] = None
    kill_mode: Annotated[
        Literal["control-group", "mixed", "process", "none"] | None,
        UnitField("Service", "KillMode"),
    ] = None
    watchdog_sec: Annotated[
        Seconds | None, UnitField("Service", "WatchdogSec", "integer")
    ] = None
    watchdog_signal: Annotated[
        Literal["SIGKILL"] | None, UnitField("Service", "WatchdogSignal")
    ] = None
    timeout_start_sec: Annotated[
        Seconds | None, UnitField("Service", "TimeoutStartSec", "integer")
    ] = None
    timeout_stop_sec: Annotated[
        Seconds | None, UnitField("Service", "TimeoutStopSec", "integer")
    ] = None
    wanted_by: Annotated[
        tuple[UnitToken, ...], UnitField("Install", "WantedBy", "units")
    ] = ()

    @model_validator(mode="after")
    def _validate_related_values(self) -> Self:
        targets = tuple(str(volume.target) for volume in self.volumes)
        _validate_unique(targets, "Volume target")
        if self.pod is not None and not self.pod.endswith(".pod"):
            msg = "Container Pod must reference a .pod source unit"
            raise ValueError(msg)
        if self.pod is not None and self.networks:
            msg = "a container in a Pod cannot configure its own Network"
            raise ValueError(msg)
        if self.service_type == "oneshot" and self.restart in {"always", "on-success"}:
            msg = "oneshot services cannot restart always or on success"
            raise ValueError(msg)
        if self.watchdog_sec and self.notify is not True:
            msg = "positive WatchdogSec requires Notify=true"
            raise ValueError(msg)
        return self
