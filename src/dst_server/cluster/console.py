from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from logbook import Logger

from dst_server.runtime import Server
from dst_server.runtime.fds import PROTOCOL_LINE_LIMIT, open_reader

logger = Logger(__name__)


async def _read_command(reader: asyncio.StreamReader) -> tuple[bytes | None, bool]:
    oversized = False
    while True:
        try:
            line = await reader.readuntil(b"\n")
        except asyncio.LimitOverrunError as error:
            await reader.readexactly(min(error.consumed, PROTOCOL_LINE_LIMIT))
            oversized = True
            continue
        except asyncio.IncompleteReadError as error:
            return (error.partial or None, oversized)
        return line, oversized


def ensure(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISFIFO(mode):
            return
        if stat.S_ISDIR(mode):
            msg = f"console path is a directory: {path}"
            raise IsADirectoryError(msg)
        path.unlink()
    os.mkfifo(path)


async def forward(path: Path, server: Server) -> None:
    descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    reader, transport = await open_reader(descriptor)
    try:
        while True:
            raw_line, oversized = await _read_command(reader)
            if oversized:
                logger.warning(
                    "{shard}: discarded oversized console command",
                    shard=server.config.shard,
                )
                if raw_line is None:
                    break
                continue
            if raw_line is None:
                break
            command = raw_line.decode(errors="replace").rstrip("\r\n")
            if not command:
                continue
            try:
                result = await server.execute(command)
            except Exception as error:
                logger.exception(
                    "{shard}: console command failed: {error}",
                    shard=server.config.shard,
                    error=error,
                )
                continue
            for line in result.splitlines():
                logger.info("{shard}: {line}", shard=server.config.shard, line=line)
    finally:
        transport.close()


__all__ = ["ensure", "forward"]
