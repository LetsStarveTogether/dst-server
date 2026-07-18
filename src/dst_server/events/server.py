from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dst_server.schema import FrozenModel, Identifier, NonNegativeInt


class ServerReadyEvent(FrozenModel):
    event: Literal["ready"] = "ready"
    detail: Annotated[str, Field(max_length=4096)]


class ServerSessionEvent(FrozenModel):
    event: Literal["session"] = "session"
    session_id: Identifier


class ServerSavedEvent(FrozenModel):
    event: Literal["saved"] = "saved"
    path: Annotated[str, Field(max_length=4096)]
    snapshot: NonNegativeInt | None


class ServerStoppingEvent(FrozenModel):
    event: Literal["stopping"] = "stopping"


class ServerShutdownEvent(FrozenModel):
    event: Literal["shutdown"] = "shutdown"


class UnknownServerEvent(FrozenModel):
    event: Literal["unknown"] = "unknown"
    line: Annotated[str, Field(max_length=1024 * 1024)]


type ServerEvent = (
    ServerReadyEvent
    | ServerSessionEvent
    | ServerSavedEvent
    | ServerStoppingEvent
    | ServerShutdownEvent
    | UnknownServerEvent
)


def parse_server_event(line: str) -> ServerEvent:
    if line == "DST_Master_Ready" or line.startswith("DST_Master_Ready|"):
        return ServerReadyEvent(detail=line.partition("|")[2])
    if line.startswith("DST_SessionId|"):
        session_id = line.removeprefix("DST_SessionId|")
        if session_id:
            return ServerSessionEvent(session_id=session_id)
    if line == "DST_Saved" or line.startswith("DST_Saved|"):
        path = line.partition("|")[2]
        tail = path.rsplit("/", 1)[-1]
        return ServerSavedEvent(
            path=path,
            snapshot=int(tail) if tail.isdigit() else None,
        )
    if line == "DST_Stopping":
        return ServerStoppingEvent()
    if line == "DST_Shutdown":
        return ServerShutdownEvent()
    return UnknownServerEvent(line=line)


__all__ = [
    "ServerEvent",
    "ServerReadyEvent",
    "ServerSavedEvent",
    "ServerSessionEvent",
    "ServerShutdownEvent",
    "ServerStoppingEvent",
    "UnknownServerEvent",
    "parse_server_event",
]
