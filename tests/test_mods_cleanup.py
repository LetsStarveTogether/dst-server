import asyncio
from pathlib import Path

import pytest

from dst_server import steamcmd
from dst_server.cluster import mods

from .helpers import BlockingProcess, process_stopped

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
print(
    "FinishDownloadingServerMods Complete! Process trying to quit nicely..",
    flush=True,
)
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
        mods.prepare_shared(cluster)

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
        mods.prepare_shared(tmp_path / "cluster")

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
        mods.activate(install, cluster)

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
        mods.prepare_shared(cluster)

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
        mods.prepare_shared(cluster)

    assert not (cluster / "mods").exists()
    assert outside.read_text(encoding="utf-8").endswith("true } }")


def test_strict_setup_accepts_static_calls_and_empty_template(tmp_path: Path) -> None:
    setup = tmp_path / "setup.lua"
    setup.write_text(
        '-- ServerModSetup("100")\n'
        "ServerModSetup(\"\"); ServerModSetup('42'); ServerModSetup([[7]])\n"
        'return ServerModSetup("8"), ServerModCollectionSetup("99")\n',
        encoding="utf-8",
    )

    assert mods.setup_downloads(setup, strict=True) == ((7, 8, 42), (99,))


@pytest.mark.parametrize(
    "source",
    [
        'local id = "42"; ServerModSetup(id)',
        'if false then ServerModSetup("42") end',
        'ServerModSetup("4" .. "2")',
        'OtherSetup("42")',
        'ServerModSetup("42", "43")',
        "return",
    ],
)
def test_strict_setup_rejects_dynamic_or_unsupported_code(
    tmp_path: Path,
    source: str,
) -> None:
    setup = tmp_path / "setup.lua"
    setup.write_text(source, encoding="utf-8")

    assert mods.setup_downloads(setup) == ((), ())
    with pytest.raises(ValueError, match="requires only static"):
        mods.setup_downloads(setup, strict=True)


def test_strict_setup_rejects_non_ascii_ids(tmp_path: Path) -> None:
    setup = tmp_path / "setup.lua"
    setup.write_text('ServerModSetup("\uff14\uff12")\n', encoding="utf-8")

    assert mods.setup_downloads(setup) == ((), ())
    with pytest.raises(ValueError, match="invalid Workshop ID"):
        mods.setup_downloads(setup, strict=True)


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


async def test_update_chains_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = BlockingProcess()

    async def spawn(  # ruff:ignore[unused-async]
        *_args: object,
        **_kwargs: object,
    ) -> BlockingProcess:
        return process

    async def fail_cleanup(_process: object) -> None:  # ruff:ignore[unused-async]
        msg = "cleanup B"
        raise LookupError(msg)

    def fail_log(_line: str) -> None:
        msg = "primary A"
        raise RuntimeError(msg)

    monkeypatch.setattr(mods, "free_udp_ports", lambda _count: (1, 2))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(steamcmd, "terminate_process", fail_cleanup)
    existing_tasks = asyncio.all_tasks()

    with pytest.raises(RuntimeError, match="primary A") as caught:
        await mods.update(tmp_path / "updater", tmp_path, log_handler=fail_log)

    assert isinstance(caught.value.__cause__, LookupError)
    assert str(caught.value.__cause__) == "cleanup B"
    assert asyncio.all_tasks() == existing_tasks


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

    assert lines[1:] == [
        "DONE",
        "FinishDownloadingServerMods Complete! Process trying to quit nicely..",
    ]
    await assert_stopped(parse_processes(lines[0]))
