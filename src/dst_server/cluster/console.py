import os
import stat
from pathlib import Path

from logbook import Logger

from dst_server.runtime import Server
from dst_server.runtime.fds import open_reader, read_line

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
    reader, transport = await open_reader(descriptor)
    try:
        while True:
            raw_line, oversized = await read_line(reader)
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
