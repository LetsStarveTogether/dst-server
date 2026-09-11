"""Typed views of native room files and their small operational policy."""

import json
import re
import secrets
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, time
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    SerializerFunctionWrapHandler,
    field_serializer,
    model_validator,
)

from dst_server.configuration.files import (
    atomic_write,
    configuration_file_exists,
    read_text,
    validate_directory,
    write_files,
)
from dst_server.configuration.models import (
    PERMISSION_FILES,
    ClusterConfig,
    ClusterSettings,
    ShardSettings,
    _shared_cluster_key,
)
from dst_server.configuration.overrides import FrozenMapping
from dst_server.deployment import DEFAULT_IMAGE, QuadletApplication, RoomPortAllocation
from dst_server.deployment.application import CLUSTER_ENVIRONMENT, MAX_ROOM_SLOT
from dst_server.deployment.models import EnvironmentName, IDMap, UnitToken, UnitValue
from dst_server.models.base import RevalidatedFrozenModel

CONTROL_FILE = ".dst-control.json"
DEFAULT_ROOT = Path("/srv/dst")
DEFAULT_QUADLET_DIR = Path("/etc/containers/systemd")
_PERMISSION_FIELDS = frozenset(Path(name).stem for name in PERMISSION_FILES)
_ROOM_DIRECTORY = re.compile(r"[0-9]{3}\Z")


class DailyWindow(RevalidatedFrozenModel):
    start: time
    end: time

    @model_validator(mode="after")
    def _validate_times(self) -> Self:
        for value in (self.start, self.end):
            if value.tzinfo is not None or value.second or value.microsecond:
                msg = "daily windows require local times with minute precision"
                raise ValueError(msg)
        if self.start == self.end:
            msg = "daily window start and end must differ"
            raise ValueError(msg)
        return self

    def contains(self, value: time) -> bool:
        value = value.replace(tzinfo=None)
        if self.start < self.end:
            return self.start <= value < self.end
        return value >= self.start or value < self.end


class RoomDeployment(RevalidatedFrozenModel):
    image: UnitToken = DEFAULT_IMAGE
    environment: FrozenMapping[EnvironmentName, UnitValue] = Field(default_factory=dict)
    volume_idmap: IDMap | None = None
    userns: UnitToken | None = None
    start_on_boot: bool = True


class Room(RevalidatedFrozenModel):
    number: Annotated[int, Field(ge=0, le=MAX_ROOM_SLOT)]
    template: str | None = None
    cluster: ClusterConfig
    deployment: RoomDeployment = Field(default_factory=RoomDeployment)
    schedule: tuple[DailyWindow, ...] = ()
    recycle: bool = False

    @field_serializer("cluster", mode="wrap")
    def _serialize_cluster(
        self, value: ClusterConfig, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        # Permissions belong to the live files; persisting them here loses new bans.
        return {
            name: item
            for name, item in handler(value).items()
            if name not in _PERMISSION_FIELDS
        }

    def game_files(self) -> dict[Path, str]:
        return {
            path: content
            for path, content in self.cluster.files().items()
            if path.name not in PERMISSION_FILES
        }

    def save_game(
        self, directory: Path, *, previous: Room | None = None
    ) -> tuple[Path, ...]:
        """Write changed native files, preserving permissions and shard saves."""
        cluster = self.cluster
        if (
            _shared_cluster_key(
                cluster.settings, (shard.settings for shard in cluster.shards.values())
            )
            is None
        ):
            path = directory / "cluster.ini"
            key = (
                _shared_cluster_key(
                    previous.cluster.settings,
                    (shard.settings for shard in previous.cluster.shards.values()),
                )
                if previous is not None
                else _shared_cluster_key(
                    ClusterSettings.load(path)
                    if configuration_file_exists(path)
                    else ClusterSettings(),
                    (
                        ShardSettings.load(server)
                        for name in cluster.shards
                        if configuration_file_exists(
                            server := directory / name / "server.ini"
                        )
                    ),
                )
            )
            cluster = cluster.replace(
                settings=cluster.settings.replace(
                    cluster_key=key or SecretStr(secrets.token_urlsafe(32))
                )
            )
        files = self.replace(cluster=cluster).game_files()
        old = previous.game_files() if previous is not None else {}
        removed = {
            path for path in old.keys() - files.keys() if path.name == "server.ini"
        } | {
            Path(shard) / name
            for shard in self.cluster.shards
            for name in ("worldgenoverride.lua", "leveldataoverride.lua")
            if Path(shard) / name not in files
            and configuration_file_exists(directory / shard / name)
        }
        for path in removed:
            validate_directory(directory / path.parent)
            configuration_file_exists(directory / path)
        changed = {
            path: content for path, content in files.items() if old.get(path) != content
        }
        written = write_files(directory, changed, directories=(Path("mods/ugc"),))
        for path in removed:
            (directory / path).unlink(missing_ok=True)
        return written

    def save_policy(self, path: Path) -> None:
        current = read_control(path)
        updates: dict[str, Any] = {
            "template": self.template,
            "schedule": self.schedule,
            "recycle": self.recycle,
        }
        if current.schedule != self.schedule:
            updates.update(override=None, until=None)
        updated = current.model_copy(update=updates)
        if updated != current:
            write_control(
                path, updated.model_copy(update={"revision": current.revision + 1})
            )

    def application(self, directory: Path) -> QuadletApplication:
        deployment = self.deployment
        application = QuadletApplication.for_cluster(
            self.cluster,
            directory,
            allocation=RoomPortAllocation(number=self.number),
            image=deployment.image,
            telemetry_environment=deployment.environment,
            volume_idmap=deployment.volume_idmap,
            userns=deployment.userns,
        )
        if not deployment.start_on_boot:
            application = application.replace(pod=application.pod.replace(wanted_by=()))
        return application

    def get(self, pointer: str) -> Any:
        value: Any = self.model_dump(mode="json")
        for part in _pointer_parts(pointer):
            value = _pointer_item(value, part)
        return value

    def edit(self, pointer: str, value: Any = None, *, unset: bool = False) -> Self:
        return (
            self.edit_many((), unset=(pointer,))
            if unset
            else self.edit_many(((pointer, value),))
        )

    def edit_many(
        self,
        changes: Sequence[tuple[str, Any]],
        *,
        unset: Sequence[str] = (),
    ) -> Self:
        data = self.model_dump(
            mode="json", exclude_unset=True, context={"secrets": True}
        )
        defaults = self.model_dump(mode="json", context={"secrets": True})
        for pointer, value in changes:
            _edit_pointer(data, defaults, pointer, value, unset=False)
        for pointer in unset:
            _edit_pointer(data, defaults, pointer, None, unset=True)
        return type(self).model_validate_json(json.dumps(data))


def _edit_pointer(  # ruff: ignore[complex-structure, too-many-branches]
    data: dict[str, Any], defaults: Any, pointer: str, value: Any, *, unset: bool
) -> None:
    parts = _pointer_parts(pointer)
    if not parts or parts[0] == "number":
        msg = "room identity cannot be changed through a configuration field"
        raise ValueError(msg)
    permission = (
        parts[1] in _PERMISSION_FIELDS
        if len(parts) > 1
        else isinstance(value, dict) and bool(_PERMISSION_FIELDS.intersection(value))
    )
    if parts[0] == "cluster" and permission:
        msg = "permission lists are managed through player commands"
        raise ValueError(msg)
    target: Any = data
    for part in parts[:-1]:
        try:
            defaults = _pointer_item(defaults, part)
        except KeyError, IndexError, ValueError:
            defaults = _pointer_item(target, part)
        if isinstance(target, dict) and part not in target:
            if isinstance(defaults, dict):
                target[part] = {}
            elif isinstance(defaults, list):
                target[part] = deepcopy(defaults)
        target = _pointer_item(target, part)
    key = parts[-1]
    if isinstance(target, dict):
        if unset:
            if key not in target:
                raise KeyError(pointer)
            del target[key]
        else:
            target[key] = deepcopy(value)
    elif isinstance(target, list):
        if key == "-" and not unset:
            target.append(deepcopy(value))
        else:
            index = _pointer_index(key, len(target))
            if unset:
                del target[index]
            else:
                target[index] = deepcopy(value)
    else:
        msg = f"JSON Pointer parent is not an object or array: {pointer}"
        raise TypeError(msg)


def _pointer_parts(pointer: str) -> tuple[str, ...]:
    if not pointer:
        return ()
    if not pointer.startswith("/") or re.search(r"~(?:[^01]|$)", pointer):
        msg = f"invalid JSON Pointer: {pointer!r}"
        raise ValueError(msg)
    return tuple(
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    )


def _pointer_index(part: str, length: int) -> int:
    if re.fullmatch(r"0|[1-9][0-9]*", part) is None or int(part) >= length:
        raise IndexError(part)
    return int(part)


def _pointer_item(value: Any, part: str) -> Any:
    if isinstance(value, Mapping):
        return value[part]
    if isinstance(value, list):
        return value[_pointer_index(part, len(value))]
    msg = f"JSON Pointer cannot traverse a scalar at {part!r}"
    raise ValueError(msg)


class Control(BaseModel):
    """Only operational policy/state is stored outside the native configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    template: str | None = None
    schedule: tuple[DailyWindow, ...] = ()
    recycle: bool = False
    paused: bool = False
    override: bool | None = None
    until: datetime | None = None
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_until(self) -> Self:
        if self.until is not None and self.until.tzinfo is None:
            msg = "control expiry must include a timezone"
            raise ValueError(msg)
        return self


def read_control(path: Path) -> Control:
    target = path / CONTROL_FILE
    return (
        Control.model_validate_json(read_text(target))
        if configuration_file_exists(target)
        else Control()
    )


def write_control(path: Path, control: Control) -> None:
    target = path / CONTROL_FILE
    validate_directory(path)
    configuration_file_exists(target)
    atomic_write(target, control.model_dump_json(indent=2) + "\n", 0o600)


def control_revision(path: Path) -> int:
    return read_control(path).revision


class RoomStore:
    def __init__(
        self, root: Path = DEFAULT_ROOT, quadlet_dir: Path | None = None
    ) -> None:
        self.root = root
        self.quadlet_dir = quadlet_dir

    def path(self, number: int) -> Path:
        RoomPortAllocation(number=number)
        return self.root / f"{number:03d}"

    def policy(self, number: int) -> Control:
        directory = self.path(number)
        validate_directory(directory)
        if not configuration_file_exists(directory / "cluster.ini"):
            raise FileNotFoundError(directory / "cluster.ini")
        return read_control(directory)

    def load(self, number: int) -> Room:
        directory = self.path(number)
        policy = self.policy(number)
        cluster = ClusterConfig.load(directory)
        deployment = RoomDeployment()
        if self.quadlet_dir is not None:
            application = QuadletApplication.load(
                self.quadlet_dir, name=f"dst-{number:03d}", legacy=True
            )
            master = application.master
            environment = dict(master.environment)
            environment.pop(CLUSTER_ENVIRONMENT, None)
            volume = next(
                (
                    volume
                    for volume in master.volumes
                    if str(volume.target) == "/cluster"
                ),
                None,
            )
            if volume is None or volume.source != directory:
                msg = f"Quadlet must mount {directory} at /cluster"
                raise ValueError(msg)
            deployment = RoomDeployment(
                image=master.image,
                environment=environment,
                volume_idmap=volume.idmap,
                userns=application.pod.userns,
                start_on_boot=bool(application.pod.wanted_by),
            )
        return Room(
            number=number,
            cluster=cluster,
            deployment=deployment,
            template=policy.template,
            schedule=policy.schedule,
            recycle=policy.recycle,
        )

    def numbers(self) -> tuple[int, ...]:
        if not self.root.exists():
            return ()
        validate_directory(self.root)
        return tuple(
            int(path.name)
            for path in sorted(self.root.iterdir())
            if _ROOM_DIRECTORY.fullmatch(path.name)
            and int(path.name) <= MAX_ROOM_SLOT
            and ((path / "cluster.ini").exists() or (path / "cluster.ini").is_symlink())
        )

    def list(self) -> tuple[Room, ...]:
        return tuple(self.load(number) for number in self.numbers())

    def save_policy(self, room: Room) -> None:
        room.save_policy(self.path(room.number))

    def save(self, room: Room) -> tuple[Path, ...]:
        """Generate native room files and deployment from an explicit template."""
        room = Room.model_validate(room)
        directory = self.path(room.number)
        if self.root.exists() or self.root.is_symlink():
            validate_directory(self.root)
        read_control(directory)
        application = (
            room.application(directory) if self.quadlet_dir is not None else None
        )
        if application is not None and self.quadlet_dir is not None:
            application.validate_save(self.quadlet_dir)
        previous = (
            self.load(room.number)
            if configuration_file_exists(directory / "cluster.ini")
            else None
        )
        written = room.save_game(directory, previous=previous)
        self.save_policy(room)
        if application is not None and self.quadlet_dir is not None:
            written += application.save(self.quadlet_dir)
        return written
