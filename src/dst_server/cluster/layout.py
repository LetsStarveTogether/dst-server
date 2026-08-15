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


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        match value.casefold():
            case "true":
                return True
            case "false":
                return False
    msg = f"DST booleans must be true or false, got {value!r}"
    raise ValueError(msg)


def read_master(path: Path) -> bool:
    parser = ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (ConfigError, OSError) as error:
        msg = f"invalid DST shard configuration: {path}: {error}"
        raise ValueError(msg) from error
    sections = [value for value in parser.sections() if value.casefold() == "shard"]
    if len(sections) > 1:
        msg = f"duplicate DST INI section: {sections[1]}"
        raise ValueError(msg)
    if not sections:
        return True
    return _parse_bool(parser.get(sections[0], "is_master", fallback="true"))


def discover(cluster: Path) -> tuple[Shard, ...]:
    shards = []
    for path in sorted(cluster.iterdir(), key=lambda item: item.name.casefold()):
        if path.name == "mods":
            continue
        if path.is_symlink():
            if path.is_dir():
                msg = f"DST shard directory cannot be a symlink: {path}"
                raise ValueError(msg)
            continue
        if not path.is_dir():
            continue
        server_ini = path / "server.ini"
        if server_ini.is_symlink():
            msg = f"DST shard configuration cannot be a symlink: {server_ini}"
            raise ValueError(msg)
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
        if path.is_symlink():
            msg = f"DST cluster configuration cannot be a symlink: {path}"
            raise ValueError(msg)
        if not path.is_file():
            raise FileNotFoundError(path)
    missing = []
    for name in PERMISSION_FILES:
        path = cluster / name
        if path.is_symlink():
            msg = f"DST permission file cannot be a symlink: {path}"
            raise ValueError(msg)
        if path.exists():
            if not path.is_file():
                msg = f"DST permission path is not a file: {path}"
                raise ValueError(msg)
        else:
            missing.append(path)
    for path in missing:
        path.touch(exist_ok=False)


__all__ = ["Shard", "discover", "prepare"]
