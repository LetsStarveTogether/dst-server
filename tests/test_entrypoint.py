from __future__ import annotations

import asyncio
import time
from pathlib import Path

from dst_server.runner import run

FAKE_SERVER = r"""#!/usr/bin/env python3
import json
import os
import re
import signal
import sys
from pathlib import Path

arguments = sys.argv[1:]
shard = arguments[arguments.index("-shard") + 1]
markers = Path(__MARKERS__)
commands = os.fdopen(3)
results = os.fdopen(4, "w", buffering=1)
events = os.fdopen(5, "w", buffering=1)
events.write("DST_SessionId|TEST-" + shard + "\n")

def stop(signum, frame):
    del signum, frame
    events.write("DST_Shutdown\nDST_Saved|session/TEST/1\nDST_Stopping\n")

signal.signal(signal.SIGTERM, stop)
for command in commands:
    if "driver.install" in command:
        match = re.search(r'\\"nonce\\":\\"([^"\\]+)', command)
        assert match is not None
        health = {
            "protocol": 1,
            "installed": True,
            "profile": "history",
            "events_emitted": 0,
            "errors": 0,
            "players": 0,
            "action_hook": True,
            "shard_hook": True,
        }
        results.write("DST_SERVER_RESULT|" + json.dumps({"ok": True, "data": health}))
        results.write("\nDST_RemoteCommandDone\n")
        (markers / ("ready-" + shard)).touch()
        continue
    (markers / ("command-" + shard)).write_text(command)
    results.write("console-result\nDST_RemoteCommandDone\n")
"""


def write_shard(path: Path, *, master: bool) -> None:
    path.mkdir()
    (path / "server.ini").write_text(
        f"[SHARD]\nis_master = {str(master).lower()}\n",
        encoding="utf-8",
    )
    (path / "modoverrides.lua").write_text("return {}", encoding="utf-8")


def wait_for(path: Path) -> None:
    for attempt in range(1_000):
        del attempt
        if path.exists():
            return
        time.sleep(0.01)
    raise TimeoutError(path)


async def test_python_entrypoint_runs_shards_and_console(tmp_path: Path) -> None:
    install = tmp_path / "install"
    cluster = tmp_path / "cluster"
    markers = tmp_path / "markers"
    executable = install / "bin64" / "dontstarve_dedicated_server_nullrenderer_x64"
    executable.parent.mkdir(parents=True)
    markers.mkdir()
    executable.write_text(
        FAKE_SERVER.replace("__MARKERS__", repr(str(markers))),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    cluster.mkdir()
    (cluster / "cluster.ini").touch()
    (cluster / "cluster_token.txt").touch()
    write_shard(cluster / "forest", master=True)
    write_shard(cluster / "cave", master=False)
    shutdown = asyncio.Event()
    running = asyncio.create_task(
        run(
            install_path=install,
            cluster_path=cluster,
            update_mods=False,
            shutdown=shutdown,
        )
    )

    async with asyncio.timeout(10):
        await asyncio.gather(
            asyncio.to_thread(wait_for, markers / "ready-forest"),
            asyncio.to_thread(wait_for, markers / "ready-cave"),
        )
        await asyncio.to_thread(
            (cluster / "console").write_text,
            'print("hello")\n',
            encoding="utf-8",
        )
        await asyncio.to_thread(wait_for, markers / "command-forest")
        shutdown.set()
        assert await running == 0

    assert (cluster / "console").is_fifo()
    assert (cluster / "cave" / "console").is_fifo()
    assert 'print("hello")' in (markers / "command-forest").read_text(encoding="utf-8")
