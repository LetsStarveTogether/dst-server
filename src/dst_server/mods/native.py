import asyncio
import os
import socket
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from .process import download_environment, positive_integer, redact, run_process

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


async def update(
    executable: Path,
    ugc_directory: Path,
    *,
    attempts: int = 5,
    proxy: str | None = None,
    log_handler: Callable[[str], None] | None = None,
) -> None:
    """Let DST execute its setup script, including native dynamic Lua."""
    attempts = positive_integer("Mod update attempts", attempts)
    environment = download_environment(proxy)
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
                failure = await _attempt(
                    command, executable.parent, environment, proxy, log_handler
                )
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
                        f"retrying ({attempt + 1}/{attempts}): {failure}"
                    )


async def _attempt(
    command: tuple[str, ...],
    directory: Path,
    environment: dict[str, str],
    proxy: str | None,
    log_handler: Callable[[str], None] | None,
) -> str | None:
    completed = False
    failure: str | None = None

    def on_line(line: str) -> None:
        nonlocal completed, failure
        text = redact(line.rstrip("\r\n"), (proxy,) if proxy else ())
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

    returncode = await run_process(
        *command, cwd=directory, environment=environment, on_line=on_line
    )
    if returncode:
        msg = f"DST mod updater exited with status {returncode}"
        raise ChildProcessError(msg)
    return failure or (None if completed else UPDATE_INCOMPLETE)


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
