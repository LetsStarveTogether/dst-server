from typing import Literal

from dst_server.lua_codec import NonNegativeSafeLuaInteger, PositiveSafeLuaInteger
from dst_server.models.base import FrozenModel, Identifier

from .base import EventRecord


class VoteData(FrozenModel):
    # Scoped to the originating driver nonce and generation, not a cluster-wide counter.
    vote_id: Identifier


class VoteStartedData(VoteData):
    command: str | None
    command_hash: NonNegativeSafeLuaInteger
    starter_userid: Identifier | None
    target_userid: Identifier | None
    timeout: PositiveSafeLuaInteger
    options: tuple[str, ...]


class VoteCastData(VoteData):
    userid: Identifier
    selection: PositiveSafeLuaInteger


class VoteResultData(VoteData):
    command: str
    target_userid: Identifier | None
    passed: bool
    selection: PositiveSafeLuaInteger | None
    count: NonNegativeSafeLuaInteger | None
    total: NonNegativeSafeLuaInteger
    total_voted: NonNegativeSafeLuaInteger
    total_not_voted: NonNegativeSafeLuaInteger
    options: tuple[NonNegativeSafeLuaInteger, ...]


class VoteStartedEvent(EventRecord[VoteStartedData]):
    event: Literal["dst.vote.started"]


class VoteCastEvent(EventRecord[VoteCastData]):
    event: Literal["dst.vote.cast"]


class VoteClosedEvent(EventRecord[VoteData]):
    event: Literal["dst.vote.closed"]


class VoteResultEvent(EventRecord[VoteResultData]):
    event: Literal["dst.vote.result"]
