from __future__ import annotations

import os
import stat
from configparser import ConfigParser
from configparser import Error as ConfigError
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict

PERMISSION_FILES = ("adminlist.txt", "whitelist.txt", "blocklist.txt")


class ShardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    is_master: bool
    name: str | None
    shard_id: int | None

    @classmethod
    def load(cls, path: Path) -> ShardConfig:
        parser = ConfigParser(interpolation=None)
        try:
            with path.open(encoding="utf-8") as stream:
                parser.read_file(stream)
            is_master, name, shard_id = read_shard_section(parser)
        except (ConfigError, OSError, ValueError) as error:
            msg = f"invalid DST shard configuration: {path}: {error}"
            raise ValueError(msg) from error
        return cls(is_master=is_master, name=name, shard_id=shard_id)


@dataclass(frozen=True, slots=True)
class Shard:
    name: str
    path: Path
    config: ShardConfig
    console_path: Path


def read_shard_section(
    parser: ConfigParser,
) -> tuple[bool, str | None, int | None]:
    section = next(
        (value for value in parser.sections() if value.casefold() == "shard"),
        None,
    )
    if section is None:
        return True, None, None
    return (
        parser.getboolean(section, "is_master", fallback=True),
        parser.get(section, "name", fallback=None),
        parser.getint(section, "id", fallback=None),
    )


def discover_shards(cluster_path: Path) -> tuple[Shard, ...]:
    shards = []
    for path in sorted(cluster_path.iterdir(), key=lambda item: item.name.casefold()):
        if path.name == "mods" or not path.is_dir():
            continue
        server_ini = path / "server.ini"
        if not server_ini.is_file():
            raise FileNotFoundError(server_ini)
        config = ShardConfig.load(server_ini)
        console_path = (
            cluster_path / "console" if config.is_master else path / "console"
        )
        shards.append(
            Shard(
                name=path.name,
                path=path,
                config=config,
                console_path=console_path,
            )
        )

    if not shards:
        msg = f"no DST shard directories found in {cluster_path}"
        raise ValueError(msg)
    masters = [shard.name for shard in shards if shard.config.is_master]
    if len(masters) != 1:
        msg = f"expected exactly one master shard, found {len(masters)}: {masters}"
        raise ValueError(msg)
    return tuple(shards)


def prepare_cluster(cluster_path: Path) -> None:
    for name in ("cluster.ini", "cluster_token.txt"):
        path = cluster_path / name
        if not path.is_file():
            raise FileNotFoundError(path)
    for name in PERMISSION_FILES:
        (cluster_path / name).touch()


def ensure_fifo(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISFIFO(mode):
            return
        if stat.S_ISDIR(mode):
            msg = f"console path is a directory: {path}"
            raise IsADirectoryError(msg)
        path.unlink()
    os.mkfifo(path)


__all__ = [
    "Shard",
    "ShardConfig",
    "discover_shards",
    "ensure_fifo",
    "prepare_cluster",
]
