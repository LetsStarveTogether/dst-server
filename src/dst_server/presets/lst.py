from collections.abc import Iterable, Mapping
from datetime import time
from enum import StrEnum
from pathlib import Path

from pydantic import SecretStr

from dst_server.configuration.files import configuration_file_exists, discover
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
from dst_server.rooms import DailyWindow, Room, RoomDeployment, RoomStore, read_control

CLUSTER_DESCRIPTION = "朗诵团：924715341 频道：饥荒联机版 Let's Starve Together!"  # ruff: ignore[ambiguous-unicode-character-string]
TOKEN_ENVIRONMENT = "DST_SERVER_CLUSTER_TOKEN"  # ruff: ignore[hardcoded-password-string]
NETDATA_ENVIRONMENT = {
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://10.255.255.254:4317",
    "OTEL_METRICS_EXPORTER": "none",
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
        (range(20), RoomType.PURE_SURVIVAL),
        (range(20, 50), RoomType.PURE_ENDLESS),
        (range(50, 70), RoomType.SEMI_SURVIVAL),
        (range(70, 100), RoomType.SEMI_ENDLESS),
        (range(100, 110), RoomType.AFK),
        (range(110, 115), RoomType.LIGHTS_OUT_SURVIVAL),
        (range(115, 120), RoomType.LIGHTS_OUT_ENDLESS),
        (range(120, 125), RoomType.ISLAND_ADVENTURE),
        (range(125, 130), RoomType.HAMLET),
        (range(130, 133), RoomType.ADVENTURE),
        (range(133, 135), RoomType.GORGE),
        (range(135, 140), RoomType.FORGE),
    )
)
ROOM_NUMBERS = tuple(number for numbers, _, _, _ in ROOMS for number in numbers)
_ROOM_SCHEDULES: tuple[tuple[str, int, int] | None, ...] = (
    None,
    ("晨餐", 9, 12),
    ("午膳", 13, 18),
    ("晚宴", 19, 0),
    ("夜饮", 22, 5),
)


def room(number: int) -> tuple[RoomType, str, int]:
    if isinstance(number, bool) or not isinstance(number, int):
        msg = "room number must be an integer"
        raise TypeError(msg)
    for numbers, kind, label, max_players in ROOMS:
        if number in numbers:
            return kind, label, max_players
    msg = "room number must be an integer from 0 through 139"
    raise ValueError(msg)


def room_schedule(number: int) -> tuple[str, int, int] | None:
    room(number)
    for numbers, _, _, _ in ROOMS[:4]:
        if number in numbers:
            group_size = len(numbers) // len(_ROOM_SCHEDULES)
            return _ROOM_SCHEDULES[(number - numbers.start) // group_size]
    return None


def room_name(number: int) -> str:
    _, label, _ = room(number)
    name = f"LST-{number:03d}-{label}"
    schedule = room_schedule(number)
    if schedule:
        suffix, start, end = schedule
        return f"{name}-{suffix} | 每日 {start}-{end} 开放"
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


def generate_configured_room(
    number: int,
    *,
    cluster: ClusterConfig,
    cluster_dir: Path,
    quadlet_dir: Path,
    image: str = DEFAULT_IMAGE,
    environment: Mapping[str, str] | None = None,
    volume_idmap: str | None = None,
    userns: str | None = None,
    start_on_boot: bool = True,
) -> tuple[Path, ...]:
    for label, directory in (
        ("cluster_dir", cluster_dir),
        ("quadlet_dir", quadlet_dir),
    ):
        if not directory.is_absolute():
            msg = f"{label} must be an absolute path"
            raise ValueError(msg)

    configured = Room(
        number=number,
        cluster=cluster,
        deployment=RoomDeployment(
            image=image,
            environment=environment or {},
            volume_idmap=volume_idmap,
            userns=userns,
            start_on_boot=start_on_boot,
        ),
    )
    if configuration_file_exists(cluster_dir / "cluster.ini") and {
        (shard.name, shard.master) for shard in discover(cluster_dir)
    } != {(name, shard.settings.is_master) for name, shard in cluster.shards.items()}:
        msg = "room topology changes require Host.edit"
        raise ValueError(msg)
    application = configured.application(cluster_dir)
    application.validate_save(quadlet_dir)
    return (*configured.save_game(cluster_dir), *application.save(quadlet_dir))


def generate_room(
    number: int,
    *,
    token: SecretStr,
    cluster_key: SecretStr | None = None,
    cluster_dir: Path,
    quadlet_dir: Path,
    image: str = DEFAULT_IMAGE,
    environment: Mapping[str, str] | None = None,
    volume_idmap: str | None = None,
    userns: str | None = None,
) -> tuple[Path, ...]:
    configured = fleet_room(
        number,
        token=token,
        cluster_key=cluster_key,
        image=image,
        volume_idmap=volume_idmap,
        userns=userns,
    )
    read_control(cluster_dir)
    written = generate_configured_room(
        number,
        cluster=configured.cluster,
        cluster_dir=cluster_dir,
        quadlet_dir=quadlet_dir,
        image=image,
        environment=environment,
        volume_idmap=volume_idmap,
        userns=userns,
        start_on_boot=configured.deployment.start_on_boot,
    )
    configured.save_policy(cluster_dir)
    return written


def generate_configured_rooms(
    configurations: Mapping[int, ClusterConfig],
    *,
    cluster_root: Path,
    quadlet_dir: Path,
    image: str = DEFAULT_IMAGE,
    environments: Mapping[int, Mapping[str, str]] | None = None,
    volume_idmap: str | None = None,
    userns: str | None = None,
) -> tuple[Path, ...]:
    """Generate explicitly configured rooms in any port slot from 000 through 299."""
    written = []
    for number, cluster in sorted(configurations.items()):
        written.extend(
            generate_configured_room(
                number,
                cluster=cluster,
                cluster_dir=cluster_root / f"{number:03d}",
                quadlet_dir=quadlet_dir,
                image=image,
                environment=(environments or {}).get(number),
                volume_idmap=volume_idmap,
                userns=userns,
            )
        )
    return tuple(written)


def generate_rooms(
    numbers: Iterable[int],
    *,
    token: SecretStr,
    cluster_root: Path,
    quadlet_dir: Path,
    image: str = DEFAULT_IMAGE,
    volume_idmap: str | None = None,
    userns: str | None = None,
) -> tuple[Path, ...]:
    selected = tuple(dict.fromkeys(numbers))
    for number in selected:
        room(number)
    store = RoomStore(cluster_root, quadlet_dir)
    written = []
    for number in selected:
        written.extend(
            store.save(
                fleet_room(
                    number,
                    token=token,
                    image=image,
                    volume_idmap=volume_idmap,
                    userns=userns,
                )
            )
        )
    return tuple(written)
