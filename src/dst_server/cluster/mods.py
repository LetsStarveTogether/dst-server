from __future__ import annotations

import asyncio
import os
import shutil
import socket
from collections.abc import Callable, Iterable
from pathlib import Path
from tempfile import TemporaryDirectory

from luaparser import ast
from luaparser.ast import SyntaxException
from luaparser.astnodes import (
    Call,
    Name,
    Return,
    Statement,
    String,
)

from dst_server.steamcmd import cleanup_process_tasks, terminate_process

from .config import _atomic_write, _configuration_file_exists
from .overrides import MAX_WORKSHOP_ID, WORKSHOP_MOD, _literal_return_table

SETUP_FUNCTIONS = {
    "ServerModSetup": 0,
    "ServerModCollectionSetup": 1,
}


async def _stream_logs(
    stdout: asyncio.StreamReader,
    log_handler: Callable[[str], None] | None,
) -> None:
    while line := await stdout.readline():
        if log_handler is not None:
            log_handler(line.decode(errors="replace").rstrip("\r\n"))


def prepare(  # ruff: ignore[complex-structure, too-many-branches, too-many-locals, too-many-statements]
    install_path: Path,
    cluster_path: Path,
) -> tuple[int, ...]:
    install_path = install_path.resolve()
    cluster_path = cluster_path.resolve()
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
    setup_path = mods_path / "dedicated_server_mods_setup.lua"
    modsettings_path = mods_path / "modsettings.lua"
    ugc_path = mods_path / "ugc"
    _validate_directory(mods_path)
    _validate_directory(ugc_path)
    _configuration_file_exists(setup_path)
    _configuration_file_exists(modsettings_path)

    existing_items, _ = setup_downloads(setup_path)
    override_paths = []
    for path in cluster_path.iterdir():
        if path.name == "mods":
            continue
        if path.is_symlink() and path.is_dir():
            msg = f"DST shard directory cannot be a symlink: {path}"
            raise ValueError(msg)
        if path.is_dir():
            override = path / "modoverrides.lua"
            _configuration_file_exists(override)
            override_paths.append(override)
    mod_ids = tuple(sorted(set(existing_items).union(workshop_ids(override_paths))))
    setup = _read_text(setup_path) if setup_path.is_file() else ""
    missing = sorted(set(mod_ids).difference(existing_items))
    if missing:
        generated = "".join(f'ServerModSetup("{mod_id}")\n' for mod_id in missing)
        if setup.startswith("#!"):
            shebang, _, setup = setup.partition("\n")
            generated = f"{shebang}\n{generated}"
        setup = generated + setup

    ugc_path.mkdir(parents=True, exist_ok=True)
    if not modsettings_path.exists():
        _atomic_write(modsettings_path, "", 0o644)
    if not setup_path.exists() or missing:
        _atomic_write(setup_path, setup, 0o644)

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

    return mod_ids


def _validate_directory(path: Path) -> None:
    if path.is_symlink():
        msg = f"managed DST directory cannot be a symlink: {path}"
        raise ValueError(msg)
    if path.exists() and not path.is_dir():
        msg = f"managed DST directory is not a directory: {path}"
        raise ValueError(msg)


def workshop_ids(paths: Iterable[Path]) -> tuple[int, ...]:
    ids = set()
    for path in paths:
        if not _configuration_file_exists(path):
            continue
        try:
            ids.update(_enabled_workshop_ids(path))
        except ValueError as error:
            msg = f"invalid DST mod override configuration: {path}: {error}"
            raise ValueError(msg) from error
    return tuple(sorted(ids))


def _enabled_workshop_ids(path: Path) -> set[int]:
    values = _literal_return_table(path, "modoverrides.lua", allow_empty=True)
    if "client_mods_disabled" in values and not isinstance(
        values.pop("client_mods_disabled"),
        bool,
    ):
        msg = "client_mods_disabled must be a literal boolean"
        raise ValueError(msg)

    enabled_ids: set[int] = set()
    for name, entry in values.items():
        if not isinstance(entry, dict):
            msg = "modoverrides.lua entries must be literal tables"
            raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
        match = WORKSHOP_MOD.fullmatch(name)
        if name.startswith("workshop-") and match is None:
            msg = f"invalid Workshop mod name: {name!r}"
            raise ValueError(msg)
        if match is None:
            continue
        workshop_id = int(match.group(1))
        if workshop_id > MAX_WORKSHOP_ID:
            msg = f"Workshop ID exceeds uint64: {workshop_id}"
            raise ValueError(msg)
        enabled = entry.get("enabled", False)
        if not isinstance(enabled, bool):
            msg = "Workshop mod enabled must be a literal boolean"
            raise ValueError(msg)  # ruff: ignore[type-check-without-type-error]
        if enabled:
            enabled_ids.add(workshop_id)
    return enabled_ids


def setup_downloads(path: Path) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if not path.is_file():
        return (), ()
    values = [set(), set()]
    statements = _setup_statements(path)
    for statement in statements:
        calls = statement.values if isinstance(statement, Return) else (statement,)
        for call in calls:
            if (
                not isinstance(call, Call)
                or not isinstance(call.func, Name)
                or (index := SETUP_FUNCTIONS.get(call.func.id)) is None
                or len(call.args) != 1
                or not isinstance(call.args[0], String)
            ):
                continue
            try:
                value = call.args[0].s.decode("ascii")
            except UnicodeDecodeError:
                continue
            if not value and index == SETUP_FUNCTIONS["ServerModSetup"]:
                continue
            if not value.isascii() or not value.isdigit() or value.startswith("0"):
                msg = f"invalid Workshop ID: {value!r}"
                raise ValueError(msg)
            workshop_id = int(value)
            if workshop_id > MAX_WORKSHOP_ID:
                msg = f"Workshop ID exceeds uint64: {workshop_id}"
                raise ValueError(msg)
            values[index].add(workshop_id)
    return tuple(sorted(values[0])), tuple(sorted(values[1]))


def has_setup_code(path: Path) -> bool:
    return path.is_file() and bool(_setup_statements(path))


def _setup_statements(path: Path) -> list[Statement]:
    try:
        return ast.parse(_read_text(path)).body.body
    except SyntaxException as error:
        msg = f"invalid DST Mod setup configuration: {path}: {error}"
        raise ValueError(msg) from error


def _read_text(path: Path) -> str:
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


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
        game_port, master_port = free_udp_ports(2)
        command = (
            str(executable),
            "-only_update_server_mods",
            "-monitor_parent_process",
            str(os.getpid()),
            "-port",
            str(game_port),
            "-steam_master_server_port",
            str(master_port),
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
            start_new_session=True,
        )
        stdout = process.stdout
        if stdout is None:
            await terminate_process(process)
            msg = "DST mod updater stdout pipe is unavailable"
            raise RuntimeError(msg)
        wait_task = asyncio.create_task(process.wait())
        log_task = asyncio.create_task(_stream_logs(stdout, log_handler))
        try:  # ruff:ignore[too-many-statements-in-try-clause]
            done, _ = await asyncio.wait(
                (wait_task, log_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if log_task in done:
                log_task.result()
            returncode = await wait_task
            await terminate_process(process)
            await log_task
        except BaseException as primary:
            cleanup_error = await cleanup_process_tasks(
                process,
                primary,
                wait_task,
                log_task,
            )
            if cleanup_error is not None:
                raise primary from cleanup_error
            raise
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
    "has_setup_code",
    "prepare",
    "setup_downloads",
    "update",
    "workshop_ids",
]
