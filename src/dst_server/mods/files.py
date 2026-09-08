import shutil
from collections.abc import Iterable
from pathlib import Path

from dst_server.configuration.files import (
    atomic_write,
    configuration_file_exists,
    read_text,
)
from dst_server.configuration.overrides import ModOverrides, scan_setup


def prepare_shared(cluster_path: Path) -> tuple[int, ...]:
    """Prepare shared files while preserving native executable setup scripts."""
    validate_directory(cluster_path)
    cluster_path = cluster_path.resolve()
    if not cluster_path.is_dir():
        raise NotADirectoryError(cluster_path)
    mods_path = cluster_path / "mods"
    setup = mods_path / "dedicated_server_mods_setup.lua"
    settings = mods_path / "modsettings.lua"
    ugc = mods_path / "ugc"
    validate_directory(mods_path)
    validate_directory(ugc)
    configuration_file_exists(setup)
    configuration_file_exists(settings)
    existing_items, _ = scan_setup(setup)
    override_paths = []
    for shard in cluster_path.iterdir():
        if shard.name == "mods":
            continue
        if shard.is_symlink() and shard.is_dir():
            msg = f"DST shard directory cannot be a symlink: {shard}"
            raise ValueError(msg)
        if shard.is_dir():
            override = shard / "modoverrides.lua"
            configuration_file_exists(override)
            override_paths.append(override)
    items = tuple(sorted(set(existing_items).union(workshop_ids(override_paths))))
    original = read_text(setup) if setup.is_file() else ""
    missing = sorted(set(items).difference(existing_items))
    if missing:
        generated = "".join(f'ServerModSetup("{item}")\n' for item in missing)
        if original.startswith("#!"):
            shebang, _, original = original.partition("\n")
            generated = f"{shebang}\n{generated}"
        original = generated + original
    ugc.mkdir(parents=True, exist_ok=True)
    if not settings.exists():
        atomic_write(settings, "", 0o644)
    if not setup.exists() or missing:
        atomic_write(setup, original, 0o644)
    return items


def workshop_ids(paths: Iterable[Path]) -> tuple[int, ...]:
    items: set[int] = set()
    for path in paths:
        if configuration_file_exists(path):
            try:
                items.update(ModOverrides.load(path).workshop_items)
            except ValueError as error:
                msg = f"invalid DST mod override configuration: {path}: {error}"
                raise ValueError(msg) from error
    return tuple(sorted(items))


def activate(install_path: Path, cluster_path: Path) -> None:
    install_path, cluster_path = install_path.resolve(), cluster_path.resolve()
    if not install_path.is_dir():
        raise NotADirectoryError(install_path)
    if not cluster_path.is_dir():
        raise NotADirectoryError(cluster_path)
    mods_path = cluster_path / "mods"
    install_mods = install_path / "mods"
    if (
        install_mods == mods_path
        or install_mods.is_relative_to(mods_path)
        or mods_path.is_relative_to(install_mods)
    ):
        msg = "install and cluster Mod directories cannot contain each other"
        raise ValueError(msg)
    validate_directory(mods_path)
    if install_mods.is_symlink():
        if install_mods.resolve() == mods_path.resolve():
            return
        install_mods.unlink()
    elif install_mods.is_dir():
        shutil.rmtree(install_mods)
    elif install_mods.exists():
        install_mods.unlink()
    install_mods.symlink_to(mods_path, target_is_directory=True)


def validate_directory(path: Path) -> None:
    if path.is_symlink():
        msg = f"managed DST directory cannot be a symlink: {path}"
        raise ValueError(msg)
    if path.exists() and not path.is_dir():
        msg = f"managed DST directory is not a directory: {path}"
        raise ValueError(msg)
