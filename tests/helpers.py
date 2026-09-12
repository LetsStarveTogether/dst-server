import asyncio
import os
import select
import subprocess  # ruff:ignore[suspicious-subprocess-import]
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

import orjson
from luaparser import ast
from luaparser.astnodes import Function, LocalFunction, Name
from pydantic import JsonValue

from dst_server.lua_codec import lua_string
from dst_server.models.driver import DriverHealth
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.console import StaleGenerationError

COMMAND_DONE = b"DST_RemoteCommandDone"


async def wait_for_event(
    event: asyncio.Event,
    *tasks: asyncio.Task[Any],
    timeout: float = 5,  # ruff: ignore[async-function-with-timeout]
) -> None:
    """Reach a test milestone, surfacing a worker's early failure immediately."""
    waiter = asyncio.create_task(event.wait())
    try:
        async with asyncio.timeout(timeout):
            done, _ = await asyncio.wait(
                (waiter, *tasks), return_when=asyncio.FIRST_COMPLETED
            )
            for task in tasks:
                if task in done:
                    task.result()
            if event.is_set():
                return
            message = "worker finished before the expected event"
            raise AssertionError(message)
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def run_lua_process(
    runtime: str, *arguments: str | Path, input: bytes | None = None
) -> bytes:
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [runtime, *map(str, arguments)],
        input=input,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, (result.stderr or result.stdout).decode(
        errors="replace"
    )
    return result.stdout


def native_scripts() -> Path:
    return Path(
        os.environ.get(
            "DST_SERVER_TEST_SCRIPTS", Path(__file__).parents[1] / "dst-scripts/scripts"
        )
    )


def native_functions(filename: str, names: set[str]) -> str:
    tree = ast.parse((native_scripts() / filename).read_text())
    functions = [
        (node.name.id, node)
        for node in tree.body.body
        if isinstance(node, (Function, LocalFunction))
        and isinstance(node.name, Name)
        and node.name.id in names
    ]
    assert {name for name, _ in functions} == names
    assert len(functions) == len(names)
    return "\n".join(ast.to_lua_source(node) for _, node in functions)


def run_lua(source: str, luajit: str, *, driver_path: bool = True) -> bytes:
    root = Path(__file__).parents[1]
    scripts = native_scripts()
    assert (scripts / "json.lua").is_file(), "Real DST json.lua is required"
    package_path = f"{scripts}/?.lua;"
    if driver_path:
        package_path = f"{root}/src/dst_server/lua/?.lua;" + package_path
    setup = (
        f'package.path={lua_string(package_path)}..package.path;json=require("json");'
    )
    return run_lua_process(luajit, "-", input=(setup + source).encode())


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
        self.read_fd, self.write_fd = os.pipe()
        self.transport = self

    def get_extra_info(self, name: str) -> int:
        assert name == "pipe"
        return self.write_fd

    def get_write_buffer_size(self) -> int:
        return 0

    def write(self, data: bytes) -> None:
        assert os.write(self.write_fd, data) == len(data)
        self.commands.append(data)
        self.writes.put_nowait(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        if self.write_fd >= 0:
            os.close(self.write_fd)
            os.close(self.read_fd)
            self.write_fd = -1
            self.read_fd = -1

    async def wait_closed(self) -> None:
        pass


async def next_request(writer: StubWriter) -> dict[str, Any]:
    encoded = await asyncio.wait_for(writer.writes.get(), 1)
    assert os.read(writer.read_fd, len(encoded)) == encoded
    assert encoded.startswith(b"DST_RPC|")
    return orjson.loads(encoded.removeprefix(b"DST_RPC|"))


def feed_response(
    reader: asyncio.StreamReader,
    request: dict[str, Any],
    data: object,
    *,
    accepted: bool = True,
    done: bool = True,
) -> None:
    header = {key: request[key] for key in ("v", "nonce", "id", "generation")}
    if accepted:
        reader.feed_data(
            b"DST_RPC|" + orjson.dumps(header | {"accepted": True}) + b"\n"
        )
    reader.feed_data(
        b"DST_RPC|"
        + orjson.dumps(header | {"result": {"ok": True, "data": data}})
        + b"\n"
    )
    if done:
        reader.feed_data(COMMAND_DONE + b"\n")


FAKE_SERVER = (
    f"#!{sys.executable}\n"
    r"""import orjson
import os
import re
import signal
import stat
import sys
from pathlib import Path

assert all(stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (3, 4, 5))
arguments = sys.argv[1:]
shard = arguments[arguments.index("-shard") + 1]


def argument(name):
    return arguments[arguments.index(name) + 1]


directory = Path(
    argument("-persistent_storage_root")
    + "/"
    + argument("-conf_dir")
    + "/"
    + argument("-cluster")
    + "/"
    + shard
)
options = orjson.loads((directory / "dst_server_driver.json").read_bytes())
nonce, generation = options["nonce"], 0
commands = os.fdopen(3)
results = os.fdopen(4, "w", buffering=1)
events = os.fdopen(5, "w", buffering=1)
events.write("DST_SessionId|TEST\n")


def stop(signum, frame):
    del signum, frame
    events.write("DST_Shutdown\nDST_Saved|session/TEST/1\nDST_Stopping\n")


signal.signal(signal.SIGTERM, stop)


def driver_record(record):
    print("DST_DRIVER|" + orjson.dumps({"nonce": nonce, **record}).decode(), flush=True)


if shard != "driver-eof":
    driver_record({"generation": generation})
    if shard == "core-failure":
        driver_record({"error": "installation_failed", "generation": generation})
    else:
        failed = shard == "telemetry-failure"
        health = {
            "protocol": 2,
            "generation": generation,
            "telemetry_status": "failed" if failed else "active",
            "last_error": {
                "stage": "install",
                "message": "installation_failed",
                "count": 1,
            }
            if failed
            else None,
            "events_emitted": 0,
            "errors": 1 if failed else 0,
        }
        driver_record({"health": health})
        if not failed:
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
            print("[00:00:01]: DST_OTEL|" + orjson.dumps(event).decode(), flush=True)

busy = True
for command in commands:
    assert "driver.install" not in command, "unexpected console injection"
    if busy:
        busy = False
        results.write("DST_LuaBusy\n")
        continue
    request = orjson.loads(command.removeprefix("DST_RPC|"))
    header = {key: request[key] for key in ("v", "nonce", "id", "generation")}
    results.write(
        "DST_RPC|" + orjson.dumps(header | {"accepted": True}).decode() + "\n"
    )
    method = request["method"]
    if method == "get_players":
        data = []
    elif method == "save":
        data = {"snapshot": "session/TEST/0000000001"}
    else:
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
        print("DST_OTEL|" + orjson.dumps(event).decode(), flush=True)
        data = {
            "output": "result:" + request["arguments"]["source"],
            "values": [],
            "error": None,
            "truncated": False,
        }
    results.write(
        "DST_RPC|"
        + orjson.dumps(header | {"result": {"ok": True, "data": data}}).decode()
        + "\n"
    )
    results.write("DST_RemoteCommandDone\n")
"""
)


def structured_result(data: object) -> bytes:
    return orjson.dumps({"ok": True, "data": data})


class StubServer(Server):
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.commands: list[tuple[str, dict[str, JsonValue]]] = []
        super().__init__(ServerConfig(shard="test"))

    async def initialize(self) -> Self:
        self.driver.ready(
            DriverHealth(
                protocol=2,
                generation=0,
                telemetry_status="disabled",
                last_error=None,
                events_emitted=0,
                errors=0,
            )
        )
        return self

    async def _execute(
        self,
        method: str,
        arguments: dict[str, JsonValue],
        generation: int,
        generation_is_current: Callable[[], bool] | None = None,
    ) -> bytes:
        del generation
        if generation_is_current is not None and not generation_is_current():
            msg = "DST generation changed before the command was written"
            raise StaleGenerationError(msg)
        self.commands.append((method, arguments))
        return self.responses.pop(0)
