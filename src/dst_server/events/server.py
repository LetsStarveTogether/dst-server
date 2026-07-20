from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dst_server.models.base import FrozenModel, Identifier, NonNegativeInt


class ReadyEvent(FrozenModel):
    event: Literal["ready"] = "ready"
    detail: Annotated[str, Field(max_length=4096)]


class SessionEvent(FrozenModel):
    event: Literal["session"] = "session"
    session_id: Identifier


class SavedEvent(FrozenModel):
    event: Literal["saved"] = "saved"
    path: Annotated[str, Field(max_length=4096)]
    snapshot: NonNegativeInt | None


class StoppingEvent(FrozenModel):
    event: Literal["stopping"] = "stopping"


class ShutdownEvent(FrozenModel):
    event: Literal["shutdown"] = "shutdown"


class UnknownEvent(FrozenModel):
    event: Literal["unknown"] = "unknown"
    line: Annotated[str, Field(max_length=1024 * 1024)]


type Event = (
    ReadyEvent
    | SessionEvent
    | SavedEvent
    | StoppingEvent
    | ShutdownEvent
    | UnknownEvent
)


def parse_event(line: str) -> Event:
    if line == "DST_Master_Ready" or line.startswith("DST_Master_Ready|"):
        return ReadyEvent(detail=line.partition("|")[2])
    if line.startswith("DST_SessionId|"):
        session_id = line.removeprefix("DST_SessionId|")
        if session_id:
            return SessionEvent(session_id=session_id)
    if line == "DST_Saved" or line.startswith("DST_Saved|"):
        path = line.partition("|")[2]
        tail = path.rsplit("/", 1)[-1]
        return SavedEvent(
            path=path,
            snapshot=int(tail) if tail.isdigit() else None,
        )
    if line == "DST_Stopping":
        return StoppingEvent()
    if line == "DST_Shutdown":
        return ShutdownEvent()
    return UnknownEvent(line=line)


__all__ = [
    "Event",
    "ReadyEvent",
    "SavedEvent",
    "SessionEvent",
    "ShutdownEvent",
    "StoppingEvent",
    "UnknownEvent",
    "parse_event",
]
