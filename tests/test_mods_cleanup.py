from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from dst_server import steamcmd
from dst_server.cluster import mods

from .helpers import process_stopped

FAKE_UPDATER = r"""#!/usr/bin/env python3
import os
import signal
import subprocess
import sys

signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.pause()",
])
print(f"READY|{os.getpid()}|{os.getpgrp()}|{child.pid}", flush=True)
signal.pause()
"""

EXITING_UPDATER = r"""#!/usr/bin/env python3
import os
import signal
import subprocess
import sys

child = subprocess.Popen([
    sys.executable,
    "-c",
    "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); signal.pause()",
])
print(f"READY|{os.getpid()}|{os.getpgrp()}|{child.pid}", flush=True)
print("DONE", flush=True)
sys.exit(int(os.environ["FAKE_RETURN_CODE"]))
"""


@pytest.mark.parametrize("existing", ["directory", "file", "symlink"])
def test_prepare_validates_overrides_before_replacing_install_mods(
    tmp_path: Path,
    existing: str,
) -> None:
    install = tmp_path / "install"
    install.mkdir()
    install_mods = install / "mods"
    sentinel = b"keep"
    if existing == "directory":
        install_mods.mkdir()
        (install_mods / "sentinel").write_bytes(sentinel)
    elif existing == "file":
        install_mods.write_bytes(sentinel)
    else:
        old_mods = tmp_path / "old-mods"
        old_mods.mkdir()
        (old_mods / "sentinel").write_bytes(sentinel)
        install_mods.symlink_to(old_mods, target_is_directory=True)

    cluster = tmp_path / "cluster"
    shard = cluster / "forest"
    shard.mkdir(parents=True)
    (shard / "modoverrides.lua").write_bytes(b"\xff")

    with pytest.raises(ValueError, match=r"invalid.*utf-8"):
        mods.prepare(install, cluster)

    if existing == "file":
        assert install_mods.read_bytes() == sentinel
    else:
        assert (install_mods / "sentinel").read_bytes() == sentinel
    assert not (cluster / "mods").exists()


def test_prepare_rejects_managed_setup_symlink_before_replacing_install_mods(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    install_mods = install / "mods"
    install_mods.mkdir(parents=True)
    sentinel = install_mods / "keep"
    sentinel.touch()
    cluster_mods = tmp_path / "cluster" / "mods"
    cluster_mods.mkdir(parents=True)
    outside = tmp_path / "outside.lua"
    outside.write_text('ServerModSetup("42")\n', encoding="utf-8")
    (cluster_mods / "dedicated_server_mods_setup.lua").symlink_to(outside)

    with pytest.raises(ValueError, match="configuration cannot be a symlink"):
        mods.prepare(install, tmp_path / "cluster")

    assert sentinel.is_file()
    assert outside.read_text(encoding="utf-8") == 'ServerModSetup("42")\n'


def test_prepare_rejects_overlapping_install_and_cluster_mod_directories(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    cluster = install / "mods" / "cluster"
    cluster.mkdir(parents=True)
    sentinel = cluster / "keep"
    sentinel.touch()

    with pytest.raises(ValueError, match="cannot contain each other"):
        mods.prepare(install, cluster)

    assert sentinel.is_file()
    assert not (cluster / "mods").exists()


def test_prepare_rejects_invalid_setup_before_mutation(tmp_path: Path) -> None:
    install = tmp_path / "install"
    install_mods = install / "mods"
    install_mods.mkdir(parents=True)
    sentinel = install_mods / "keep"
    sentinel.touch()
    cluster = tmp_path / "cluster"
    cluster_mods = cluster / "mods"
    cluster_mods.mkdir(parents=True)
    setup = cluster_mods / "dedicated_server_mods_setup.lua"
    setup.write_text("this is not Lua }", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid DST Mod setup"):
        mods.prepare(install, cluster)

    assert setup.read_text(encoding="utf-8") == "this is not Lua }"
    assert sentinel.is_file()
    assert not (cluster_mods / "ugc").exists()
    assert not (cluster_mods / "modsettings.lua").exists()


def test_prepare_rejects_shard_override_symlink_before_mutation(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    install.mkdir()
    cluster = tmp_path / "cluster"
    shard = cluster / "Master"
    shard.mkdir(parents=True)
    outside = tmp_path / "outside.lua"
    outside.write_text(
        'return { ["workshop-42"] = { enabled = true } }',
        encoding="utf-8",
    )
    (shard / "modoverrides.lua").symlink_to(outside)

    with pytest.raises(ValueError, match="configuration cannot be a symlink"):
        mods.prepare(install, cluster)

    assert not (cluster / "mods").exists()
    assert outside.read_text(encoding="utf-8").endswith("true } }")


def write_updater(
    tmp_path: Path,
    source: str = FAKE_UPDATER,
) -> tuple[Path, Path]:
    executable = tmp_path / "fake-updater"
    executable.write_text(source, encoding="utf-8")
    executable.chmod(0o755)
    ugc = tmp_path / "ugc"
    ugc.mkdir()
    return executable, ugc


def parse_processes(line: str) -> tuple[int, int]:
    _, process, group, child = line.split("|")
    assert process == group
    return int(process), int(child)


async def assert_stopped(processes: tuple[int, int]) -> None:
    assert all(
        await asyncio.gather(
            *(asyncio.to_thread(process_stopped, process) for process in processes)
        )
    )


@pytest.mark.parametrize(
    ("cancelled", "interrupt_cleanup"),
    [(False, False), (True, False), (False, True), (True, True)],
)
async def test_cleanup_failure_preserves_primary_and_reaps_tasks(  # ruff:ignore[complex-structure]
    cancelled: bool,
    interrupt_cleanup: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reader:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.line = None if cancelled else b"READY\n"

        async def readline(self) -> bytes:
            if self.line is not None:
                line, self.line = self.line, None
                return line
            self.started.set()
            await asyncio.Event().wait()
            return b""

    class Process:
        pid = 1
        returncode = None

        def __init__(self) -> None:
            self.stdout = Reader()

        async def wait(self) -> int:
            await asyncio.Event().wait()
            return 0

    process = Process()
    tasks: list[asyncio.Task[object]] = []
    create_task = asyncio.create_task
    gather = asyncio.gather
    gather_calls = 0

    def track_task(
        coroutine: Coroutine[Any, Any, object],
    ) -> asyncio.Task[object]:
        task = create_task(coroutine)
        tasks.append(task)
        return task

    async def interrupt_gather(*values: Any, **options: Any) -> Any:
        nonlocal gather_calls
        gather_calls += 1
        if gather_calls == 1:
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel, "cleanup cancel")
        return await gather(*values, **options)

    async def spawn(  # ruff:ignore[unused-async]
        *_args: object,
        **_kwargs: object,
    ) -> Process:
        return process

    async def fail_cleanup(_process: object) -> None:  # ruff:ignore[unused-async]
        msg = "cleanup B"
        raise LookupError(msg)

    def fail_log(_line: str) -> None:
        msg = "primary A"
        raise RuntimeError(msg)

    monkeypatch.setattr(mods, "free_udp_ports", lambda _count: (1, 2))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "create_task", track_task)
    monkeypatch.setattr(steamcmd, "terminate_process", fail_cleanup)
    if interrupt_cleanup:
        monkeypatch.setattr(asyncio, "gather", interrupt_gather)
    update = mods.update(tmp_path / "updater", tmp_path, log_handler=fail_log)

    if cancelled:
        update_task = create_task(update)
        await process.stdout.started.wait()
        update_task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await update_task
    else:
        with pytest.raises(RuntimeError, match="primary A") as caught:
            await update

    cause = caught.value.__cause__
    if interrupt_cleanup:
        assert isinstance(cause, BaseExceptionGroup)
        assert [type(error) for error in cause.exceptions] == [
            LookupError,
            asyncio.CancelledError,
        ]
    else:
        assert isinstance(cause, LookupError)
        assert str(cause) == "cleanup B"
    assert len(tasks) == 2
    assert all(task.done() for task in tasks)


async def test_log_handler_failure_terminates_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, ugc = write_updater(tmp_path)
    processes = (0, 0)
    monkeypatch.setattr(steamcmd, "TERMINATE_GRACE_PERIOD", 0.01)

    def fail(line: str) -> None:
        nonlocal processes
        processes = parse_processes(line)
        msg = "log failed"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="log failed"):
        await mods.update(executable, ugc, log_handler=fail)

    await assert_stopped(processes)


async def test_cancellation_terminates_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable, ugc = write_updater(tmp_path)
    ready = asyncio.Event()
    processes = (0, 0)
    monkeypatch.setattr(steamcmd, "TERMINATE_GRACE_PERIOD", 0.01)

    def handle_log(line: str) -> None:
        nonlocal processes
        processes = parse_processes(line)
        ready.set()

    task = asyncio.create_task(mods.update(executable, ugc, log_handler=handle_log))
    async with asyncio.timeout(2):
        await ready.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await assert_stopped(processes)


@pytest.mark.parametrize("returncode", [0, 7])
async def test_leader_exit_terminates_descendant_holding_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    executable, ugc = write_updater(tmp_path, EXITING_UPDATER)
    lines: list[str] = []
    monkeypatch.setenv("FAKE_RETURN_CODE", str(returncode))

    async def run() -> None:
        async with asyncio.timeout(2):
            await mods.update(executable, ugc, log_handler=lines.append)

    if returncode:
        with pytest.raises(ChildProcessError, match=f"status {returncode}"):
            await run()
    else:
        await run()

    assert lines[1:] == ["DONE"]
    await assert_stopped(parse_processes(lines[0]))
