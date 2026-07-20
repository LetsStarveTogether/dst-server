from __future__ import annotations

from configparser import ConfigParser
from configparser import Error as ConfigError
from dataclasses import dataclass
from pathlib import Path

PERMISSION_FILES = ("adminlist.txt", "whitelist.txt", "blocklist.txt")


@dataclass(frozen=True, slots=True)
class Shard:
    name: str
    master: bool
    console: Path


def read_master(path: Path) -> bool:
    parser = ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
        section = next(
            (value for value in parser.sections() if value.casefold() == "shard"),
            None,
        )
        return (
            True
            if section is None
            else parser.getboolean(section, "is_master", fallback=True)
        )
    except (ConfigError, OSError, ValueError) as error:
        msg = f"invalid DST shard configuration: {path}: {error}"
        raise ValueError(msg) from error


def discover(cluster: Path) -> tuple[Shard, ...]:
    shards = []
    for path in sorted(cluster.iterdir(), key=lambda item: item.name.casefold()):
        if path.name == "mods" or not path.is_dir():
            continue
        server_ini = path / "server.ini"
        if not server_ini.is_file():
            raise FileNotFoundError(server_ini)
        master = read_master(server_ini)
        shards.append(
            Shard(
                name=path.name,
                master=master,
                console=cluster / "console" if master else path / "console",
            )
        )

    if not shards:
        msg = f"no DST shard directories found in {cluster}"
        raise ValueError(msg)
    masters = [shard.name for shard in shards if shard.master]
    if len(masters) != 1:
        msg = f"expected exactly one master shard, found {len(masters)}: {masters}"
        raise ValueError(msg)
    return tuple(shards)


def prepare(cluster: Path) -> None:
    for name in ("cluster.ini", "cluster_token.txt"):
        path = cluster / name
        if not path.is_file():
            raise FileNotFoundError(path)
    for name in PERMISSION_FILES:
        path = cluster / name
        if not path.is_file():
            path.touch()


__all__ = ["Shard", "discover", "prepare"]
