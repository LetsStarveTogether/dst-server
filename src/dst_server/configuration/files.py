import os
from collections.abc import Iterator, Mapping
from configparser import Error as ConfigError
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, SecretStr

from dst_server.lua_codec import LuaKey, LuaValue, literal_calls, parse_return_table

from .ini import parse_ini
from .models import (
    PERMISSION_FILES,
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
    _shared_cluster_key,
)
from .overrides import (
    LevelDataOverride,
    ModOverrides,
    ModSettings,
    WorkshopDownloads,
    WorldgenOverride,
    scan_setup,
)
from .world import WorldOverrides


def validate_directory(path: Path) -> None:
    if path.is_symlink():
        msg = f"configuration directory cannot be a symlink: {path}"
        raise ValueError(msg)
    if not path.is_dir():
        raise NotADirectoryError(path)


def configuration_file_exists(path: Path) -> bool:
    if path.is_symlink():
        msg = f"managed DST configuration cannot be a symlink: {path}"
        raise ValueError(msg)
    if path.exists() and not path.is_file():
        msg = f"managed DST configuration is not a file: {path}"
        raise ValueError(msg)
    return path.is_file()


def read_text(path: Path) -> str:
    if not configuration_file_exists(path):
        raise FileNotFoundError(path)
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()
    except (OSError, UnicodeError) as error:
        msg = f"invalid DST text configuration: {path}: {error}"
        raise ValueError(msg) from error


def write_files(  # ruff: ignore[complex-structure, too-many-branches, too-many-statements]
    root: Path,
    files: dict[Path, str],
    *,
    directories: tuple[Path, ...] = (),
) -> tuple[Path, ...]:
    if root.is_symlink():
        msg = f"configuration root cannot be a symlink: {root}"
        raise ValueError(msg)
    if root.exists() and not root.is_dir():
        msg = f"configuration root is not a directory: {root}"
        raise ValueError(msg)
    relatives = (*files, *directories)
    for path in relatives:
        if path.is_absolute() or not path.parts or ".." in path.parts:
            msg = f"unsafe relative configuration path: {path}"
            raise ValueError(msg)
        try:
            path.as_posix().encode("utf-8")
        except UnicodeEncodeError as error:
            msg = f"configuration paths must contain valid UTF-8: {path!r}"
            raise ValueError(msg) from error
    for content in files.values():
        try:
            content.encode("utf-8")
        except UnicodeEncodeError as error:
            msg = "DST configuration files must contain valid UTF-8"
            raise ValueError(msg) from error

    file_paths = set(files)
    if file_paths.intersection(directories):
        msg = "configuration path is both a file and directory"
        raise ValueError(msg)
    for relative in relatives:
        if any(parent in file_paths for parent in relative.parents):
            msg = f"configuration file is an ancestor of another path: {relative}"
            raise ValueError(msg)

    required_directories = set(directories)
    for relative in relatives:
        required_directories.update(
            parent for parent in relative.parents if parent != Path()
        )

    root_resolved = root.resolve()
    ordered_directories = sorted(
        required_directories,
        key=lambda path: (len(path.parts), path.as_posix()),
    )
    for relative in ordered_directories:
        path = root / relative
        if path.is_symlink():
            msg = f"managed DST directory cannot be a symlink: {path}"
            raise ValueError(msg)
        if path.exists() and not path.is_dir():
            msg = f"managed DST directory is not a directory: {path}"
            raise ValueError(msg)
    targets: set[Path] = set()
    for relative in files:
        path = root / relative
        if path.is_symlink():
            msg = f"managed DST configuration cannot be a symlink: {path}"
            raise ValueError(msg)
        if path.exists() and not path.is_file():
            msg = f"managed DST configuration is not a file: {path}"
            raise ValueError(msg)
        parent = path.parent.resolve()
        if not parent.is_relative_to(root_resolved):
            msg = f"configuration path escapes cluster root: {path}"
            raise ValueError(msg)
        target = parent / path.name
        if target in targets:
            msg = f"configuration paths resolve to the same target: {path}"
            raise ValueError(msg)
        targets.add(target)

    root.mkdir(parents=True, exist_ok=True)
    for relative in ordered_directories:
        (root / relative).mkdir(exist_ok=True)

    written = []
    for relative, content in sorted(files.items(), key=lambda item: item[0].as_posix()):
        path = root / relative
        mode = (
            0o600
            if relative.name
            in {
                "cluster.ini",
                "cluster_token.txt",
                "server.ini",
            }
            else 0o644
        )
        atomic_write(path, content, mode)
        written.append(path)
    return tuple(written)


def atomic_write(path: Path, content: str, mode: int) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_ini[Settings: BaseModel](
    path: Path, model: type[Settings], *, include: frozenset[str] | None = None
) -> Settings:
    try:
        return parse_ini(read_text(path), model, include=include)
    except ConfigError as error:
        line = getattr(error, "lineno", None)
        if line is None and (errors := getattr(error, "errors", ())):
            line = errors[0][0]
        location = f" at line {line}" if line is not None else ""
        msg = f"invalid DST INI configuration: {path}: {type(error).__name__}{location}"
        raise ValueError(msg) from None
    except OSError as error:
        msg = f"invalid DST INI configuration: {path}: {error}"
        raise ValueError(msg) from error


def load_shard(
    directory: Path,
    *,
    level_overrides_type: type[WorldOverrides] | None = None,
    world_overrides_type: type[WorldOverrides] | None = None,
) -> ShardConfig:
    validate_directory(directory)
    server_ini = directory / "server.ini"
    if not configuration_file_exists(server_ini):
        raise FileNotFoundError(server_ini)
    values: dict[str, object] = {"settings": ShardSettings.load(server_ini)}
    modoverrides = directory / "modoverrides.lua"
    if configuration_file_exists(modoverrides):
        values["mods"] = ModOverrides.load(modoverrides)
    leveldataoverride = directory / "leveldataoverride.lua"
    if configuration_file_exists(leveldataoverride):
        level = LevelDataOverride.load(
            leveldataoverride,
            overrides_type=level_overrides_type,
        )
        values["level"] = level
    worldgenoverride = directory / "worldgenoverride.lua"
    if configuration_file_exists(worldgenoverride):
        values["world"] = WorldgenOverride.load(
            worldgenoverride,
            overrides_type=world_overrides_type,
        )
    return ShardConfig.model_validate(values)


def load_cluster(  # ruff: ignore[complex-structure]
    directory: Path,
    *,
    level_overrides_types: Mapping[str, type[WorldOverrides]] | None = None,
    world_overrides_types: Mapping[str, type[WorldOverrides]] | None = None,
) -> ClusterConfig:
    validate_directory(directory)
    cluster_ini = directory / "cluster.ini"
    token_path = directory / "cluster_token.txt"
    if not configuration_file_exists(cluster_ini):
        raise FileNotFoundError(cluster_ini)
    if not configuration_file_exists(token_path):
        raise FileNotFoundError(token_path)

    shards = {}
    for shard in shard_directories(directory):
        shards[shard.name] = ShardConfig.load(
            shard,
            level_overrides_type=(level_overrides_types or {}).get(shard.name),
            world_overrides_type=(world_overrides_types or {}).get(shard.name),
        )
    token = read_text(token_path).removesuffix("\r\n").removesuffix("\n")
    settings = ClusterSettings.load(cluster_ini)
    if len(shards) > 1 and "shard_enabled" not in settings.model_fields_set:
        msg = "shard_enabled = true is required in an existing multi-shard cluster"
        raise ValueError(msg)
    if len(shards) > 1 or settings.shard_enabled:
        implicit = sorted(
            name
            for name, shard in shards.items()
            if "is_master" not in shard.settings.model_fields_set
        )
        if implicit:
            msg = (
                "is_master is required in every existing sharded server.ini: "
                f"{implicit}"
            )
            raise ValueError(msg)
    values: dict[str, object] = {
        "settings": settings,
        "shards": shards,
        "token": SecretStr(token),
    }
    for name in PERMISSION_FILES:
        path = directory / name
        if configuration_file_exists(path):
            values[path.stem] = read_text(path).replace(
                "\r\n",
                "\n",
            )
    mods = directory / "mods"
    if mods.exists() or mods.is_symlink():
        validate_directory(mods)
    modsettings = mods / "modsettings.lua"
    if configuration_file_exists(modsettings):
        values["mod_settings"] = ModSettings.load(modsettings)
    setup = mods / "dedicated_server_mods_setup.lua"
    if configuration_file_exists(setup):
        values["downloads"] = WorkshopDownloads.load(setup)
    return ClusterConfig.model_validate(values)


def save_cluster(  # ruff: ignore[complex-structure, too-many-branches, too-many-locals]
    configuration: ClusterConfig, directory: Path
) -> tuple[Path, ...]:
    validated = ClusterConfig.model_validate(configuration)
    if directory.is_symlink():
        msg = f"configuration root cannot be a symlink: {directory}"
        raise ValueError(msg)
    if directory.exists() and not directory.is_dir():
        msg = f"configuration root is not a directory: {directory}"
        raise ValueError(msg)
    if directory.is_dir():
        expected = set(validated.shards) | {"mods"}
        unexpected = sorted(
            path.name
            for path in directory.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and path.name not in expected
        )
        if unexpected:
            msg = f"unmanaged shard directories would remain active: {unexpected}"
            raise ValueError(msg)
        for name in expected:
            path = directory / name
            if path.is_symlink():
                msg = f"managed DST directory cannot be a symlink: {path}"
                raise ValueError(msg)
            if path.exists() and not path.is_dir():
                msg = f"managed DST directory is not a directory: {path}"
                raise ValueError(msg)
    if (
        _shared_cluster_key(
            validated.settings,
            (shard.settings for shard in validated.shards.values()),
        )
        is None
    ):
        cluster_ini = directory / "cluster.ini"
        existing_settings = (
            ClusterSettings.load(cluster_ini)
            if configuration_file_exists(cluster_ini)
            else ClusterSettings()
        )
        existing_shards = [
            ShardSettings.load(path)
            for name in validated.shards
            if configuration_file_exists(path := directory / name / "server.ini")
        ]
        validated = ClusterConfig.model_validate(
            validated,
            context={
                "cluster_key": _shared_cluster_key(existing_settings, existing_shards)
            },
        )
    files = validated.files()
    token_path = Path("cluster_token.txt")
    preserved = [token_path, *(Path(name) for name in PERMISSION_FILES)]
    setup = Path("mods/dedicated_server_mods_setup.lua")
    for path in (*preserved, Path("mods/modsettings.lua"), setup):
        target = directory / path
        if target.is_symlink():
            msg = f"managed DST configuration cannot be a symlink: {target}"
            raise ValueError(msg)
    if "token" not in validated.model_fields_set and (directory / token_path).is_file():
        files.pop(token_path)
    for name in PERMISSION_FILES:
        path = Path(name)
        if path.stem not in validated.model_fields_set and (directory / path).is_file():
            files.pop(path)
    modsettings = Path("mods/modsettings.lua")
    if (
        "mod_settings" not in validated.model_fields_set
        and (directory / modsettings).is_file()
    ):
        existing_items, existing_collections = scan_setup(directory / setup)
        downloads = validated.resolved_downloads()
        files[setup] = WorkshopDownloads(
            items=downloads.items.union(existing_items),
            collections=downloads.collections.union(existing_collections),
        ).render()
        files.pop(modsettings)

    return write_files(directory, files, directories=(Path("mods/ugc"),))


@dataclass(frozen=True, slots=True)
class Shard:
    name: str
    master: bool


def shard_directories(cluster: Path) -> tuple[Path, ...]:
    validate_directory(cluster)
    directories = []
    for path in sorted(cluster.iterdir(), key=lambda item: item.name.casefold()):
        if path.name == "mods" or path.name.startswith("."):
            continue
        if path.is_symlink():
            if path.is_dir():
                msg = f"DST shard directory cannot be a symlink: {path}"
                raise ValueError(msg)
            continue
        if path.is_dir() and configuration_file_exists(path / "server.ini"):
            directories.append(path)
    if not directories:
        msg = f"no DST shard directories found in {cluster}"
        raise ValueError(msg)
    return tuple(directories)


def read_master(path: Path) -> bool:
    return load_ini(path, ShardSettings, include=frozenset({"is_master"})).is_master


def discover(cluster: Path) -> tuple[Shard, ...]:
    shards = []
    for path in shard_directories(cluster):
        master = read_master(path / "server.ini")
        shards.append(Shard(path.name, master))
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


def load_lua_table(
    path: Path, description: str, *, allow_empty: bool = False
) -> dict[LuaKey, LuaValue]:

    return parse_return_table(read_text(path), description, allow_empty=allow_empty)


def load_lua_calls(
    path: Path, description: str
) -> Iterator[tuple[str, tuple[LuaValue, ...]]]:

    return literal_calls(read_text(path), description)
