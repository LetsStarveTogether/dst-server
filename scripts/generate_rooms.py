import os
from argparse import ArgumentParser
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path

from pydantic import SecretStr

from dst_server.cluster.config import ClusterConfig, ClusterSettings, ShardSettings
from dst_server.cluster.overrides import ModOverride, ModOverrides, WorldgenOverride
from dst_server.cluster.presets import (
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
from dst_server.cluster.quadlet import (
    DEFAULT_IMAGE,
    QuadletApplication,
    RoomPortAllocation,
)
from dst_server.cluster.world import CaveOverrides, ForestOverrides
from scripts.mod_configurations import MOD_CONFIGURATIONS

CLUSTER_DESCRIPTION = "朗诵团：924715341 频道：饥荒联机版 Let's Starve Together!"  # ruff: ignore[ambiguous-unicode-character-string]
DEFAULT_CLUSTER_KEY = SecretStr("lst")
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


type RoomDefinition = tuple[range, RoomType, str, int]

ROOMS: tuple[RoomDefinition, ...] = (
    (range(20), RoomType.PURE_SURVIVAL, "纯净生存", 9),
    (range(20, 50), RoomType.PURE_ENDLESS, "纯净无尽", 6),
    (range(50, 70), RoomType.SEMI_SURVIVAL, "半纯生存", 9),
    (range(70, 100), RoomType.SEMI_ENDLESS, "半纯无尽", 6),
    (range(100, 110), RoomType.AFK, "挂皮肤", 64),
    (range(110, 115), RoomType.LIGHTS_OUT_SURVIVAL, "永夜生存", 9),
    (range(115, 120), RoomType.LIGHTS_OUT_ENDLESS, "永夜无尽", 6),
    (range(120, 125), RoomType.ISLAND_ADVENTURE, "岛屿冒险", 4),
    (range(125, 130), RoomType.HAMLET, "云霄国度", 4),
    (range(130, 133), RoomType.ADVENTURE, "冒险", 9),
    (range(133, 135), RoomType.GORGE, "暴食", 9),
    (range(135, 140), RoomType.FORGE, "熔炉", 6),
)
ROOM_NUMBERS = tuple(number for numbers, _, _, _ in ROOMS for number in numbers)


def room(number: int) -> tuple[RoomType, str, int]:
    if isinstance(number, bool) or not isinstance(number, int):
        msg = "room number must be an integer"
        raise TypeError(msg)
    for numbers, kind, label, max_players in ROOMS:
        if number in numbers:
            return kind, label, max_players
    msg = "room number must be an integer from 0 through 139"
    raise ValueError(msg)


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


def build(
    number: int,
    *,
    token: SecretStr,
    cluster_key: SecretStr | None = None,
) -> ClusterConfig:
    kind, label, max_players = room(number)
    cluster = _ROOM_PRESETS[kind].build(
        token=token,
        cluster_key=cluster_key,
        settings=ClusterSettings(
            cluster_name=f"LST-{number:03d}-{label}",
            cluster_description=CLUSTER_DESCRIPTION,
            cluster_language="zh",
            steam_group_id=45_524_458,
            max_players=max_players,
        ),
    )
    # Re-Gorge-itated 1918927570 corrupts non-empty blocklists in Gorge rooms.
    return cluster.replace(blocklist="") if kind is RoomType.GORGE else cluster


def generate_configured_room(
    number: int,
    *,
    cluster: ClusterConfig,
    cluster_dir: Path,
    quadlet_dir: Path,
    image: str = DEFAULT_IMAGE,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    for label, directory in (
        ("cluster_dir", cluster_dir),
        ("quadlet_dir", quadlet_dir),
    ):
        if not directory.is_absolute():
            msg = f"{label} must be an absolute path"
            raise ValueError(msg)

    allocation = RoomPortAllocation(number=number)
    application = QuadletApplication.for_cluster(
        cluster,
        cluster_dir,
        image=image,
        allocation=allocation,
        telemetry_environment=environment,
    )
    return (*cluster.save(cluster_dir), *application.save(quadlet_dir))


def generate_room(
    number: int,
    *,
    token: SecretStr,
    cluster_key: SecretStr | None = None,
    cluster_dir: Path,
    quadlet_dir: Path,
    image: str = DEFAULT_IMAGE,
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    return generate_configured_room(
        number,
        cluster=build(number, token=token, cluster_key=cluster_key),
        cluster_dir=cluster_dir,
        quadlet_dir=quadlet_dir,
        image=image,
        environment=environment,
    )


def generate_configured_rooms(
    configurations: Mapping[int, ClusterConfig],
    *,
    cluster_root: Path,
    quadlet_dir: Path,
    image: str = DEFAULT_IMAGE,
    environments: Mapping[int, Mapping[str, str]] | None = None,
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
) -> tuple[Path, ...]:
    selected = tuple((number, room(number)[0]) for number in dict.fromkeys(numbers))

    written = []
    for number, kind in selected:
        written.extend(
            generate_room(
                number,
                token=token,
                cluster_key=DEFAULT_CLUSTER_KEY,
                cluster_dir=cluster_root / f"{number:03d}",
                quadlet_dir=quadlet_dir,
                image=image,
                environment=NETDATA_ENVIRONMENT
                | (
                    {"DST_SERVER_TELEMETRY_PROFILE": "history"}
                    if kind in _HISTORY_ROOM_TYPES
                    else {}
                ),
            )
        )
    return tuple(written)


def main(argv: Sequence[str] | None = None) -> None:
    parser = ArgumentParser(description="Generate DST room and Quadlet files.")
    parser.add_argument("rooms", nargs="*", type=int, metavar="ROOM")
    parser.add_argument("--cluster-root", required=True, type=Path)
    parser.add_argument("--quadlet-dir", required=True, type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    arguments = parser.parse_args(argv)
    if arguments.token_file is None:
        token = os.environ.get(TOKEN_ENVIRONMENT)
        if token is None:
            parser.error(f"set {TOKEN_ENVIRONMENT} or pass --token-file")
        source = TOKEN_ENVIRONMENT
    else:
        token = arguments.token_file.read_text(encoding="utf-8")
        token = token.removesuffix("\n").removesuffix("\r")
        source = str(arguments.token_file)
    if not token:
        parser.error(f"token is empty: {source}")
    generate_rooms(
        arguments.rooms or ROOM_NUMBERS,
        token=SecretStr(token),
        cluster_root=arguments.cluster_root.resolve(),
        quadlet_dir=arguments.quadlet_dir.resolve(),
        image=arguments.image,
    )


if __name__ == "__main__":
    main()
