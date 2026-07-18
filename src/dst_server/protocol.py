from __future__ import annotations

import asyncio
import fcntl
import json
import os
from pathlib import Path

from pydantic import JsonValue

RESULT_PREFIX = "DST_SERVER_RESULT|"
LAST_PROTOCOL_FD = 5


class LuaBusyError(Exception):
    pass


def open_protocol_pipes() -> tuple[list[int], tuple[int, int, int]]:
    pairs: list[tuple[int, int]] = []
    open_fds: set[int] = set()
    try:  # ruff:ignore[too-many-statements-in-try-clause]
        for _ in range(3):
            pair = os.pipe()
            pairs.append(pair)
            open_fds.update(pair)

        parent_fds = [pairs[0][1], pairs[1][0], pairs[2][0]]
        server_fds = [pairs[0][0], pairs[1][1], pairs[2][1]]
        for index, descriptor in enumerate(server_fds):
            replacement = move_above_protocol_fds(descriptor)
            if replacement != descriptor:
                open_fds.remove(descriptor)
                open_fds.add(replacement)
                server_fds[index] = replacement
    except BaseException:
        for descriptor in open_fds:
            os.close(descriptor)
        raise
    return parent_fds, (server_fds[0], server_fds[1], server_fds[2])


def move_above_protocol_fds(descriptor: int) -> int:
    if descriptor > LAST_PROTOCOL_FD:
        return descriptor
    replacement = fcntl.fcntl(
        descriptor,
        fcntl.F_DUPFD_CLOEXEC,
        LAST_PROTOCOL_FD + 1,
    )
    os.close(descriptor)
    return replacement


async def open_pipe_reader(
    descriptor: int,
) -> tuple[asyncio.StreamReader, asyncio.ReadTransport]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    pipe = os.fdopen(descriptor, "rb", buffering=0)
    try:
        transport, _ = await loop.connect_read_pipe(lambda: protocol, pipe)
    except BaseException:
        pipe.close()
        raise
    return reader, transport


async def open_pipe_writer(descriptor: int) -> asyncio.StreamWriter:
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


def lua_request(body: str) -> str:
    return (
        "local ok,payload=pcall(function() local data=(function() "
        f"{body} end)();if data==nil then data=json.null end;"
        "return json.encode_compliant({ok=true,data=data}) end);"
        "if not ok then "
        "payload=json.encode_compliant({ok=false,error=tostring(payload)}) end;"
        f"print({lua_string(RESULT_PREFIX)}..payload)"
    )


def lua_string(value: str) -> str:
    escaped = []
    for character in value:
        if character == '"':
            escaped.append('\\"')
        elif character == "\\":
            escaped.append("\\\\")
        elif character.isprintable():
            escaped.append(character)
        else:
            escaped.extend(f"\\{byte:03d}" for byte in character.encode())
    return f'"{"".join(escaped)}"'


def json_text(value: JsonValue) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def lua_package_path(directory: Path) -> str:
    value = str(directory)
    if any(character in value for character in ";?\r\n"):
        msg = "Lua directory must not contain ';', '?', CR, or LF"
        raise ValueError(msg)
    return f"{value}/?.lua;"


__all__ = [
    "RESULT_PREFIX",
    "LuaBusyError",
    "json_text",
    "lua_package_path",
    "lua_request",
    "lua_string",
    "open_pipe_reader",
    "open_pipe_writer",
    "open_protocol_pipes",
]
