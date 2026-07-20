from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
from collections.abc import Callable, Iterable
from pathlib import Path
from tempfile import TemporaryDirectory

WORKSHOP_ID = re.compile(r"\bworkshop-(\d+)\b")


def prepare(install_path: Path, cluster_path: Path) -> tuple[int, ...]:
    mods_path = cluster_path / "mods"
    ugc_path = mods_path / "ugc"
    mods_path.mkdir(parents=True, exist_ok=True)
    ugc_path.mkdir(parents=True, exist_ok=True)
    (mods_path / "modsettings.lua").touch()

    install_mods = install_path / "mods"
    if install_mods.is_symlink():
        if install_mods.resolve() != mods_path.resolve():
            install_mods.unlink()
            install_mods.symlink_to(mods_path, target_is_directory=True)
    else:
        if install_mods.is_dir():
            shutil.rmtree(install_mods)
        elif install_mods.exists():
            install_mods.unlink()
        install_mods.symlink_to(mods_path, target_is_directory=True)

    mod_ids = workshop_ids(
        path / "modoverrides.lua"
        for path in cluster_path.iterdir()
        if path.is_dir() and path.name != "mods"
    )
    setup = "".join(f'ServerModSetup("{mod_id}")\n' for mod_id in mod_ids)
    (mods_path / "dedicated_server_mods_setup.lua").write_text(
        setup,
        encoding="utf-8",
    )
    return mod_ids


def workshop_ids(paths: Iterable[Path]) -> tuple[int, ...]:
    ids = {
        int(match)
        for path in paths
        if path.is_file()
        for match in WORKSHOP_ID.findall(path.read_text(encoding="utf-8"))
    }
    return tuple(sorted(ids))


async def update(
    executable: Path,
    ugc_directory: Path,
    *,
    proxy_url: str | None = None,
    log_handler: Callable[[str], None] | None = None,
) -> None:
    with TemporaryDirectory(prefix="dst-mod-update-") as temporary:
        root = Path(temporary)
        (root / "conf" / "cluster" / "shard").mkdir(parents=True)
        game_port, master_port, authentication_port = free_udp_ports(3)
        command = (
            str(executable),
            "-only_update_server_mods",
            "-monitor_parent_process",
            str(os.getpid()),
            "-port",
            str(game_port),
            "-steam_master_server_port",
            str(master_port),
            "-steam_authentication_port",
            str(authentication_port),
            "-ugc_directory",
            str(ugc_directory),
            "-persistent_storage_root",
            str(root),
            "-conf_dir",
            "conf",
            "-cluster",
            "cluster",
            "-shard",
            "shard",
        )
        environment = None
        if proxy_url is not None:
            environment = os.environ | {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
            }
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=executable.parent,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout = process.stdout
        if stdout is None:
            msg = "DST mod updater stdout pipe is unavailable"
            raise RuntimeError(msg)
        while line := await stdout.readline():
            if log_handler is not None:
                log_handler(line.decode(errors="replace").rstrip("\r\n"))
        returncode = await process.wait()
        if returncode:
            msg = f"DST mod updater exited with status {returncode}"
            raise ChildProcessError(msg)


def free_udp_ports(count: int) -> tuple[int, ...]:
    sockets = []
    try:
        for _ in range(count):
            value = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            value.bind(("", 0))
            sockets.append(value)
        return tuple(value.getsockname()[1] for value in sockets)
    finally:
        for value in sockets:
            value.close()


__all__ = [
    "prepare",
    "update",
    "workshop_ids",
]
