import asyncio
import os
import signal
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from dst_server.concurrency import cancel_tasks, complete

TERMINATE_GRACE_PERIOD = 5.0


async def run_process(
    *command: str,
    cwd: Path | None,
    environment: dict[str, str],
    on_line: Callable[[str], None],
) -> int:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=1024 * 1024,
        start_new_session=True,
    )
    if process.stdout is None:
        await complete(terminate_process(process))
        msg = "Mod updater stdout pipe is unavailable"
        raise RuntimeError(msg)
    waiting = asyncio.create_task(process.wait())
    reading = asyncio.create_task(_read_output(process.stdout, on_line))
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        done, _ = await asyncio.wait(
            (waiting, reading), return_when=asyncio.FIRST_COMPLETED
        )
        if reading in done:
            reading.result()
        returncode = await waiting
        # Descendants can retain stdout after the command itself has exited.
        await complete(terminate_process(process))
        await reading
    except BaseException as primary:
        try:
            await complete(_cleanup(process, waiting, reading))
        except BaseException as error:
            raise primary from error
        raise
    return returncode


async def _read_output(
    stdout: asyncio.StreamReader, on_line: Callable[[str], None]
) -> None:
    while line := await stdout.readline():
        on_line(line.decode(errors="replace"))


async def _cleanup(
    process: asyncio.subprocess.Process, *tasks: asyncio.Task[object]
) -> None:
    try:
        await terminate_process(process)
    finally:
        await cancel_tasks(*tasks)


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        signal_process_group(process.pid, signal.SIGTERM)
        try:
            async with asyncio.timeout(TERMINATE_GRACE_PERIOD):
                await process.wait()
        except TimeoutError:
            signal_process_group(process.pid, signal.SIGKILL)
            await process.wait()
    signal_process_group(process.pid, signal.SIGKILL)


def signal_process_group(process_id: int, value: signal.Signals) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process_id, value)


def positive_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)
    return value


def validate_argument(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        msg = f"{name} must not be empty"
        raise ValueError(msg)
    if not value.isprintable():
        msg = f"{name} must not contain control characters"
        raise ValueError(msg)
    return value


def validate_proxy(proxy: str | None) -> str | None:
    if proxy is not None:
        try:
            address = urlsplit(validate_argument("download proxy", proxy))
            valid = (
                address.scheme in {"http", "https"}
                and bool(address.hostname)
                and (address.port is None or address.port > 0)
            )
        except ValueError:
            valid = False
        if not valid:
            msg = "download proxy must be an HTTP(S) URL with a valid host and port"
            raise ValueError(msg)
    return proxy


def download_environment(proxy: str | None = None) -> dict[str, str]:
    proxy = validate_proxy(proxy)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.lower()
        not in {"http_proxy", "https_proxy", "all_proxy", "ftp_proxy", "no_proxy"}
    }
    if proxy is not None:
        environment.update(dict.fromkeys(("http_proxy", "https_proxy"), proxy))
    return environment


def redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        value = value.replace(secret, "***")
    return value
