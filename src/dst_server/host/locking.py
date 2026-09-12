# ruff: file-ignore[blocking-path-method-in-async-function]
import asyncio
import fcntl
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path


class RoomBusyError(RuntimeError):
    """Another host command currently owns this room."""


@asynccontextmanager
async def room_lock(directory: Path, *, wait: bool = False) -> AsyncIterator[None]:
    if directory.is_symlink():
        msg = f"room directory cannot be a symlink: {directory}"
        raise ValueError(msg)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".dst-operation.lock"
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            msg = f"room lock must be a regular file: {path}"
            raise ValueError(msg)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    msg = f"room operation is busy: {path}"
                    raise RoomBusyError(msg) from None
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
