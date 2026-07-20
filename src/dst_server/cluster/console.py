from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from logbook import Logger

from dst_server.runtime import Server

logger = Logger(__name__)


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
    pipe = os.fdopen(descriptor, "rb", buffering=0)
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: protocol,
        pipe,
    )
    try:
        while raw_line := await reader.readline():
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
