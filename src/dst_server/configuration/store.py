from pathlib import Path

from pydantic import TypeAdapter

from .files import Shard, load_ini, shard_directories
from .models import (
    CLUSTER_STRUCTURE_FIELDS,
    ClusterConfig,
    ClusterSettings,
    ShardName,
    ShardSettings,
)

_SHARD_NAME = TypeAdapter(ShardName)


class ConfigurationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.shards = _deployment(directory)

    async def read(self) -> ClusterConfig:
        return ClusterConfig.load(self.directory)


def _deployment(root: Path) -> tuple[Shard, ...]:
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
    load_ini(
        root / "cluster.ini",
        ClusterSettings,
        include=frozenset(CLUSTER_STRUCTURE_FIELDS),
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
    return shards
