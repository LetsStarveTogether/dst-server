from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from ulid import ULID

from dst_server.errors import TopologyChangeError
from dst_server.models.cluster import (
    ConfigurationRead,
    ConfigurationSnapshot,
    InvalidConfiguration,
)

from .files import Shard, load_ini, shard_directories
from .models import (
    CLUSTER_STRUCTURE_FIELDS,
    ClusterConfig,
    ClusterSettings,
    ShardName,
    ShardSettings,
    cluster_structure,
)

type FieldPath = tuple[str, ...]
type _Topology = dict[FieldPath, object]

_SHARD_NAME = TypeAdapter(ShardName)


class ConfigurationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._configuration: ClusterConfig | None = None
        self._revision = ULID()
        self.shards, self._topology = _deployment(directory)

    async def read(self) -> ConfigurationRead:
        try:
            configuration = ClusterConfig.load(self.directory)
        except (OSError, ValueError) as error:
            return InvalidConfiguration(self._revision, _error_paths(error))
        if configuration != self._configuration:
            self._configuration = configuration
            self._revision = ULID()
        return ConfigurationSnapshot(self._revision, configuration)

    def validate_deployment(self) -> None:
        _, actual = _deployment(self.directory)
        if self._topology.keys() != actual.keys():
            raise TopologyChangeError((("shards",),))
        if paths := tuple(
            path for path in sorted(actual) if self._topology[path] != actual[path]
        ):
            raise TopologyChangeError(paths)


def _deployment(root: Path) -> tuple[tuple[Shard, ...], _Topology]:
    settings_by_name = {
        path.name: load_ini(path / "server.ini", ShardSettings)
        for path in shard_directories(root)
    }
    shards = tuple(
        sorted(
            (
                Shard(name, settings.is_master)
                for name, settings in settings_by_name.items()
            ),
            key=lambda shard: not shard.master,
        )
    )
    masters = [shard.name for shard in shards if shard.master]
    if len(masters) != 1:
        msg = f"expected exactly one master shard, found {len(masters)}: {masters}"
        raise ValueError(msg)
    topology = cluster_structure(
        load_ini(
            root / "cluster.ini",
            ClusterSettings,
            include=frozenset(CLUSTER_STRUCTURE_FIELDS),
        ),
        settings_by_name,
    )
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
        for field in ("server_port", "master_server_port"):
            value = getattr(settings, field)
            owner = f"{shard.name}.{field}"
            if previous := ports.get(value):
                msg = f"UDP port {value} is shared by {previous} and {owner}"
                raise ValueError(msg)
            ports[value] = owner
    return shards, topology


def _error_paths(error: BaseException) -> tuple[FieldPath, ...]:
    if isinstance(error, ValidationError):
        paths = {
            tuple(map(str, item["loc"])) or ("configuration",)
            for item in error.errors(include_input=False, include_url=False)
        }
        return tuple(sorted(paths))
    return (("configuration",),)
