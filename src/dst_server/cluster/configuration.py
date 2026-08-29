import asyncio
import os
import stat
from configparser import ConfigParser
from configparser import Error as ConfigError
from dataclasses import dataclass
from hashlib import file_digest, sha256
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from ulid import ULID

from .config import ClusterConfig, ShardName, ShardSettings
from .layout import Shard, discover

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


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    revision: str
    configuration: ClusterConfig


@dataclass(frozen=True, slots=True)
class InvalidConfiguration:
    revision: str
    paths: tuple[FieldPath, ...]


type ConfigurationRead = ConfigurationSnapshot | InvalidConfiguration


class ConfigurationStoreError(RuntimeError):
    pass


class GamesRunningError(ConfigurationStoreError):
    def __init__(self) -> None:
        super().__init__("all game processes must be stopped")


class RevisionConflictError(ConfigurationStoreError):
    def __init__(self, revision: str) -> None:
        self.revision = revision
        super().__init__("configuration revision conflict")


class InvalidConfigurationError(ConfigurationStoreError):
    def __init__(self, revision: str, paths: tuple[FieldPath, ...]) -> None:
        self.revision = revision
        self.paths = paths
        super().__init__("configuration is invalid")


class TopologyChangeError(ConfigurationStoreError):
    def __init__(self, paths: tuple[FieldPath, ...]) -> None:
        self.paths = paths
        super().__init__("configuration changes deployment topology")


class ConfigurationWriteError(ConfigurationStoreError):
    def __init__(self, revision: str, paths: tuple[FieldPath, ...]) -> None:
        self.revision = revision
        self.paths = paths
        super().__init__("configuration write failed")


class ConfigurationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = asyncio.Lock()
        self._fingerprint: bytes | None = None
        self._revision = str(ULID())
        self.shards, self._topology = _deployment(directory)

    async def read(self) -> ConfigurationRead:
        async with self._lock:
            return self._read()

    async def save(
        self,
        expected_revision: str,
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

            self._revision = str(ULID())
            self._fingerprint = _fingerprint(self.directory)
            saved = self._read()
            if isinstance(saved, InvalidConfiguration):
                raise ConfigurationWriteError(saved.revision, saved.paths)
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
            self._revision = str(ULID())


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
    shards = tuple(sorted(discover(root), key=lambda shard: not shard.master))
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
        settings = _deployment_settings(root / shard.name / "server.ini")
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


def _deployment_settings(path: Path) -> ShardSettings:
    parser = ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (ConfigError, OSError) as error:
        msg = f"invalid DST deployment configuration: {path}: {error}"
        raise ValueError(msg) from error

    fields = {
        "shard": ("is_master",),
        "steam": ("master_server_port",),
        "network": ("server_port",),
    }
    sections: dict[str, str] = {}
    for section in parser.sections():
        folded = section.casefold()
        if folded in fields and folded in sections:
            msg = f"duplicate DST INI section: {section}"
            raise ValueError(msg)
        sections[folded] = section
    relevant = {field for names in fields.values() for field in names}
    if relevant.intersection(parser.defaults()):
        msg = f"deployment fields cannot be INI defaults: {path}"
        raise ValueError(msg)
    values = {
        field: parser.get(section, field, raw=True)
        for folded, names in fields.items()
        if (section := sections.get(folded)) is not None
        for field in names
        if parser.has_option(section, field)
    }
    try:
        return ShardSettings.model_validate_strings(values)
    except ValidationError as error:
        msg = f"invalid DST deployment configuration: {path}: {error}"
        raise ValueError(msg) from error


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
