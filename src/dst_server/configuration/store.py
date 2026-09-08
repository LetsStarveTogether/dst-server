import asyncio
import os
import stat
from hashlib import file_digest, sha256
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from ulid import ULID

from dst_server.errors import (
    ConfigurationWriteError,
    GamesRunningError,
    InvalidConfigurationError,
    RevisionConflictError,
    TopologyChangeError,
)
from dst_server.models.cluster import (
    ConfigurationRead,
    ConfigurationSnapshot,
    InvalidConfiguration,
)

from .files import Shard, load_ini, shard_directories
from .models import ClusterConfig, ShardName, ShardSettings

type FieldPath = tuple[str, ...]
type _Topology = dict[FieldPath, bool | int]

_SHARD_NAME = TypeAdapter(ShardName)

_ROOT_FILES = (
    Path("cluster.ini"),
    Path("cluster_token.txt"),
    Path("adminlist.txt"),
    Path("whitelist.txt"),
    Path("blocklist.txt"),
    Path("mods/modsettings.lua"),
    Path("mods/dedicated_server_mods_setup.lua"),
)
_SHARD_FILES = tuple(
    map(
        Path,
        (
            "server.ini",
            "modoverrides.lua",
            "worldgenoverride.lua",
            "leveldataoverride.lua",
        ),
    )
)


class ConfigurationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = asyncio.Lock()
        self._fingerprint: bytes | None = None
        self._revision = ULID()
        self.shards, self._topology = _deployment(directory)

    async def read(self) -> ConfigurationRead:
        async with self._lock:
            return self._read()

    async def save(
        self,
        expected_revision: ULID,
        desired: ClusterConfig,
        *,
        all_stopped: bool,
    ) -> ConfigurationSnapshot:
        async with self._lock:
            if not all_stopped:
                raise GamesRunningError

            current = self._read()
            if expected_revision != current.revision:
                raise RevisionConflictError(current.revision)

            try:
                desired = ClusterConfig.model_validate(desired)
            except (TypeError, ValueError) as error:
                raise InvalidConfigurationError(
                    current.revision,
                    _error_paths(error),
                ) from None

            self.validate_deployment(desired)

            fingerprint = _fingerprint(self.directory)
            if fingerprint != self._fingerprint:
                self._accept_fingerprint(fingerprint)
                raise RevisionConflictError(self._revision)

            try:
                desired.save(self.directory)
            except (OSError, ValueError) as error:
                self._accept_fingerprint(_fingerprint(self.directory))
                raise ConfigurationWriteError(
                    self._revision,
                    _error_paths(error),
                ) from None

            self._revision = ULID()
            self._fingerprint = _fingerprint(self.directory)
            saved = self._read()
            if isinstance(saved, InvalidConfiguration):
                raise ConfigurationWriteError(saved.revision, saved.fields)
            return saved

    def validate_deployment(self, configuration: ClusterConfig) -> None:
        if paths := _topology_changes(self._topology, configuration):
            raise TopologyChangeError(paths)

    def _read(self) -> ConfigurationRead:
        for _ in range(3):
            before = _fingerprint(self.directory)
            self._accept_fingerprint(before)
            try:
                result: ClusterConfig | OSError | ValueError = ClusterConfig.load(
                    self.directory
                )
            except (OSError, ValueError) as error:
                result = error
            after = _fingerprint(self.directory)
            self._accept_fingerprint(after)
            if before != after:
                continue
            if isinstance(result, Exception):
                return InvalidConfiguration(
                    self._revision,
                    _error_paths(result),
                )
            return ConfigurationSnapshot(self._revision, result)
        return InvalidConfiguration(self._revision, (("configuration",),))

    def _accept_fingerprint(self, fingerprint: bytes) -> None:
        if self._fingerprint is None:
            self._fingerprint = fingerprint
        elif self._fingerprint != fingerprint:
            self._fingerprint = fingerprint
            self._revision = ULID()


def _managed_paths(root: Path) -> tuple[Path, ...]:
    paths = {Path(), *_ROOT_FILES}
    try:
        entries = tuple(root.iterdir())
    except OSError:
        entries = ()
    for entry in entries:
        if entry.name == "mods":
            continue
        try:
            mode = entry.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            directory = Path(entry.name)
            paths.add(directory)
            paths.update(directory / name for name in _SHARD_FILES)
    return tuple(sorted(paths, key=lambda path: os.fsencode(path.as_posix())))


def _fingerprint(root: Path) -> bytes:
    digest = sha256()
    for relative in _managed_paths(root):
        encoded = os.fsencode(relative.as_posix())
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(_path_state(root / relative))
    return digest.digest()


def _path_state(path: Path) -> bytes:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        return b"N" if isinstance(error, FileNotFoundError) else b"E"
    if not stat.S_ISREG(mode):
        return b"O" + stat.S_IFMT(mode).to_bytes(4, "big")

    try:
        content_digest = _file_digest(path)
    except OSError:
        return b"E"
    return b"F" + content_digest


def _file_digest(path: Path) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        return file_digest(stream, "sha256").digest()


def _topology(configuration: ClusterConfig) -> _Topology:
    return {
        ("shards", name, "settings", field): getattr(
            configuration.shards[name].settings, field
        )
        for name in sorted(configuration.shards, key=str.casefold)
        for field in ("is_master", "server_port", "master_server_port")
    }


def _deployment(root: Path) -> tuple[tuple[Shard, ...], _Topology]:
    settings_by_name = {
        path.name: load_ini(
            path / "server.ini",
            ShardSettings,
            include=frozenset({"is_master", "server_port", "master_server_port"}),
        )
        for path in shard_directories(root)
    }
    shards = tuple(
        sorted(
            (
                Shard(
                    name,
                    settings.is_master,
                    root / "console" if settings.is_master else root / name / "console",
                )
                for name, settings in settings_by_name.items()
            ),
            key=lambda shard: not shard.master,
        )
    )
    masters = [shard.name for shard in shards if shard.master]
    if len(masters) != 1:
        msg = f"expected exactly one master shard, found {len(masters)}: {masters}"
        raise ValueError(msg)
    topology: _Topology = {}
    names: dict[str, str] = {}
    ports: dict[int, str] = {}
    for shard in shards:
        _SHARD_NAME.validate_python(shard.name)
        folded = shard.name.casefold()
        if previous := names.get(folded):
            msg = f"duplicate DST shard directory names: {previous!r}, {shard.name!r}"
            raise ValueError(msg)
        names[folded] = shard.name
        settings = settings_by_name[shard.name]
        for field in ("is_master", "server_port", "master_server_port"):
            value = getattr(settings, field)
            topology["shards", shard.name, "settings", field] = value
            if field == "is_master":
                continue
            owner = f"{shard.name}.{field}"
            if previous := ports.get(value):
                msg = f"UDP port {value} is shared by {previous} and {owner}"
                raise ValueError(msg)
            ports[value] = owner
    return shards, topology


def _topology_changes(
    expected: _Topology,
    desired: ClusterConfig,
) -> tuple[FieldPath, ...]:
    actual = _topology(desired)
    if expected.keys() != actual.keys():
        return (("shards",),)
    return tuple(path for path in sorted(expected) if actual[path] != expected[path])


def _error_paths(error: BaseException) -> tuple[FieldPath, ...]:
    if isinstance(error, ValidationError):
        paths = {
            tuple(map(str, item["loc"])) or ("configuration",)
            for item in error.errors(include_input=False, include_url=False)
        }
        return tuple(sorted(paths))
    return (("configuration",),)
