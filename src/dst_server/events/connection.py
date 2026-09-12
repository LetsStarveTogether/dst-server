from typing import Literal

from dst_server.lua_codec import NonNegativeSafeLuaInteger, PositiveSafeLuaInteger
from dst_server.models.base import FrozenModel, Identifier
from dst_server.models.driver import DriverHealth

from .base import EventRecord


class ClientData(FrozenModel):
    userid: Identifier


class PresencePlayer(ClientData):
    guid: PositiveSafeLuaInteger


class PresenceData(FrozenModel):
    reason: Literal["startup", "periodic"]
    clients: tuple[Identifier, ...]
    players: tuple[PresencePlayer, ...]
    max_players: NonNegativeSafeLuaInteger
    health: DriverHealth


class ClientAuthenticatedEvent(EventRecord[ClientData]):
    event: Literal["dst.client.authenticated"]


class ClientDisconnectedEvent(EventRecord[ClientData]):
    event: Literal["dst.client.disconnected"]


class PresenceEvent(EventRecord[PresenceData]):
    event: Literal["dst.server.presence"]
