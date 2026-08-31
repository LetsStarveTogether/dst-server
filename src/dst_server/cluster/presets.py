from pydantic import Field, SecretStr

from dst_server.models.base import FrozenModel, RevalidatedFrozenModel

from .config import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardName,
    ShardSettings,
)
from .overrides import (
    FrozenMapping,
    LevelDataOverride,
    ModOverrides,
    WorldgenOverride,
)
from .world import (
    CaveOverrides,
    ForestOverrides,
    LavaArenaOverrides,
    QuagmireOverrides,
    WorldOverrides,
)


def _overlay[T: FrozenModel](base: T, patch: T) -> T:
    return base.replace(**{
        field: getattr(patch, field) for field in patch.model_fields_set
    })


def _complete[T: WorldOverrides](overrides: T) -> T:
    return type(overrides).model_validate(overrides.model_dump())


def _merge_mods(base: ModOverrides, patch: ModOverrides) -> ModOverrides:
    if "entries" in patch.model_fields_set:
        patch = patch.replace(entries={**base.entries, **patch.entries})
    return _overlay(base, patch)


def _merge_world(
    base: WorldgenOverride,
    patch: WorldgenOverride,
) -> WorldgenOverride:
    if "overrides" in patch.model_fields_set:
        if type(base.overrides) is not type(patch.overrides):
            msg = "cannot compose different world override types"
            raise TypeError(msg)
        patch = patch.replace(overrides=_overlay(base.overrides, patch.overrides))
    return _overlay(base, patch)


def _merge_shard(base: ShardConfig, patch: ShardConfig) -> ShardConfig:
    patch = patch.replace(settings=_overlay(base.settings, patch.settings))
    if (
        "world" in patch.model_fields_set
        and base.world is not None
        and patch.world is not None
    ):
        patch = patch.replace(world=_merge_world(base.world, patch.world))
    if "mods" in patch.model_fields_set:
        patch = patch.replace(mods=_merge_mods(base.mods, patch.mods))
    return _overlay(base, patch)


class RoomPreset(RevalidatedFrozenModel):
    settings: ClusterSettings = Field(default_factory=ClusterSettings)
    shards: FrozenMapping[ShardName, ShardConfig] = Field(default_factory=dict)
    mods: ModOverrides = Field(default_factory=ModOverrides)

    def build(
        self,
        *,
        token: SecretStr,
        cluster_key: SecretStr | None = None,
        settings: ClusterSettings | None = None,
    ) -> ClusterConfig:
        preset = type(self).model_validate(self)
        cluster_settings = (
            _overlay(preset.settings, settings)
            if settings is not None
            else preset.settings
        )
        if cluster_key is not None and (
            len(preset.shards) > 1 or cluster_settings.shard_enabled
        ):
            cluster_settings = cluster_settings.replace(cluster_key=cluster_key)
        shards = {
            name: shard.replace(mods=_merge_mods(preset.mods, shard.mods))
            for name, shard in preset.shards.items()
        }
        return ClusterConfig(settings=cluster_settings, shards=shards, token=token)


def compose(*parts: RoomPreset) -> RoomPreset:
    settings = ClusterSettings()
    shards: dict[str, ShardConfig] = {}
    mods = ModOverrides()
    for part in parts:
        settings = _overlay(settings, part.settings)
        if part.mods.model_fields_set:
            mods = _merge_mods(mods, part.mods)
            shards = {
                name: current.replace(mods=_merge_mods(current.mods, part.mods))
                for name, current in shards.items()
            }
        for name, patch in part.shards.items():
            if name in shards:
                shards[name] = _merge_shard(shards[name], patch)
            else:
                shards[name] = patch.replace(mods=_merge_mods(mods, patch.mods))
    return RoomPreset(settings=settings, shards=shards, mods=mods)


def shard(
    name: str,
    world: WorldgenOverride | None = None,
    *,
    level: LevelDataOverride | None = None,
    settings: ShardSettings | None = None,
) -> RoomPreset:
    values: dict[str, object] = {
        "settings": settings if settings is not None else ShardSettings()
    }
    if world is not None:
        values["world"] = world
    if level is not None:
        values["level"] = level
    return RoomPreset(shards={name: ShardConfig.model_validate(values)})


def custom_world(preset: str) -> WorldgenOverride:
    return WorldgenOverride(worldgen_preset=preset, settings_preset=preset)


SHARDED = RoomPreset(
    settings=ClusterSettings(shard_enabled=True, master_ip="127.0.0.1")
)
FOREST = shard("forest", WorldgenOverride.forest())
CAVES = compose(
    SHARDED,
    shard(
        "cave",
        WorldgenOverride.cave(),
        settings=ShardSettings(
            is_master=False,
            name="Caves",
            id=2,
            master_server_port=27017,
            server_port=11000,
        ),
    ),
)
FOREST_CAVES = compose(FOREST, CAVES)

QUAGMIRE = compose(
    RoomPreset(settings=ClusterSettings(game_mode="quagmire")),
    shard(
        "gorge",
        WorldgenOverride(
            worldgen_preset="QUAGMIRE",
            settings_preset="QUAGMIRE",
            overrides=QuagmireOverrides(),
        ),
        level=LevelDataOverride(
            id="QUAGMIRE",
            name="The Gorge",
            desc="",
            location="quagmire",
            overrides=_complete(QuagmireOverrides()),
            background_node_range=(0, 1),
            required_prefabs=("quagmire_portal",),
            version=4,
        ),
    ),
)
LAVAARENA = compose(
    RoomPreset(settings=ClusterSettings(game_mode="lavaarena")),
    shard(
        "xforge",
        WorldgenOverride(
            worldgen_preset="LAVAARENA",
            settings_preset="LAVAARENA",
            overrides=LavaArenaOverrides(),
        ),
        level=LevelDataOverride(
            id="LAVAARENA",
            name="The Forge",
            desc="",
            location="lavaarena",
            overrides=_complete(LavaArenaOverrides()),
            background_node_range=(0, 1),
            required_prefabs=("lavaarena_portal",),
            version=4,
        ),
    ),
)

ENDLESS_GENERATION = shard(
    "forest",
    WorldgenOverride(worldgen_preset="ENDLESS"),
)
ENDLESS_SETTINGS = compose(
    shard("forest", WorldgenOverride(settings_preset="ENDLESS")),
    shard(
        "cave",
        WorldgenOverride(
            overrides=CaveOverrides(
                basicresource_regrowth="always",
                ghostsanitydrain="none",
                portalresurection="always",
                resettime="none",
            )
        ),
    ),
)
ENDLESS = compose(ENDLESS_GENERATION, ENDLESS_SETTINGS)

LIGHTS_OUT_GENERATION = compose(
    shard("forest", WorldgenOverride(worldgen_preset="LIGHTS_OUT")),
    shard(
        "cave",
        WorldgenOverride(overrides=CaveOverrides(day="onlynight")),
    ),
)
LIGHTS_OUT_SETTINGS = shard(
    "forest",
    WorldgenOverride(settings_preset="LIGHTS_OUT"),
)
FOREST_ONLY_NIGHT = shard(
    "forest",
    WorldgenOverride(overrides=ForestOverrides(day="onlynight")),
)
