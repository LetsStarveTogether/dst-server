from typing import Annotated, Literal

from pydantic import Field

from dst_server.lua_codec import NonNegativeSafeLuaInteger
from dst_server.models.base import FrozenModel, Identifier


class ReadyEvent(FrozenModel):
    event: Literal["ready"] = "ready"
    detail: Annotated[str, Field(max_length=4096)]


class SessionEvent(FrozenModel):
    event: Literal["session"] = "session"
    session_id: Identifier


class SavedEvent(FrozenModel):
    event: Literal["saved"] = "saved"
    path: Annotated[str, Field(max_length=4096)]
    snapshot: NonNegativeSafeLuaInteger | None

    @classmethod
    def from_path(cls, path: str) -> SavedEvent:
        tail = path.rsplit("/", 1)[-1]
        return cls(path=path, snapshot=int(tail) if tail.isdigit() else None)


class StoppingEvent(FrozenModel):
    event: Literal["stopping"] = "stopping"


class ShutdownEvent(FrozenModel):
    event: Literal["shutdown"] = "shutdown"


class UnknownEvent(FrozenModel):
    event: Literal["unknown"] = "unknown"
    line: str


type Event = (
    ReadyEvent
    | SessionEvent
    | SavedEvent
    | StoppingEvent
    | ShutdownEvent
    | UnknownEvent
)


def parse_event(line: str) -> Event:
    try:  # ruff:ignore[too-many-statements-in-try-clause]
        if line == "DST_Master_Ready" or line.startswith("DST_Master_Ready|"):
            return ReadyEvent(detail=line.partition("|")[2])
        if line.startswith("DST_SessionId|"):
            session_id = line.removeprefix("DST_SessionId|")
            if session_id:
                return SessionEvent(session_id=session_id)
        if line == "DST_Saved" or line.startswith("DST_Saved|"):
            return SavedEvent.from_path(line.partition("|")[2])
        if line == "DST_Stopping":
            return StoppingEvent()
        if line == "DST_Shutdown":
            return ShutdownEvent()
    except ValueError:
        pass
    return UnknownEvent(line=line)
