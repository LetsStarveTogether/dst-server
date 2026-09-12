import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

import orjson
from pydantic import JsonValue

from dst_server.models.driver import DriverHealth
from dst_server.runtime import Server, ServerConfig
from dst_server.runtime.console import StaleGenerationError

COMMAND_DONE = b"DST_RemoteCommandDone"


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
    f"#!{sys.executable}\n" + Path(__file__).with_name("fake_server.py").read_text()
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
