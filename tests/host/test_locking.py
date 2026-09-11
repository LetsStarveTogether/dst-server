import asyncio
import errno
import os
import stat
import sys
from pathlib import Path

import pytest

from dst_server.host import locking
from dst_server.host.locking import room_lock


async def test_cross_process_wait_busy_and_release(tmp_path: Path) -> None:
    lockfile = tmp_path / ".dst-operation.lock"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import fcntl, sys\n"
        "with open(sys.argv[1], 'a') as lock:\n"
        "    fcntl.flock(lock, fcntl.LOCK_EX)\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.read(1)\n",
        str(lockfile),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    entered = asyncio.Event()

    async def acquire() -> None:
        async with room_lock(tmp_path):
            entered.set()

    waiter: asyncio.Task[None] | None = None
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"locked\n"
        with pytest.raises(RuntimeError, match="room operation is busy"):
            async with room_lock(tmp_path, wait=False):
                pytest.fail("a second process acquired the busy lock")
        waiter = asyncio.create_task(acquire())
        await asyncio.sleep(0.1)
        assert not entered.is_set()
        process.stdin.write(b"x")
        await process.stdin.drain()
        await asyncio.wait_for(waiter, 5)
        assert entered.is_set()
        assert await asyncio.wait_for(process.wait(), 5) == 0
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        if process.returncode is None:
            process.kill()
        await process.wait()
        process.stdin.close()
        await process.stdin.wait_closed()


@pytest.mark.parametrize("waiting", [False, True])
async def test_cancellation_closes_descriptor_and_releases_owned_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waiting: bool
) -> None:
    descriptors: list[int] = []
    opened = os.open
    entered = asyncio.Event()

    def record_open(path: Path, flags: int, mode: int) -> int:
        descriptor = opened(path, flags, mode)
        descriptors.append(descriptor)
        return descriptor

    async def acquire() -> None:
        async with room_lock(tmp_path):
            entered.set()
            await asyncio.Event().wait()

    async with room_lock(tmp_path, name=".dst-operation.lock" if waiting else "other"):
        monkeypatch.setattr(locking.os, "open", record_open)
        task = asyncio.create_task(acquire())
        if waiting:
            await asyncio.sleep(0)
            assert not entered.is_set()
        else:
            await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if waiting:
            with pytest.raises(RuntimeError, match="busy"):
                async with room_lock(tmp_path, wait=False):
                    pytest.fail("cancelling a waiter released another owner's lock")

    for descriptor in descriptors:
        with pytest.raises(OSError, match=r"\[Errno 9\]") as failure:
            os.fstat(descriptor)
        assert failure.value.errno == errno.EBADF
    async with room_lock(tmp_path, wait=False):
        pass


async def test_exception_releases_lock_and_keeps_reusable_private_file(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "new" / "room"
    msg = "operation failed"
    with pytest.raises(LookupError, match="operation failed"):
        async with room_lock(directory):
            raise LookupError(msg)
    lockfile = directory / ".dst-operation.lock"
    inode = lockfile.stat().st_ino
    assert stat.S_IMODE(lockfile.stat().st_mode) == 0o600
    async with room_lock(directory, wait=False):
        assert lockfile.stat().st_ino == inode


@pytest.mark.parametrize("name", ["", ".", "..", "../outside", "/absolute"])
async def test_invalid_filename_is_rejected_before_creating_directory(
    tmp_path: Path, name: str
) -> None:
    directory = tmp_path / "absent"
    with pytest.raises(ValueError, match="invalid lock filename"):
        async with room_lock(directory, name=name):
            pytest.fail("unsafe filename was accepted")
    assert not directory.exists()


async def test_symlink_room_directory_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    directory = tmp_path / "room"
    directory.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="room directory cannot be a symlink"):
        async with room_lock(directory):
            pytest.fail("symlink room was accepted")
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
async def test_special_lock_file_is_rejected_without_touching_target(
    tmp_path: Path, kind: str
) -> None:
    lockfile = tmp_path / ".dst-operation.lock"
    target = tmp_path / "target"
    target.write_text("untouched", encoding="utf-8")
    if kind == "symlink":
        lockfile.symlink_to(target)
        expected = OSError
        match = "Too many levels of symbolic links"
    elif kind == "fifo":
        os.mkfifo(lockfile)
        expected = ValueError
        match = "room lock must be a regular file"
    else:
        lockfile.mkdir()
        expected = IsADirectoryError
        match = "Is a directory"
    with pytest.raises(expected, match=match):
        async with room_lock(tmp_path):
            pytest.fail("special lock file was accepted")
    assert target.read_text(encoding="utf-8") == "untouched"
