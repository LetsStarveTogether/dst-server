from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from dst_server.cluster.service import run
from dst_server.telemetry import TelemetrySettings, otel

FAKE_SERVER = r"""#!/usr/bin/env python3
import json
import os
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
        failed = shard == "cave"
        health = {
            "protocol": 1,
            "telemetry_status": "failed" if failed else "active",
            "telemetry_error": "world hook unavailable" if failed else None,
            "events_emitted": 0,
            "errors": 0,
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


async def test_service_runs_shards_and_falls_back_from_otlp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    configure = Mock(side_effect=RuntimeError("collector unavailable"))
    monkeypatch.setattr(otel, "configure", configure)
    shutdown = asyncio.Event()
    running = asyncio.create_task(
        run(
            install_path=install,
            cluster_path=cluster,
            update_mods=False,
            telemetry=TelemetrySettings(profile="history"),
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

    configure.assert_called_once()
    assert (cluster / "console").is_fifo()
    assert (cluster / "cave" / "console").is_fifo()
    assert 'print("hello")' in (markers / "command-forest").read_text(encoding="utf-8")
