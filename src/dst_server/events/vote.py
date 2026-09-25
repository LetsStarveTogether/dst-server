from typing import Literal

from dst_server.lua_codec import NonNegativeSafeLuaInteger
from dst_server.models.base import FrozenModel, Identifier

from .base import EventRecord


class VoteData(FrozenModel):
    source: Literal["worldvoter", "gorge_voter", "lobbyvote"]


class VoteSelection(FrozenModel):
    userid: Identifier
    selection: NonNegativeSafeLuaInteger


class VoteUpdatedData(VoteData):
    command: str | None
    command_hash: NonNegativeSafeLuaInteger
    starter_userid: Identifier | None
    target_userid: Identifier | None
    countdown: NonNegativeSafeLuaInteger
    voters: tuple[VoteSelection, ...]


class VoteSubmittedData(VoteData):
    command: str
    userid: Identifier
    target_userid: Identifier | None
    selection: NonNegativeSafeLuaInteger | Identifier


class VoteClosedData(VoteData):
    command: str
    starter_userid: Identifier | None
    target_userid: Identifier | None


class VoteResultData(VoteData):
    command: str
    target_userid: Identifier | None
    passed: bool
    selection: NonNegativeSafeLuaInteger | Identifier | None
    count: NonNegativeSafeLuaInteger | None
    total: NonNegativeSafeLuaInteger
    total_voted: NonNegativeSafeLuaInteger
    total_not_voted: NonNegativeSafeLuaInteger
    options: tuple[NonNegativeSafeLuaInteger, ...]


class VoteUpdatedEvent(EventRecord[VoteUpdatedData]):
    event: Literal["dst.vote.updated"]


class VoteSubmittedEvent(EventRecord[VoteSubmittedData]):
    event: Literal["dst.vote.submitted"]


class VoteClosedEvent(EventRecord[VoteClosedData]):
    event: Literal["dst.vote.closed"]


class VoteResultEvent(EventRecord[VoteResultData]):
    event: Literal["dst.vote.result"]
