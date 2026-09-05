import asyncio
import json
import os
import re
import select
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from collections.abc import Callable
from pathlib import Path
from typing import Self

from dst_server.game import DriverHealth, rpc
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.console import StaleGenerationError

FRAME = re.compile(rb"DST_SERVER_FRAME\|([0-9A-HJKMNP-TV-Z]{26})\|START")
COMMAND_DONE = b"DST_RemoteCommandDone"


def run_lua(source: str, luajit: str, *, driver_path: bool = True) -> bytes:
    root = Path(__file__).parents[1]
    scripts = root / "dst-scripts/scripts"
    assert (scripts / "json.lua").is_file(), "Real DST json.lua is required"
    package_path = f"{scripts}/?.lua;"
    if driver_path:
        package_path = f"{root}/src/dst_server/lua/?.lua;" + package_path
    setup = (
        f"package.path={rpc.lua_string(package_path)}..package.path;"
        'json=require("json");'
    )
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [luajit, "-"],
        input=(setup + source).encode(),
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def process_stopped(process_id: int) -> bool:
    try:
        descriptor = os.pidfd_open(process_id)
    except ProcessLookupError:
        return True
    try:
        readable, _, _ = select.select((descriptor,), (), (), 2)
        return bool(readable)
    finally:
        os.close(descriptor)


class BlockingProcess:
    pid = 1
    returncode = None

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b"READY\n")

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0


class StubWriter:
    def __init__(self) -> None:
        self.commands: list[bytes] = []
        self.writes: asyncio.Queue[bytes] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.commands.append(data)
        self.writes.put_nowait(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


async def next_frame(writer: StubWriter) -> tuple[bytes, bytes, bytes]:
    wrapped = await asyncio.wait_for(writer.writes.get(), 1)
    match = FRAME.search(wrapped)
    assert match is not None
    token = match.group(1)
    prefix = b"DST_SERVER_FRAME|" + token
    return prefix + b"|START", prefix + b"|END", wrapped


def feed_frame(
    reader: asyncio.StreamReader,
    start: bytes,
    end: bytes,
    *lines: bytes,
) -> None:
    reader.feed_data(b"\n".join((start, *lines, end, COMMAND_DONE, b"")))


FAKE_SERVER = r"""#!/usr/bin/env python3
import json
import os
import re
import signal
import stat
import sys

assert all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (3, 4, 5))
arguments = sys.argv[1:]
shard = arguments[arguments.index("-shard") + 1]
commands = os.fdopen(3)
results = os.fdopen(4, "w", buffering=1)
events = os.fdopen(5, "w", buffering=1)
events.write("DST_SessionId|TEST\n")

def stop(signum, frame):
    del signum, frame
    events.write("DST_Shutdown\nDST_Saved|session/TEST/1\nDST_Stopping\n")

signal.signal(signal.SIGTERM, stop)

def start_frame(command):
    match = re.search(r"DST_SERVER_FRAME\|([0-9A-HJKMNP-TV-Z]{26})\|START", command)
    assert match is not None
    token = match.group(1)
    results.write(f"DST_SERVER_FRAME|{token}|START\n")
    return token

def finish_frame(token):
    results.write(
        f"DST_SERVER_FRAME|{token}|END\nDST_RemoteCommandDone\n"
    )

busy = True
for command in commands:
    if busy:
        busy = False
        results.write("DST_LuaBusy\n")
        continue
    token = start_frame(command)
    if "driver.install" in command:
        if shard == "driver-eof":
            results.close()
            continue
        options = command.replace("\\", "")
        match = re.search(r'\["nonce"\]="([^"]+)"', options)
        assert match is not None
        nonce = match.group(1)
        match = re.search(r'\["generation"\]=(\d+)', options)
        assert match is not None
        generation = int(match.group(1))
        if shard == "core-failure":
            results.write(
                "DST_SERVER_RESULT|"
                + json.dumps({"ok": False, "error": "lua_error"})
                + "\n"
            )
            finish_frame(token)
            continue
        failed = shard == "telemetry-failure"
        health = {
            "protocol": 2,
            "generation": generation,
            "telemetry_status": "failed" if failed else "active",
            "last_error": {
                "stage": "install", "message": "installation_failed", "count": 1,
            } if failed else None,
            "events_emitted": 0,
            "errors": 1 if failed else 0,
        }
        results.write(
            "DST_SERVER_RESULT|"
            + json.dumps({"ok": True, "data": health})
            + "\n"
        )
        finish_frame(token)
        if health["telemetry_status"] == "active":
            event = {
                "v": 2,
                "nonce": nonce,
                "generation": generation,
                "session_id": "TEST",
                "seq": 1,
                "event": "dst.world.state_changed",
                "tick": 10,
                "monotonic_ms": 20,
                "cycle": 2,
                "data": {"name": "cycles", "value": 2},
            }
            print("[00:00:01]: DST_OTEL|" + json.dumps(event), flush=True)
        continue
    if "get_players" in command:
        results.write(
            "DST_SERVER_RESULT|"
            + json.dumps({"ok": True, "data": []})
            + "\n"
        )
        finish_frame(token)
        continue
    if "save" in command:
        results.write(
            "DST_SERVER_RESULT|"
            + json.dumps({"ok": True, "data": True})
            + "\n"
        )
        finish_frame(token)
        continue
    print("command received", flush=True)
    event = {
        "v": 2,
        "nonce": nonce,
        "generation": generation,
        "session_id": "TEST",
        "seq": 2,
        "event": "dst.entity.death",
        "tick": 11,
        "monotonic_ms": 21,
        "cycle": 2,
        "data": {
            "victim": {
                "prefab": "rabbit",
                "guid": 42,
                "userid": None,
                "position": None,
            },
            "cause": None,
            "afflicter": None,
            "attributed_player": None,
            "corpsing": False,
            "caused_by_action_sequence": None,
        },
    }
    results.write("DST_OTEL|" + json.dumps(event) + "\n")
    results.write('result:print("hello")\n')
    finish_frame(token)
"""


def structured_result(data: object) -> str:
    return "unrelated print\nDST_SERVER_RESULT|" + json.dumps({
        "ok": True,
        "data": data,
    })


class StubServer(Server):
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.commands: list[str] = []
        self.initial_install = True
        super().__init__(ServerConfig(shard="test"))

    async def initialize(self) -> Self:
        await self.driver.install(0)
        return self

    async def install_driver(self, generation: int) -> DriverHealth:
        if not self.initial_install:
            return await super().install_driver(generation)
        self.initial_install = False
        return DriverHealth(
            protocol=2,
            generation=generation,
            telemetry_status="disabled",
            last_error=None,
            events_emitted=0,
            errors=0,
        )

    async def _execute(
        self,
        command: str,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> str:
        if generation_is_current is not None and not generation_is_current():
            msg = "DST generation changed before the command was written"
            raise StaleGenerationError(msg)
        self.commands.append(command)
        return self.responses.pop(0)
