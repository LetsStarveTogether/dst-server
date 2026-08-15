from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from dst_server.cluster import console
from dst_server.runtime import Server
from dst_server.runtime.fds import PROTOCOL_LINE_LIMIT


class StubServer:
    def __init__(self) -> None:
        self.config = SimpleNamespace(shard="forest")
        self.commands: list[str] = []
        self.complete = asyncio.Event()

    async def execute(self, command: str) -> str:
        self.commands.append(command)
        if command == "fail":
            msg = "injected command failure"
            raise RuntimeError(msg)
        self.complete.set()
        return "first\nsecond"


async def test_forward_discards_oversized_command_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "console"
    console.ensure(path)
    server = StubServer()
    logger = Mock()
    monkeypatch.setattr(console, "logger", logger)
    reader = asyncio.StreamReader(limit=PROTOCOL_LINE_LIMIT)
    transport = Mock(spec=asyncio.ReadTransport)
    connected = asyncio.Event()

    async def open_reader(  # ruff:ignore[unused-async]
        descriptor: int,
    ) -> tuple[asyncio.StreamReader, asyncio.ReadTransport]:
        os.close(descriptor)
        connected.set()
        return reader, cast("asyncio.ReadTransport", transport)

    monkeypatch.setattr(console, "open_reader", open_reader)
    task = asyncio.create_task(console.forward(path, cast("Server", server)))
    await connected.wait()
    payload = b"x" * (PROTOCOL_LINE_LIMIT + 1) + b"\n\nfail\nok\n"
    reader.feed_data(payload)

    async with asyncio.timeout(1):
        await server.complete.wait()

    assert server.commands == ["fail", "ok"]
    assert not task.done()
    logger.warning.assert_called_once()
    logger.exception.assert_called_once()
    assert logger.info.call_count == 2

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    transport.close.assert_called_once()
