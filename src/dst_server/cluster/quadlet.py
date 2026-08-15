from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self, cast

from pydantic import AfterValidator, Field, model_validator

from dst_server.models.base import RevalidatedFrozenModel

from .config import ClusterConfig, ShardConfig, _write_files
from .overrides import FrozenMapping

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

DEFAULT_IMAGE = "quay.io/wh2099/dst-server"
DEFAULT_TARGET = "default.target"
MAX_ROOM_SLOT = 299
MAX_ROOM_SHARDS = 4
MAX_UNIT_NAME_BYTES = 240
PREPARE_COMMAND = ("/app/.venv/bin/dst-server", "prepare")
RUN_COMMAND = ("/app/.venv/bin/dst-server", "run")
CLUSTER_ENVIRONMENT = "DST_SERVER_CLUSTER_NAME"
_UNIT_NAME = re.compile(r"(?:[A-Za-z0-9:_.-]|\\x[0-9a-f]{2})+\Z")
_PORT_MAPPING = re.compile(r"([0-9]+):([0-9]+)/(udp|tcp)\Z")


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


def _validate_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        msg = f"duplicate Quadlet {label}"
        raise ValueError(msg)


def _escape_expansions(value: str) -> str:
    return value.replace("\\", "\\\\").replace("$", "$$").replace("%", "%%")


def _collapse_expansions(value: str) -> str:
    return value.replace("$$", "$").replace("%%", "%")


def _collapse_literal(value: str) -> str:
    return _collapse_expansions(value.replace("\\\\", "\\"))


def _literal_expansions(value: str, label: str) -> str:
    index = 0
    while index < len(value):
        character = value[index]
        if character in "\\$%":
            if index + 1 >= len(value) or value[index + 1] != character:
                msg = (
                    f"dynamic systemd expansion or escape is unsupported in "
                    f"{label}: {value!r}"
                )
                raise ValueError(msg)
            index += 1
        index += 1
    return _collapse_literal(value)


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
    relabel: Literal["z", "Z"] | None = None

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
        unknown = set(options).difference({"ro", "rw", "z", "Z"})
        relabels = set(options).intersection({"z", "Z"})
        if (
            unknown
            or len(options) != len(set(options))
            or len(relabels) > 1
            or {"ro", "rw"}.issubset(options)
        ):
            msg = f"invalid Quadlet volume options: {value!r}"
            raise ValueError(msg)
        return cls(
            source=Path(source),
            target=PurePosixPath(target),
            read_only="ro" in options,
            relabel=cast(Literal["z", "Z"] | None, next(iter(relabels), None)),
        )

    def render(self) -> str:
        validated = type(self).model_validate(self)
        options = ["ro"] if validated.read_only else []
        if validated.relabel is not None:
            options.append(validated.relabel)
        suffix = f":{','.join(options)}" if options else ""
        return f"{validated.source}:{validated.target}{suffix}"


type _ParsedSections = dict[str, dict[str, list[str]]]
type _Schema = Mapping[str, Mapping[str, bool]]

_UNIT_KEYS = {"Description": False, "Requires": True, "After": True}
_INSTALL_KEYS = {"WantedBy": True}
_POD_SCHEMA: _Schema = {
    "Unit": _UNIT_KEYS,
    "Pod": {
        "PodName": False,
        "ExitPolicy": False,
        "Network": True,
        "PublishPort": True,
    },
    "Install": _INSTALL_KEYS,
}
_CONTAINER_SCHEMA: _Schema = {
    "Unit": _UNIT_KEYS,
    "Container": {
        "Image": False,
        "Pod": False,
        "Exec": False,
        "Environment": True,
        "Volume": True,
        "AutoUpdate": False,
        "ContainerName": False,
        "Network": True,
        "StopTimeout": False,
    },
    "Service": {
        "Type": False,
        "RemainAfterExit": False,
        "Nice": False,
        "Restart": False,
        "TimeoutStopSec": False,
    },
    "Install": _INSTALL_KEYS,
}


def _load_sections(  # ruff: ignore[complex-structure]
    path: Path,
    schema: _Schema,
    required: str,
) -> _ParsedSections:
    if path.is_symlink():
        msg = f"Quadlet unit cannot be a symlink: {path}"
        raise ValueError(msg)
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeError) as error:
        msg = f"invalid Quadlet unit: {path}: {error}"
        raise ValueError(msg) from error

    parsed: _ParsedSections = {}
    section = ""
    for number, source in enumerate(lines, 1):
        line = source.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            if section not in schema:
                msg = f"unknown Quadlet section at {path}:{number}: {section}"
                raise ValueError(msg)
            parsed.setdefault(section, {})
            continue
        if not section or "=" not in line:
            msg = f"invalid Quadlet line at {path}:{number}: {source!r}"
            raise ValueError(msg)
        key, value = (item.strip() for item in line.split("=", 1))
        repeat = schema[section].get(key)
        if repeat is None:
            msg = f"unknown Quadlet key at {path}:{number}: {section}.{key}"
            raise ValueError(msg)
        section_values = parsed[section]
        if not repeat and key in section_values:
            msg = f"duplicate Quadlet singleton at {path}:{number}: {section}.{key}"
            raise ValueError(msg)
        section_values.setdefault(key, []).append(value)
    if required not in parsed:
        msg = f"missing Quadlet section: {required}"
        raise ValueError(msg)
    return parsed


def _one(parsed: _ParsedSections, section: str, key: str) -> str | None:
    values = parsed.get(section, {}).get(key, ())
    return values[0] if values else None


def _many(parsed: _ParsedSections, section: str, key: str) -> tuple[str, ...]:
    return tuple(parsed.get(section, {}).get(key, ()))


def _unit_list(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    result = tuple(item for value in values for item in value.split())
    if not result:
        msg = f"invalid Quadlet {label}"
        raise ValueError(msg)
    _validate_unique(result, label)
    return result


def _shell_split(value: str, label: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError as error:
        msg = f"invalid Quadlet {label}: {value!r}"
        raise ValueError(msg) from error


def _command(value: str) -> tuple[str, ...]:
    _literal_expansions(value, "Quadlet Exec")
    command = tuple(_collapse_literal(item) for item in _shell_split(value, "Exec"))
    if not command:
        msg = "Quadlet Exec cannot be empty"
        raise ValueError(msg)
    return command


def _boolean(value: str) -> bool:
    match value.casefold():
        case "yes" | "true" | "on" | "1":
            return True
        case "no" | "false" | "off" | "0":
            return False
    msg = f"invalid systemd boolean: {value!r}"
    raise ValueError(msg)


def _integer(value: str, label: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        msg = f"invalid Quadlet {label}: {value!r}"
        raise ValueError(msg) from error


def _environment(values: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        _literal_expansions(value, "Quadlet Environment")
        assignments = _shell_split(value, "Environment")
        if not assignments:
            msg = "Quadlet Environment cannot be empty"
            raise ValueError(msg)
        for source in assignments:
            assignment = _collapse_literal(source)
            name, separator, item = assignment.partition("=")
            if not separator or name in result:
                msg = f"invalid or duplicate Quadlet environment: {name!r}"
                raise ValueError(msg)
            result[name] = item
    return result


def _single_token(value: str, label: str) -> str:
    tokens = _shell_split(value, label)
    if len(tokens) != 1:
        msg = f"invalid Quadlet {label}: {value!r}"
        raise ValueError(msg)
    return tokens[0]


def _literal_token(value: str, label: str) -> str:
    _literal_expansions(value, label)
    return _collapse_expansions(_single_token(value, label))


def _unit_reference(value: str, label: str) -> str:
    if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
        value = value[1:-1]
    if not value or any(character.isspace() for character in value):
        msg = f"invalid Quadlet {label}: {value!r}"
        raise ValueError(msg)
    return value


def _network_references(values: tuple[str, ...]) -> tuple[str, ...]:
    result = []
    for value in values:
        reference = _unit_reference(value, "Network")
        result.append(
            reference
            if reference.endswith(".network")
            else _literal_token(value, "Network")
        )
    return tuple(result)


def _render_sections(
    sections: tuple[tuple[str, list[str]], ...],
    *,
    required: frozenset[str] = frozenset(),
) -> str:
    populated = [
        f"[{name}]" + ("\n" + "\n".join(lines) if lines else "")
        for name, lines in sections
        if lines or name in required
    ]
    return "\n\n".join(populated) + ("\n" if populated else "")


def _unit_name(path: Path, suffix: str) -> str:
    if not path.name.endswith(suffix):
        msg = f"expected a {suffix} Quadlet unit: {path}"
        raise ValueError(msg)
    return _validate_unit_name(path.name[: -len(suffix)])


def _references_pod(path: Path, pod_source: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except OSError, UnicodeError:
        return False
    section = ""
    for source in lines:
        line = source.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "Container" and "=" in line:
            key, value = (item.strip() for item in line.split("=", 1))
            if key == "Pod":
                try:
                    return _unit_reference(value, "Pod") == pod_source
                except ValueError:
                    return False
    return False


class PodUnit(RevalidatedFrozenModel):
    name: UnitName
    description: BareUnitValue = ""
    requires: tuple[UnitToken, ...] = ()
    after: tuple[UnitToken, ...] = ()
    pod_name: UnitToken | None = None
    exit_policy: Literal["stop", "continue"] = "stop"
    networks: tuple[UnitToken, ...] = ()
    publish_ports: tuple[PortMapping, ...] = ()
    wanted_by: tuple[UnitToken, ...] = ()

    @model_validator(mode="after")
    def _validate_lists(self) -> Self:
        _validate_unique(self.requires, "Requires")
        _validate_unique(self.after, "After")
        _validate_unique(self.wanted_by, "WantedBy")
        _validate_unique(self.networks, "Network")
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

    @classmethod
    def load(cls, path: Path) -> Self:
        parsed = _load_sections(path, _POD_SCHEMA, "Pod")
        values: dict[str, object] = {"name": _unit_name(path, ".pod")}
        if (value := _one(parsed, "Unit", "Description")) is not None:
            values["description"] = _literal_expansions(value, "Unit.Description")
        if (value := _one(parsed, "Pod", "PodName")) is not None:
            values["pod_name"] = _literal_token(value, "Pod.PodName")
        if (value := _one(parsed, "Pod", "ExitPolicy")) is not None:
            values["exit_policy"] = value
        for field, section, key in (
            ("requires", "Unit", "Requires"),
            ("after", "Unit", "After"),
            ("wanted_by", "Install", "WantedBy"),
        ):
            if repeated := _many(parsed, section, key):
                values[field] = _unit_list(repeated, key)
        if ports := _many(parsed, "Pod", "PublishPort"):
            values["publish_ports"] = tuple(
                PortMapping.parse(_single_token(value, "PublishPort"))
                for value in ports
            )
        if repeated := _many(parsed, "Pod", "Network"):
            values["networks"] = _network_references(repeated)
        return cls.model_validate(values)

    def render(self) -> str:
        validated = type(self).model_validate(self)
        unit = []
        if validated.description:
            unit.append(f"Description={_escape_expansions(validated.description)}")
        unit.extend(f"Requires={value}" for value in validated.requires)
        unit.extend(f"After={value}" for value in validated.after)
        pod = []
        if validated.pod_name is not None:
            pod.append(f"PodName={_escape_expansions(validated.pod_name)}")
        if "exit_policy" in validated.model_fields_set:
            pod.append(f"ExitPolicy={validated.exit_policy}")
        pod.extend(
            f"Network={value}"
            if value.endswith(".network")
            else f"Network={_escape_expansions(value)}"
            for value in validated.networks
        )
        pod.extend(
            f"PublishPort={mapping.render()}" for mapping in validated.publish_ports
        )
        install = [f"WantedBy={value}" for value in validated.wanted_by]
        return _render_sections(
            (("Unit", unit), ("Pod", pod), ("Install", install)),
            required=frozenset({"Pod"}),
        )

    def save(self, directory: Path) -> tuple[Path, ...]:
        return _write_files(
            directory,
            {Path(f"{self.name}.pod"): self.render()},
        )


class ContainerUnit(RevalidatedFrozenModel):
    name: UnitName
    image: UnitToken
    description: BareUnitValue = ""
    requires: tuple[UnitToken, ...] = ()
    after: tuple[UnitToken, ...] = ()
    pod: UnitToken | None = None
    exec: tuple[NonEmptyUnitValue, ...] = ()
    environment: FrozenMapping[EnvironmentName, UnitValue] = Field(
        default_factory=dict,
    )
    volumes: tuple[VolumeMount, ...] = ()
    auto_update: Literal["registry", "local"] | None = None
    container_name: UnitToken | None = None
    networks: tuple[UnitToken, ...] = ()
    stop_timeout: Seconds | None = None
    service_type: Literal["oneshot", "notify"] | None = None
    remain_after_exit: bool | None = None
    nice: Annotated[int, Field(ge=-20, le=19)] | None = None
    restart: RestartPolicy | None = None
    timeout_stop_sec: Seconds | None = None
    wanted_by: tuple[UnitToken, ...] = ()

    @model_validator(mode="after")
    def _validate_related_values(self) -> Self:
        _validate_unique(self.requires, "Requires")
        _validate_unique(self.after, "After")
        _validate_unique(self.wanted_by, "WantedBy")
        _validate_unique(self.networks, "Network")
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
        return self

    @classmethod
    def load(  # ruff: ignore[complex-structure, too-many-branches]
        cls,
        path: Path,
    ) -> Self:
        parsed = _load_sections(path, _CONTAINER_SCHEMA, "Container")
        image = _one(parsed, "Container", "Image")
        if image is None:
            msg = "missing Quadlet key: Container.Image"
            raise ValueError(msg)
        image_value = _unit_reference(image, "Image")
        values: dict[str, object] = {
            "name": _unit_name(path, ".container"),
            "image": (
                image_value
                if image_value.endswith((".build", ".image"))
                else _literal_token(image, "Container.Image")
            ),
        }
        if (value := _one(parsed, "Unit", "Description")) is not None:
            values["description"] = _literal_expansions(value, "Unit.Description")
        if (value := _one(parsed, "Container", "Pod")) is not None:
            values["pod"] = _unit_reference(value, "Pod")
        if (value := _one(parsed, "Container", "AutoUpdate")) is not None:
            values["auto_update"] = value
        if (value := _one(parsed, "Container", "ContainerName")) is not None:
            values["container_name"] = _literal_token(
                value,
                "Container.ContainerName",
            )
        if (value := _one(parsed, "Service", "Type")) is not None:
            values["service_type"] = value
        if (value := _one(parsed, "Service", "Restart")) is not None:
            values["restart"] = value
        for field, section, key in (
            ("requires", "Unit", "Requires"),
            ("after", "Unit", "After"),
            ("wanted_by", "Install", "WantedBy"),
        ):
            if repeated := _many(parsed, section, key):
                values[field] = _unit_list(repeated, key)
        if (value := _one(parsed, "Container", "Exec")) is not None:
            values["exec"] = _command(value)
        if repeated := _many(parsed, "Container", "Environment"):
            values["environment"] = _environment(repeated)
        if repeated := _many(parsed, "Container", "Volume"):
            values["volumes"] = tuple(
                VolumeMount.parse(_literal_expansions(value, "Container.Volume"))
                for value in repeated
            )
        if repeated := _many(parsed, "Container", "Network"):
            values["networks"] = _network_references(repeated)
        for field, section, key in (
            ("stop_timeout", "Container", "StopTimeout"),
            ("nice", "Service", "Nice"),
            ("timeout_stop_sec", "Service", "TimeoutStopSec"),
        ):
            if (value := _one(parsed, section, key)) is not None:
                values[field] = _integer(value, key)
        if (value := _one(parsed, "Service", "RemainAfterExit")) is not None:
            values["remain_after_exit"] = _boolean(value)
        return cls.model_validate(values)

    def render(self) -> str:  # ruff: ignore[complex-structure]
        validated = type(self).model_validate(self)
        unit = []
        if validated.description:
            unit.append(f"Description={_escape_expansions(validated.description)}")
        unit.extend(f"Requires={value}" for value in validated.requires)
        unit.extend(f"After={value}" for value in validated.after)
        container = [
            f"Image={validated.image}"
            if validated.image.endswith((".build", ".image"))
            else f"Image={_escape_expansions(validated.image)}"
        ]
        if validated.pod is not None:
            container.append(f"Pod={validated.pod}")
        if validated.exec:
            container.append(
                f"Exec={shlex.join(tuple(map(_escape_expansions, validated.exec)))}"
            )
        container.extend(
            f"Environment={shlex.quote(_escape_expansions(f'{name}={value}'))}"
            for name, value in sorted(validated.environment.items())
        )
        container.extend(
            f"Volume={_escape_expansions(volume.render())}"
            for volume in validated.volumes
        )
        container.extend(
            f"Network={value}"
            if value.endswith(".network")
            else f"Network={_escape_expansions(value)}"
            for value in validated.networks
        )
        if validated.auto_update is not None:
            container.append(f"AutoUpdate={validated.auto_update}")
        if validated.container_name is not None:
            container.append(
                f"ContainerName={_escape_expansions(validated.container_name)}"
            )
        if validated.stop_timeout is not None:
            container.append(f"StopTimeout={validated.stop_timeout}")
        service = []
        if validated.service_type is not None:
            service.append(f"Type={validated.service_type}")
        if validated.remain_after_exit is not None:
            service.append(
                f"RemainAfterExit={'yes' if validated.remain_after_exit else 'no'}"
            )
        if validated.nice is not None:
            service.append(f"Nice={validated.nice}")
        if validated.restart is not None:
            service.append(f"Restart={validated.restart}")
        if validated.timeout_stop_sec is not None:
            service.append(f"TimeoutStopSec={validated.timeout_stop_sec}")
        install = [f"WantedBy={value}" for value in validated.wanted_by]
        return _render_sections((
            ("Unit", unit),
            ("Container", container),
            ("Service", service),
            ("Install", install),
        ))

    def save(self, directory: Path) -> tuple[Path, ...]:
        return _write_files(
            directory,
            {Path(f"{self.name}.container"): self.render()},
        )


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
        mappings = []
        for ordinal, (_, shard) in enumerate(shards):
            settings = shard.settings
            for base, container in (
                (30000, settings.server_port),
                (30300, settings.master_server_port),
            ):
                mappings.append(
                    PortMapping(
                        host=base + 600 * ordinal + slot,
                        container=container,
                    )
                )
        ordered = tuple(sorted(mappings, key=lambda item: (item.host, item.container)))
        PodUnit(name="validation", publish_ports=ordered)
        return ordered


class QuadletApplication(RevalidatedFrozenModel):
    pod: PodUnit
    prepare: ContainerUnit
    workers: tuple[ContainerUnit, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_topology(self) -> Self:
        pod_source = f"{self.pod.name}.pod"
        prepare_source = f"{self.prepare.name}.container"
        if self.prepare.name != f"{self.pod.name}-prepare":
            msg = "Quadlet prepare unit must use the application pod name"
            raise ValueError(msg)
        if (
            self.prepare.pod is not None
            or self.prepare.service_type != "oneshot"
            or self.prepare.remain_after_exit is not None
        ):
            msg = "Quadlet prepare unit must be a transient standalone oneshot"
            raise ValueError(msg)
        if (
            prepare_source not in self.pod.requires
            or prepare_source not in self.pod.after
        ):
            msg = f"Quadlet Pod does not depend on {prepare_source}"
            raise ValueError(msg)
        worker_names = tuple(worker.name for worker in self.workers)
        if worker_names != tuple(sorted(worker_names)):
            msg = "Quadlet workers must use canonical unit-name order"
            raise ValueError(msg)
        names = {self.prepare.name}
        for worker in self.workers:
            if worker.name in names:
                msg = f"duplicate Quadlet container unit: {worker.name}"
                raise ValueError(msg)
            names.add(worker.name)
            if worker.pod != pod_source:
                msg = f"Quadlet worker is not a member of {pod_source}: {worker.name}"
                raise ValueError(msg)
        return self

    def replace(self, **changes: object) -> Self:
        if "pod" in changes and "workers" not in changes:
            pod = PodUnit.model_validate(changes["pod"])
            new_hosts = {
                mapping.container: mapping.host
                for mapping in pod.publish_ports
                if mapping.protocol == "udp"
            }
            replacements = {
                mapping.host: new_hosts.get(mapping.container)
                for mapping in self.pod.publish_ports
                if mapping.protocol == "udp"
                and mapping.host != new_hosts.get(mapping.container)
            }
            if replacements:
                workers = []
                for worker in self.workers:
                    command = list(worker.exec)
                    try:
                        index = command.index("--external-port") + 1
                        replacement = replacements[int(command[index])]
                    except ValueError, IndexError, KeyError:
                        pass
                    else:
                        if replacement is None:
                            del command[index - 1 : index + 1]
                        else:
                            command[index] = str(replacement)
                    workers.append(worker.replace(exec=tuple(command)))
                changes["workers"] = tuple(workers)
        return super().replace(**changes)

    @property
    def containers(self) -> tuple[ContainerUnit, ...]:
        return (self.prepare, *self.workers)

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
    ) -> Self:
        validated = ClusterConfig.model_validate(cluster)
        logical_name = name or f"dst-{cluster_path.name}"
        base = _escape_unit_name(logical_name)
        pod_source = f"{base}.pod"
        prepare_name = f"{base}-prepare"
        dependency = f"{prepare_name}.container"
        volume = VolumeMount(
            source=cluster_path.absolute(),
            target=PurePosixPath("/cluster"),
            relabel="z",
        )
        publish_ports = allocation.mappings(validated) if allocation else ()
        published_hosts = {mapping.container: mapping.host for mapping in publish_ports}
        pod = PodUnit(
            name=base,
            description=f"Don't Starve Together cluster {logical_name}",
            requires=(dependency,),
            after=(dependency,),
            exit_policy="continue",
            publish_ports=publish_ports,
            wanted_by=(DEFAULT_TARGET,),
        )
        prepare = ContainerUnit(
            name=prepare_name,
            description=f"Prepare Don't Starve Together cluster {logical_name}",
            exec=PREPARE_COMMAND,
            service_type="oneshot",
            image=image,
            volumes=(volume,),
            stop_timeout=40,
            restart="on-failure",
            timeout_stop_sec=50,
        )
        environment = dict(telemetry_environment or {})
        if environment.get(CLUSTER_ENVIRONMENT, logical_name) != logical_name:
            msg = f"{CLUSTER_ENVIRONMENT} is managed by QuadletApplication"
            raise ValueError(msg)
        environment[CLUSTER_ENVIRONMENT] = logical_name
        workers = tuple(
            ContainerUnit(
                name=f"{base}-{_escape_unit_name(shard_name)}",
                description=f"Don't Starve Together shard {shard_name}",
                exec=(
                    *RUN_COMMAND,
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
                environment=environment,
                image=image,
                pod=pod_source,
                volumes=(volume,),
                stop_timeout=40,
                restart="on-failure",
                timeout_stop_sec=50,
            )
            for shard_name in sorted(validated.shards, key=_escape_unit_name)
        )
        return cls(pod=pod, prepare=prepare, workers=workers)

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
        prepare_name = f"{pod.name}-prepare"
        prepare_path = directory / f"{prepare_name}.container"
        prepare = ContainerUnit.load(prepare_path)
        workers = tuple(
            ContainerUnit.load(path)
            for path in sorted(directory.glob("*.container"))
            if path != prepare_path and _references_pod(path, pod_source)
        )
        return cls(pod=pod, prepare=prepare, workers=workers)

    def files(self) -> dict[Path, str]:
        validated = type(self).model_validate(self)
        files = {Path(f"{validated.pod.name}.pod"): validated.pod.render()}
        files.update({
            Path(f"{unit.name}.container"): unit.render()
            for unit in validated.containers
        })
        return files

    def save(self, directory: Path) -> tuple[Path, ...]:
        files = self.files()
        if directory.is_dir() and not directory.is_symlink():
            pod_source = f"{self.pod.name}.pod"
            unexpected = sorted(
                path.name
                for path in directory.glob("*.container")
                if Path(path.name) not in files and _references_pod(path, pod_source)
            )
            if unexpected:
                msg = f"unmanaged Quadlet units would remain active: {unexpected}"
                raise ValueError(msg)
        return _write_files(directory, files)


__all__ = [
    "DEFAULT_IMAGE",
    "ContainerUnit",
    "PodUnit",
    "PortMapping",
    "QuadletApplication",
    "RoomPortAllocation",
    "VolumeMount",
]
