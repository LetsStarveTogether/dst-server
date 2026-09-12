from datetime import time
from enum import StrEnum

from pydantic import SecretStr

from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardSettings,
)
from dst_server.configuration.overrides import (
    ModOverride,
    ModOverrides,
    WorldgenOverride,
)
from dst_server.configuration.presets import (
    ENDLESS,
    FOREST_CAVES,
    FOREST_ONLY_NIGHT,
    LAVAARENA,
    LIGHTS_OUT_GENERATION,
    LIGHTS_OUT_SETTINGS,
    QUAGMIRE,
    SHARDED,
    RoomPreset,
    compose,
    custom_world,
    shard,
)
from dst_server.configuration.world import CaveOverrides, ForestOverrides
from dst_server.deployment import (
    DEFAULT_IMAGE,
    RoomPortAllocation,
)
from dst_server.presets.mod_configurations import MOD_CONFIGURATIONS
from dst_server.rooms import DailyWindow, Room, RoomDeployment

CLUSTER_DESCRIPTION = "朗诵团：924715341 频道：饥荒联机版 Let's Starve Together!"  # ruff: ignore[ambiguous-unicode-character-string]
TOKEN_ENVIRONMENT = "DST_SERVER_CLUSTER_TOKEN"  # ruff: ignore[hardcoded-password-string]
NETDATA_ENVIRONMENT = {
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://10.255.255.254:4317",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://10.255.255.254:4317",
    "OTEL_TRACES_EXPORTER": "none",
}
_SEMI_MODS = (1803285852, 2189004162, 2950657933, 3223103565, 3046339764)


class RoomType(StrEnum):
    PURE_SURVIVAL = "pure_survival"
    PURE_ENDLESS = "pure_endless"
    SEMI_SURVIVAL = "semi_survival"
    SEMI_ENDLESS = "semi_endless"
    AFK = "afk"
    LIGHTS_OUT_SURVIVAL = "lights_out_survival"
    LIGHTS_OUT_ENDLESS = "lights_out_endless"
    ISLAND_ADVENTURE = "island_adventure"
    HAMLET = "hamlet"
    ADVENTURE = "adventure"
    GORGE = "gorge"
    FORGE = "forge"


_HISTORY_ROOM_TYPES = {
    RoomType.PURE_SURVIVAL,
    RoomType.PURE_ENDLESS,
    RoomType.SEMI_SURVIVAL,
    RoomType.SEMI_ENDLESS,
    RoomType.LIGHTS_OUT_SURVIVAL,
    RoomType.LIGHTS_OUT_ENDLESS,
}


_TEMPLATE_DEFAULTS = {
    RoomType.PURE_SURVIVAL: ("纯净生存", 9),
    RoomType.PURE_ENDLESS: ("纯净无尽", 9),
    RoomType.SEMI_SURVIVAL: ("半纯生存", 9),
    RoomType.SEMI_ENDLESS: ("半纯无尽", 9),
    RoomType.AFK: ("挂皮肤", 64),
    RoomType.LIGHTS_OUT_SURVIVAL: ("永夜生存", 9),
    RoomType.LIGHTS_OUT_ENDLESS: ("永夜无尽", 6),
    RoomType.ISLAND_ADVENTURE: ("岛屿冒险", 4),
    RoomType.HAMLET: ("云霄国度", 4),
    RoomType.ADVENTURE: ("冒险", 9),
    RoomType.GORGE: ("暴食", 9),
    RoomType.FORGE: ("熔炉", 6),
}

ROOMS = tuple(
    (numbers, kind, *_TEMPLATE_DEFAULTS[kind])
    for numbers, kind in (
        (range(30), RoomType.PURE_SURVIVAL),
        (range(30, 60), RoomType.PURE_ENDLESS),
        (range(60, 70), RoomType.SEMI_SURVIVAL),
        (range(70, 100), RoomType.SEMI_ENDLESS),
        (range(200, 205), RoomType.AFK),
        (range(205, 206), RoomType.ADVENTURE),
        (range(206, 207), RoomType.GORGE),
        (range(207, 210), RoomType.FORGE),
        (range(210, 213), RoomType.ISLAND_ADVENTURE),
        (range(213, 216), RoomType.HAMLET),
    )
)
ROOM_NUMBERS = tuple(number for numbers, _, _, _ in ROOMS for number in numbers)
_ROOM_SCHEDULES: tuple[tuple[str, int, int] | None, ...] = (
    None,
    ("白饭", 10, 18),
    ("晚宴", 18, 0),
    ("夜饮", 0, 8),
)


def room(number: int) -> tuple[RoomType, str, int]:
    if isinstance(number, bool) or not isinstance(number, int):
        msg = "room number must be an integer"
        raise TypeError(msg)
    for numbers, kind, label, max_players in ROOMS:
        if number in numbers:
            return kind, label, max_players
    msg = "LST room number must be an integer in 000-099 or 200-215"
    raise ValueError(msg)


def room_schedule(number: int) -> tuple[str, int, int] | None:
    room(number)
    for numbers, kind, _, _ in ROOMS[:4]:
        if number in numbers:
            offset = number - numbers.start
            sizes = (6, 1, 2, 1) if kind == RoomType.SEMI_SURVIVAL else (16, 4, 8, 2)
            for size, schedule in zip(sizes, _ROOM_SCHEDULES, strict=True):
                if offset < size:
                    return schedule
                offset -= size
    return None


def room_name(number: int) -> str:
    _, label, _ = room(number)
    name = f"LST-{number:03d}-{label}"
    schedule = room_schedule(number)
    if schedule:
        suffix, start, end = schedule
        return f"{name}-{suffix} | 每日 {start}-{end or 24} 开放"
    return f"{name} | 朗诵团 5 周年啦！入团找到你未来的 5 年好饥友吧~"  # ruff: ignore[ambiguous-unicode-character-string]


def _mods(*workshop_ids: int) -> RoomPreset:
    return RoomPreset(
        mods=ModOverrides(
            entries={
                f"workshop-{workshop_id}": ModOverride(
                    enabled=True,
                    configuration_options=MOD_CONFIGURATIONS[workshop_id],
                )
                for workshop_id in workshop_ids
            }
        )
    )


_LONG_HISTORY = RoomPreset(settings=ClusterSettings(max_snapshots=999_999_999))
_STANDARD = compose(
    _LONG_HISTORY,
    RoomPreset(settings=ClusterSettings(pause_when_empty=True)),
)
_SEMI_WORLD = compose(
    shard(
        "forest",
        WorldgenOverride(
            overrides=ForestOverrides(
                antliontribute="never",
                beefalo="often",
                boons="often",
                cactus="often",
                grass="often",
                grassgekkos="never",
                krampus="always",
                moon_starfish="often",
                ocean_bullkelp="often",
                prefabswaps_start="classic",
                reeds="often",
                resettime="none",
                sapling="often",
                tallbirds="often",
                touchstone="often",
                wildfires="never",
            )
        ),
    ),
    shard(
        "cave",
        WorldgenOverride(
            overrides=CaveOverrides(
                boons="often",
                grass="often",
                grassgekkos="never",
                krampus="always",
                prefabswaps_start="classic",
                reeds="often",
                resettime="none",
                sapling="often",
                touchstone="often",
            )
        ),
    ),
)
_ROOM_PRESETS = {
    RoomType.PURE_SURVIVAL: compose(_STANDARD, FOREST_CAVES),
    RoomType.PURE_ENDLESS: compose(_STANDARD, FOREST_CAVES, ENDLESS),
    RoomType.SEMI_SURVIVAL: compose(
        _STANDARD,
        FOREST_CAVES,
        _SEMI_WORLD,
        _mods(*_SEMI_MODS),
    ),
    RoomType.SEMI_ENDLESS: compose(
        _STANDARD,
        FOREST_CAVES,
        ENDLESS,
        _SEMI_WORLD,
        _mods(*_SEMI_MODS),
    ),
    RoomType.AFK: compose(
        shard(
            "afk",
            WorldgenOverride.forest(
                worldgen_preset="ENDLESS",
                settings_preset="ENDLESS",
                overrides=ForestOverrides(
                    boons="never",
                    fruitfly="never",
                    has_ocean=True,
                    world_size="small",
                ),
            ),
        ),
        RoomPreset(
            settings=ClusterSettings(
                max_snapshots=9,
                pause_when_empty=True,
                tick_rate=1,
                vote_enabled=False,
            )
        ),
        _mods(1981709850),
    ),
    RoomType.LIGHTS_OUT_SURVIVAL: compose(
        _STANDARD,
        FOREST_CAVES,
        LIGHTS_OUT_GENERATION,
        LIGHTS_OUT_SETTINGS,
    ),
    RoomType.LIGHTS_OUT_ENDLESS: compose(
        _STANDARD,
        FOREST_CAVES,
        ENDLESS,
        LIGHTS_OUT_GENERATION,
        FOREST_ONLY_NIGHT,
    ),
    RoomType.ISLAND_ADVENTURE: compose(
        _STANDARD,
        SHARDED,
        shard("shipwrecked", custom_world("SHIPWRECKED_ENDLESS")),
        shard(
            "volcano",
            custom_world("SURVIVAL_VOLCANO_CLASSIC"),
            settings=ShardSettings(
                is_master=False,
                name="volcano",
                id=2,
                master_server_port=27017,
                server_port=11000,
            ),
        ),
        _mods(1467214795, 3435352667),
    ),
    RoomType.HAMLET: compose(
        _STANDARD,
        shard("hamlet", custom_world("PORKLAND_DEFAULT")),
        _mods(3322803908),
    ),
    RoomType.ADVENTURE: compose(
        _STANDARD,
        SHARDED,
        shard("adventure", WorldgenOverride.forest()),
        _mods(756229217, 1847959350),
    ),
    RoomType.GORGE: compose(
        _LONG_HISTORY,
        SHARDED,
        QUAGMIRE,
        _mods(1918927570),
    ),
    RoomType.FORGE: compose(
        _LONG_HISTORY,
        LAVAARENA,
        RoomPreset(
            settings=ClusterSettings(
                autosaver_enabled=False,
                tick_rate=1,
            )
        ),
        _mods(1938752683, 2038128735, 2633870801, 2961923603),
    ),
}


def template_names() -> tuple[str, ...]:
    return tuple(kind.value for kind in RoomType)


def build_template(
    name: str,
    *,
    token: SecretStr,
    number: int = 0,
    cluster_key: SecretStr | None = None,
    settings: ClusterSettings | None = None,
) -> ClusterConfig:
    """Build any gameplay template in any supported room slot."""
    RoomPortAllocation(number=number)
    kind = RoomType(name)
    label, max_players = _TEMPLATE_DEFAULTS[kind]
    defaults = ClusterSettings(
        cluster_name=f"LST-{number:03d}-{label}",
        cluster_description=CLUSTER_DESCRIPTION,
        cluster_language="zh",
        steam_group_id=45_524_458,
        max_players=max_players,
    )
    if settings is not None:
        defaults = defaults.replace(**{
            field: getattr(settings, field) for field in settings.model_fields_set
        })
    return _ROOM_PRESETS[kind].build(
        token=token, cluster_key=cluster_key, settings=defaults
    )


def fleet_room(
    number: int,
    *,
    token: SecretStr,
    cluster_key: SecretStr | None = None,
    image: str = DEFAULT_IMAGE,
    volume_idmap: str | None = None,
    userns: str | None = None,
) -> Room:
    kind, _, _ = room(number)
    schedule = room_schedule(number)
    return Room(
        number=number,
        template=kind.value,
        cluster=build(number, token=token, cluster_key=cluster_key),
        deployment=RoomDeployment(
            image=image,
            environment=NETDATA_ENVIRONMENT
            | (
                {"DST_SERVER_TELEMETRY_PROFILE": "history"}
                if kind in _HISTORY_ROOM_TYPES
                else {}
            ),
            volume_idmap=volume_idmap,
            userns=userns,
            start_on_boot=schedule is None,
        ),
        schedule=(DailyWindow(start=time(schedule[1]), end=time(schedule[2])),)
        if schedule
        else (),
        recycle=kind in {group[1] for group in ROOMS[:4]},
    )


def build(
    number: int,
    *,
    token: SecretStr,
    cluster_key: SecretStr | None = None,
) -> ClusterConfig:
    return build_template(
        room(number)[0],
        token=token,
        number=number,
        cluster_key=cluster_key,
        settings=ClusterSettings(cluster_name=room_name(number)),
    )
