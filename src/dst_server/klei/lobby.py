from __future__ import annotations

from collections.abc import Mapping
from ipaddress import IPv4Address
from typing import Annotated

from pydantic import Field, JsonValue, ValidationInfo, model_validator

from .enums import Platform, Region, Role
from .schema import KleiModel


class LobbyRegion(KleiModel):
    region: Annotated[str, Field(alias="Region")]


class Capabilities(KleiModel):
    lobby_regions: Annotated[
        tuple[LobbyRegion, ...],
        Field(default_factory=tuple, alias="LobbyRegions"),
    ]


class DataResponse[DataT](KleiModel):
    rows: Annotated[tuple[DataT, ...], Field(default_factory=tuple, alias="GET")]


class Player(KleiModel):
    name: str
    kuid: str
    role: Role | None = None
    steam_id: int | None = None
    ip: IPv4Address | None = None


class Secondary(KleiModel):
    id: str
    port: int | None = None
    addr: Annotated[IPv4Address | None, Field(alias="__addr")] = None
    steamid: str | None = None


class Lobby(KleiModel):
    row_id: Annotated[str, Field(alias="__rowId")]
    name: str
    addr: Annotated[IPv4Address, Field(alias="__addr")]
    port: int
    host: str
    connected: int
    maxconnections: int
    v: int
    allownewplayers: bool
    clanonly: bool
    clienthosted: bool
    dedicated: bool
    fo: bool
    lanonly: bool
    mods: bool
    password: bool
    pvp: bool
    serverpaused: bool
    platform: Platform
    session: str
    guid: str
    intent: str
    steamroom: str
    region: Region
    tags: str | None = None
    mode: str | None = None
    season: str | None = None
    steamclanid: str | None = None
    ownernetid: str | None = None
    steamid: str | None = None
    secondaries: dict[str, Secondary] | None = None

    @model_validator(mode="before")
    @classmethod
    def inject_region(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, Mapping) or "region" in value:
            return value
        if not isinstance(info.context, Mapping):
            return value
        region = info.context.get("region")
        if not isinstance(region, Region):
            return value
        return {**value, "region": region}

    @property
    def connect_code(self) -> str:
        return f"c_connect('{self.addr}', {self.port})"


class Room(Lobby):
    tick: int
    clientmodsoff: bool
    nat: int
    data: str | None = None
    worldgen: str | None = None
    mods_info: list[JsonValue] | None = None
    players: str | None = None
    desc: str | None = None


__all__ = [
    "Capabilities",
    "DataResponse",
    "Lobby",
    "LobbyRegion",
    "Player",
    "Room",
    "Secondary",
]
