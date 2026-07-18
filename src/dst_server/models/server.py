from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from dst_server.schema import (
    Description,
    FiniteFloat,
    FrozenModel,
    Identifier,
    Name,
    NonNegativeFloat,
    NonNegativeInt,
    Percent,
    PercentagePoints,
    PositiveInt,
)


class Room(FrozenModel):
    name: Name
    description: Description
    game_mode: Identifier
    playstyle: Identifier | None
    max_players: NonNegativeInt
    player_count: NonNegativeInt
    pvp: bool
    is_paused: bool
    has_password: bool
    is_dedicated: bool
    is_online: bool
    lan_only: bool
    friends_only: bool
    mods_enabled: bool
    clan_id: Annotated[str, Field(max_length=128)]
    clan_only: bool
    shard_id: Identifier
    is_master_shard: bool


class World(FrozenModel):
    age: NonNegativeFloat
    cycles: NonNegativeInt
    day: PositiveInt
    time: Percent
    time_in_phase: Percent
    phase: Literal["day", "dusk", "night"]
    is_day: bool
    is_dusk: bool
    is_night: bool
    moon_phase: Literal["new", "quarter", "half", "threequarter", "full"]
    is_waxing_moon: bool
    is_full_moon: bool
    is_new_moon: bool
    season: Literal["autumn", "winter", "spring", "summer"]
    is_spring: bool
    is_summer: bool
    is_autumn: bool
    is_winter: bool
    elapsed_days_in_season: NonNegativeInt
    season_progress: Percent
    remaining_days_in_season: NonNegativeInt
    spring_length: NonNegativeInt
    summer_length: NonNegativeInt
    autumn_length: NonNegativeInt
    winter_length: NonNegativeInt
    temperature: FiniteFloat
    moisture: NonNegativeFloat
    moisture_ceiling: NonNegativeFloat
    precipitation_probability: Percent
    precipitation_rate: NonNegativeFloat
    precipitation: Literal["none", "rain", "snow", "lunarhail", "acidrain"]
    is_raining: bool
    is_snowing: bool
    is_lunar_hailing: bool
    is_acid_raining: bool
    is_snow_covered: bool
    snow_level: Percent
    lunar_hail_level: PercentagePoints
    lunar_hail_rate: NonNegativeFloat
    wetness: PercentagePoints
    is_wet: bool
    is_cave: bool


class ShardStatus(FrozenModel):
    id: Identifier
    name: Name
    is_current: bool
    ready: bool
    tags: tuple[Identifier, ...]


class Runtime(FrozenModel):
    session_id: Identifier
    snapshot: NonNegativeInt
    build_version: Identifier
    save_version: FiniteFloat
    generated_on_save_version: FiniteFloat
    seed: int | Identifier
    level_id: Identifier
    branch: Identifier
    app_version: Identifier
    shard_id: Identifier
    is_master_shard: bool
    is_cave: bool


class Mod(FrozenModel):
    id: Annotated[str, Field(min_length=1, max_length=256)]
    name: Name
    version: Annotated[str, Field(max_length=128)]


__all__ = ["Mod", "Room", "Runtime", "ShardStatus", "World"]
