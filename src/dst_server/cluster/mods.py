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
    SemiColon,
    Statement,
    String,
)

from dst_server.steamcmd import (
    cleanup_process_tasks,
    positive_integer,
    terminate_process,
)

from .config import _atomic_write, _configuration_file_exists
from .overrides import MAX_WORKSHOP_ID, ModOverrides

SETUP_FUNCTIONS = {
    "ServerModSetup": 0,
    "ServerModCollectionSetup": 1,
}
UPDATE_PROCESS_TIMEOUT = 30 * 60
UPDATE_COMPLETE = (
    "FinishDownloadingServerMods Complete! Process trying to quit nicely.."
)
DOWNLOAD_TIMEOUT = "DownloadServerMods timed out with no response from Workshop..."
UPDATE_INCOMPLETE = "DST mod updater exited without reporting completion"
SETUP_FAILURE = "#ERROR: Failure to load dedicated_server_mods_setup.lua:"
DOWNLOAD_FAILURES = (
    "[Workshop] ItemQuery failed entirely, unrecoverable.",
    "[Workshop] CollectionQuery failed entirely, unrecoverable.",
    "[Workshop] ODPF failed entirely: ",
    "[Workshop] FAILED: DownloadPublishedFile [",
)


async def _stream_logs(
    stdout: asyncio.StreamReader,
    log_handler: Callable[[str], None] | None,
) -> str | None:
    completed = False
    failure = None
    while line := await stdout.readline():
        text = line.decode(errors="replace").rstrip("\r\n")
        if log_handler is not None:
            log_handler(text)
        message = text.split("]: ", 1)[-1].rstrip()
        if message == UPDATE_COMPLETE:
            completed = True
        if message.startswith(SETUP_FAILURE) or (
            failure is None
            and (message == DOWNLOAD_TIMEOUT or message.startswith(DOWNLOAD_FAILURES))
        ):
            failure = message
    return failure or (None if completed else UPDATE_INCOMPLETE)


def prepare_shared(
    cluster_path: Path,
) -> tuple[int, ...]:
    cluster_path = cluster_path.resolve()
    if not cluster_path.is_dir():
        raise NotADirectoryError(cluster_path)
    mods_path = cluster_path / "mods"
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

    return mod_ids


def _validate_activation_paths(
    install_path: Path,
    cluster_path: Path,
) -> tuple[Path, Path]:
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
    return install_path, cluster_path


def activate(install_path: Path, cluster_path: Path) -> None:
    install_path, cluster_path = _validate_activation_paths(
        install_path,
        cluster_path,
    )
    mods_path = cluster_path / "mods"
    install_mods = install_path / "mods"
    _validate_directory(mods_path)

    if install_mods.is_symlink():
        if install_mods.resolve() == mods_path.resolve():
            return
        install_mods.unlink()
    elif install_mods.is_dir():
        shutil.rmtree(install_mods)
    elif install_mods.exists():
        install_mods.unlink()
    install_mods.symlink_to(mods_path, target_is_directory=True)


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
    return set(ModOverrides.load(path).workshop_items)


def setup_downloads(
    path: Path,
    *,
    strict: bool = False,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    if not path.is_file():
        return (), ()
    values = [set(), set()]
    statements = _setup_statements(path)
    for statement in statements:
        calls = statement.values if isinstance(statement, Return) else (statement,)
        for call in calls or (statement,):
            if (
                not isinstance(call, Call)
                or not isinstance(call.func, Name)
                or (index := SETUP_FUNCTIONS.get(call.func.id)) is None
                or len(call.args) != 1
                or not isinstance(call.args[0], String)
            ):
                if strict and not isinstance(call, SemiColon):
                    msg = (
                        "SteamCMD Mod setup requires only static ServerModSetup "
                        "and ServerModCollectionSetup string calls"
                    )
                    raise ValueError(msg)
                continue
            value = call.args[0].s.decode("ascii", errors="replace")
            if not strict and not value.isascii():
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
    attempts: int = 5,
    log_handler: Callable[[str], None] | None = None,
) -> None:
    attempts = positive_integer("Mod update attempts", attempts)
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
        async with asyncio.timeout(UPDATE_PROCESS_TIMEOUT):
            for attempt in range(1, attempts + 1):
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=executable.parent,
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
                    failure = await log_task
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
                if failure is None:
                    return
                if (
                    failure == UPDATE_INCOMPLETE
                    or failure.startswith(SETUP_FAILURE)
                    or attempt == attempts
                ):
                    msg = (
                        f"DST mod updater failed after {attempt} attempt(s): {failure}"
                    )
                    raise RuntimeError(msg)
                if log_handler is not None:
                    log_handler(
                        "DST mod update incomplete; "
                        f"retrying ({attempt + 1}/{attempts}): "
                        f"{failure}"
                    )


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
