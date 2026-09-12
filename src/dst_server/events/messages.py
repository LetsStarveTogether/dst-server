from typing import Literal

from dst_server.lua_codec import PositiveSafeLuaInteger
from dst_server.models.base import FrozenModel, Identifier

from .base import EventRecord


class AnnouncementData(FrozenModel):
    kind: str
    message: str


class SkinReceivedData(FrozenModel):
    name: str
    skin: str


class SystemMessageData(FrozenModel):
    message: str


class DiceRolledData(FrozenModel):
    userid: Identifier | None
    name: str | None
    prefab: Identifier | None
    rolls: tuple[PositiveSafeLuaInteger, ...]
    max: PositiveSafeLuaInteger


class AnnouncementEvent(EventRecord[AnnouncementData]):
    event: Literal["dst.server.announcement"]


class SkinReceivedEvent(EventRecord[SkinReceivedData]):
    event: Literal["dst.player.skin_received"]


class SystemMessageEvent(EventRecord[SystemMessageData]):
    event: Literal["dst.server.system_message"]


class DiceRolledEvent(EventRecord[DiceRolledData]):
    event: Literal["dst.player.dice_rolled"]
