import asyncio
import fcntl
import os
import sys

PROTOCOL_FDS = (3, 4, 5)
LAST_PROTOCOL_FD = PROTOCOL_FDS[-1]
PROTOCOL_LINE_LIMIT = 64 * 1024


def open_pipes() -> tuple[list[int], tuple[int, int, int]]:
    pairs: list[tuple[int, int]] = []
    open_fds: set[int] = set()
    try:  # ruff:ignore[too-many-statements-in-try-clause]
        for _ in PROTOCOL_FDS:
            pair = os.pipe()
            pairs.append(pair)
            open_fds.update(pair)

        parent_fds = [pairs[0][1], pairs[1][0], pairs[2][0]]
        server_fds = [pairs[0][0], pairs[1][1], pairs[2][1]]
        for index, descriptor in enumerate(server_fds):
            replacement = move_above_protocol(descriptor)
            if replacement != descriptor:
                open_fds.remove(descriptor)
                open_fds.add(replacement)
                server_fds[index] = replacement
    except BaseException:
        for descriptor in open_fds:
            os.close(descriptor)
        raise
    return parent_fds, (server_fds[0], server_fds[1], server_fds[2])


def move_above_protocol(descriptor: int) -> int:
    if descriptor > LAST_PROTOCOL_FD:
        return descriptor
    replacement = fcntl.fcntl(
        descriptor,
        fcntl.F_DUPFD_CLOEXEC,
        LAST_PROTOCOL_FD + 1,
    )
    os.close(descriptor)
    return replacement


async def open_reader(
    descriptor: int,
) -> tuple[asyncio.StreamReader, asyncio.ReadTransport]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=PROTOCOL_LINE_LIMIT)
    protocol = asyncio.StreamReaderProtocol(reader)
    pipe = os.fdopen(descriptor, "rb", buffering=0)
    try:
        transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
    except BaseException:
        pipe.close()
        raise
    return reader, transport


async def open_writer(descriptor: int) -> asyncio.StreamWriter:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    pipe = os.fdopen(descriptor, "wb", buffering=0)
    try:
        transport, _ = await loop.connect_write_pipe(lambda: protocol, pipe)
    except BaseException:
        pipe.close()
        raise
    return asyncio.StreamWriter(transport, protocol, reader, loop)


def main() -> None:
    sources = tuple(int(value) for value in sys.argv[1:4])
    command = sys.argv[4:]
    if len(sources) != len(PROTOCOL_FDS) or not command:
        msg = "usage: fds.py FD3 FD4 FD5 COMMAND [ARG ...]"
        raise SystemExit(msg)
    for source, target in zip(sources, PROTOCOL_FDS, strict=True):
        os.dup2(source, target, inheritable=True)
    for source in sources:
        if source not in PROTOCOL_FDS:
            os.close(source)
    os.execv(command[0], command)  # ruff:ignore[start-process-with-no-shell]


if __name__ == "__main__":
    main()
