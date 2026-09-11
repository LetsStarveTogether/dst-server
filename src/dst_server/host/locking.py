# ruff: file-ignore[blocking-path-method-in-async-function]
import asyncio
import fcntl
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

MOD_LOCK = ".dst-mod-update.lock"


@asynccontextmanager
async def room_lock(
    directory: Path, *, name: str = ".dst-operation.lock", wait: bool = True
) -> AsyncIterator[None]:
    if not name or Path(name).name != name or name in {".", ".."}:
        msg = f"invalid lock filename: {name}"
        raise ValueError(msg)
    if directory.is_symlink():
        msg = f"room directory cannot be a symlink: {directory}"
        raise ValueError(msg)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        directory / name,
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            msg = f"room lock must be a regular file: {directory / name}"
            raise ValueError(msg)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    msg = f"room operation is busy: {directory / name}"
                    raise RuntimeError(msg) from None
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
