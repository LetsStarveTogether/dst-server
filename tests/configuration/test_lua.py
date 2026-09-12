import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr, ValidationError

from dst_server import mods
from dst_server.configuration import files as configuration
from dst_server.configuration.models import (
    ClusterConfig,
    ClusterSettings,
    ShardConfig,
    ShardSettings,
)
from dst_server.configuration.overrides import (
    CustomPreset,
    CustomWorldOverrides,
    LevelDataOverride,
    ModOverride,
    ModOverrides,
    ModSettings,
    WorkshopDownloads,
    WorldgenOverride,
)
from dst_server.configuration.presets import (
    ENDLESS,
    FOREST_CAVES,
    FOREST_ONLY_NIGHT,
    LAVAARENA,
    LIGHTS_OUT_GENERATION,
    QUAGMIRE,
    RoomPreset,
    compose,
    shard,
)
from dst_server.configuration.world import (
    CaveOverrides,
    ForestOverrides,
    LavaArenaOverrides,
    QuagmireOverrides,
    WorldOverrides,
)
from tests.configuration.helpers import (
    ModdedWorldOverrides,
    TestLuaValue,
    UnsupportedWorldOverrides,
    make_cluster,
    schema_definition,
    schema_types,
    source_customize_contract,
)
from tests.lua.helpers import run_lua_process


def test_worldgen_uses_sparse_official_override_and_keeps_default_token() -> None:
    world = WorldgenOverride.forest(
        worldgen_preset="RELAXED",
        settings_preset="RELAXED",
        overrides=ForestOverrides(wildfires="default", world_size="huge"),
    )

    assert world.render() == (
        "return {\n"
        "    override_enabled = true,\n"
        '    worldgen_preset = "RELAXED",\n'
        '    settings_preset = "RELAXED",\n'
        "    overrides = {\n"
        '        ["wildfires"] = "default",\n'
        '        ["world_size"] = "huge",\n'
        "    },\n"
        "}\n"
    )

    partial = WorldgenOverride(overrides=ForestOverrides(day="onlyday")).render()
    assert "worldgen_preset" not in partial
    assert "settings_preset" not in partial
    assert '["day"] = "onlyday"' in partial

    assert "override_enabled = false" in WorldgenOverride(enabled=False).render()


def test_world_factories_reject_wrong_presets_and_override_models() -> None:
    lower_forest = WorldgenOverride.forest(
        worldgen_preset="survival_together",  # ty: ignore[invalid-argument-type]
        settings_preset="survival_together",  # ty: ignore[invalid-argument-type]
    )
    lower_cave = WorldgenOverride.cave(
        worldgen_preset="dst_cave",  # ty: ignore[invalid-argument-type]
        settings_preset="dst_cave",  # ty: ignore[invalid-argument-type]
    )
    assert lower_forest.worldgen_preset == "survival_together"
    assert lower_cave.worldgen_preset == "dst_cave"

    with pytest.raises(ValueError, match="CustomPreset"):
        WorldgenOverride.forest(
            worldgen_preset="DST_CAVE"  # ty: ignore[invalid-argument-type]
        )
    with pytest.raises(ValueError, match="CustomPreset"):
        WorldgenOverride.cave(
            settings_preset="SURVIVAL_TOGETHER"  # ty: ignore[invalid-argument-type]
        )
    with pytest.raises(TypeError, match="ForestOverrides"):
        WorldgenOverride.forest(overrides=CaveOverrides())  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="CaveOverrides"):
        WorldgenOverride.cave(overrides=ForestOverrides())  # ty: ignore[invalid-argument-type]

    custom = WorldgenOverride.forest(worldgen_preset=CustomPreset("MY_MOD_PRESET"))
    assert custom.worldgen_preset == "MY_MOD_PRESET"
    with pytest.raises(ValidationError, match="same world type"):
        WorldgenOverride(
            worldgen_preset="survival_together",
            overrides=CaveOverrides(),
        )
    with pytest.raises(ValidationError, match="cannot be combined"):
        WorldgenOverride(
            worldgen_preset="SURVIVAL_TOGETHER",
            settings_preset="DST_CAVE",
        )
    unicode_custom = WorldgenOverride(
        worldgen_preset="\u017fURVIVAL_TOGETHER",
        overrides=CaveOverrides(),
    )
    assert unicode_custom.worldgen_preset == "\u017fURVIVAL_TOGETHER"


def test_builtin_preset_loading_matches_lua_ascii_case_folding(tmp_path: Path) -> None:
    path = tmp_path / "worldgenoverride.lua"
    path.write_text(
        'return { override_enabled=true, worldgen_preset="quagmire", '
        'settings_preset="quagmire", overrides={} }',
        encoding="utf-8",
    )

    loaded = WorldgenOverride.load(path)

    assert isinstance(loaded.overrides, QuagmireOverrides)


def test_room_presets_compose_worlds_and_mods_without_mutating_inputs() -> None:
    token = SecretStr("preset-token")
    cluster_key = SecretStr("preset-key")
    base_files = FOREST_CAVES.build(
        token=token,
        cluster_key=cluster_key,
    ).files()
    first_mods = RoomPreset(
        mods=ModOverrides(
            entries={
                "local": ModOverride(
                    enabled=True,
                    configuration_options={"value": 1},
                )
            }
        )
    )
    later_mods = RoomPreset(
        mods=ModOverrides(
            entries={
                "local": ModOverride(enabled=False),
                "other": ModOverride(enabled=True),
            }
        )
    )
    first_forest_mods = RoomPreset(
        shards={
            "forest": ShardConfig(
                settings=ShardSettings(),
                mods=first_mods.mods,
            )
        }
    )

    cluster = compose(
        FOREST_CAVES,
        ENDLESS,
        LIGHTS_OUT_GENERATION,
        FOREST_ONLY_NIGHT,
        first_forest_mods,
        later_mods,
    ).build(token=token, cluster_key=cluster_key)

    forest = cluster.shards["forest"]
    cave = cluster.shards["cave"]
    assert forest.world is not None
    assert cave.world is not None
    assert (forest.world.worldgen_preset, forest.world.settings_preset) == (
        "LIGHTS_OUT",
        "ENDLESS",
    )
    assert forest.world.overrides.model_dump(exclude_unset=True) == {"day": "onlynight"}
    assert cave.world.overrides.model_dump(exclude_unset=True) == {
        "basicresource_regrowth": "always",
        "day": "onlynight",
        "ghostsanitydrain": "none",
        "portalresurection": "always",
        "resettime": "none",
    }
    assert all(
        set(shard.mods.entries) == {"local", "other"} for shard in (forest, cave)
    )
    assert forest.mods.entries["local"] == ModOverride(enabled=False)
    assert (
        compose(FOREST_CAVES, later_mods, first_forest_mods)
        .build(token=token, cluster_key=cluster_key)
        .shards["forest"]
        .mods.entries["local"]
        == first_mods.mods.entries["local"]
    )
    assert (
        FOREST_CAVES.build(token=token, cluster_key=cluster_key).files() == base_files
    )
    with pytest.raises(TypeError, match="different world override types"):
        compose(
            FOREST_CAVES,
            shard("forest", WorldgenOverride(overrides=CaveOverrides(day="onlyday"))),
        )


@pytest.mark.parametrize("value", [datetime(2026, 1, 2, tzinfo=UTC), (1, 2)])
def test_custom_world_overrides_reject_non_lua_values(value: object) -> None:
    world = WorldgenOverride(overrides=UnsupportedWorldOverrides(value=value))

    with pytest.raises(ValidationError):
        world.render()


def test_internal_worldgen_topology_overrides_are_typed_and_sparse(
    tmp_path: Path,
) -> None:
    forest = WorldgenOverride.forest(
        overrides=ForestOverrides(
            has_ocean=False,
            layout_mode="RestrictNodesByKey",
            wormhole_prefab="tentacle_pillar",
        )
    )
    path = tmp_path / "worldgenoverride.lua"
    path.write_text(forest.render(), encoding="utf-8")

    assert WorldgenOverride.load(path).overrides.model_dump(exclude_unset=True) == {
        "has_ocean": False,
        "layout_mode": "RestrictNodesByKey",
        "wormhole_prefab": "tentacle_pillar",
    }
    path.write_text(
        WorldgenOverride.cave(overrides=CaveOverrides(has_ocean=None)).render(),
        encoding="utf-8",
    )
    assert configuration.load_lua_table(path, "world override")["overrides"] == {}
    with pytest.raises(ValidationError):
        ForestOverrides(layout_mode="unknown")  # ty: ignore[invalid-argument-type]


def test_world_override_fields_match_pinned_source(
    luajit: str,
    pinned_scripts: Path,
) -> None:
    contract = source_customize_contract(luajit, pinned_scripts)
    options = cast(dict[str, dict[str, object]], contract["options"])
    misc = cast(dict[str, object], contract["misc"])
    for location, model in (("forest", ForestOverrides), ("cave", CaveOverrides)):
        assert set(model.model_fields) == set(options[location]) | set(misc)


def test_world_override_models_cover_native_customize_options(
    luajit: str,
    native_scripts: Path,
) -> None:
    contract = source_customize_contract(luajit, native_scripts)
    source_options = cast(
        dict[str, dict[str, dict[str, object]]],
        contract["options"],
    )
    source_misc = cast(
        dict[str, dict[str, dict[str, object]]],
        contract["misc"],
    )
    misc_keys = {
        "has_ocean",
        "keep_disconnected_tiles",
        "layout_mode",
        "no_joining_islands",
        "no_wormholes_to_disconnected_tiles",
        "wormhole_prefab",
    }

    assert set(source_misc) == misc_keys

    for location, model in (
        ("forest", ForestOverrides),
        ("cave", CaveOverrides),
    ):
        options = source_options[location]
        schema = cast(dict[str, object], model.model_json_schema())
        properties = cast(dict[str, dict[str, object]], schema["properties"])

        native_fields = set(options) | misc_keys
        assert native_fields <= set(model.model_fields)
        for name, source in options.items():
            values = cast(list[str], source["values"])
            definition = schema_definition(schema, properties[name])
            literals = definition.get("enum", [definition.get("const")])

            assert properties[name]["default"] == source["default"]
            assert set(cast(list[str], literals)) == set(values)

        for name, source in source_misc.items():
            forest_source = source["forest"]
            location_source = source[location]
            expected_types = {cast(str, forest_source["kind"])}
            if location_source["kind"] == "nil":
                expected_types.add("null")

            assert properties[name]["default"] == location_source["default"]
            assert schema_types(schema, properties[name]) == expected_types

    layout_defaults = {
        cast(str, source_misc["layout_mode"][location]["default"])
        for location in ("forest", "cave")
    }
    for model in (ForestOverrides, CaveOverrides):
        schema = cast(dict[str, object], model.model_json_schema())
        properties = cast(dict[str, dict[str, object]], schema["properties"])
        definition = schema_definition(schema, properties["layout_mode"])
        assert set(cast(list[str], definition["enum"])) == layout_defaults


@pytest.mark.parametrize("value", [datetime(2026, 1, 2, tzinfo=UTC), (1, 2)])
def test_mod_configuration_rejects_non_lua_values(value: object) -> None:
    with pytest.raises(ValidationError):
        ModOverride.model_validate({"configuration_options": {"value": value}})


def test_sparse_defaults_preserve_explicit_values() -> None:
    implicit_world = WorldgenOverride.forest(overrides=ForestOverrides()).render()
    explicit_world = WorldgenOverride.forest(
        overrides=ForestOverrides(day="default")
    ).render()

    assert "    overrides = {}," in implicit_world
    assert '["day"]' not in implicit_world
    assert '["day"] = "default"' in explicit_world

    implicit_mod = ModOverrides(
        entries={"local": ModOverride(configuration_options={})}
    ).render()
    explicit_mod = ModOverrides(
        entries={"local": ModOverride(configuration_options={"answer": 0})}
    ).render()

    assert "configuration_options" not in implicit_mod
    assert '["answer"] = 0' in explicit_mod


def test_validated_replace_preserves_sparse_fields() -> None:
    settings = ClusterSettings().replace(pvp=True)

    assert settings.model_fields_set == {"pvp"}
    assert settings.pvp is True
    assert ForestOverrides().replace(day="onlyday").model_fields_set == {"day"}
    with pytest.raises(ValidationError):
        settings.replace(pvp="yes")


def test_lua_models_load_their_canonical_files(tmp_path: Path) -> None:
    world = WorldgenOverride(overrides=ForestOverrides(day="onlyday"))
    world_path = tmp_path / "worldgenoverride.lua"
    world_path.write_bytes(
        ("-- canonical world\r\n" + world.render().replace("\n", "\r\n")).encode()
    )

    with pytest.raises(ValueError, match="world type is ambiguous"):
        WorldgenOverride.load(world_path)
    assert (
        WorldgenOverride.load(
            world_path,
            overrides_type=ForestOverrides,
        ).render()
        == world.render()
    )

    mod = ModOverrides(
        entries={
            "local-mod": ModOverride(
                enabled=True,
                configuration_options={
                    "nested": {
                        "invisible": "\u200b",
                        "numbers": [-1, -1.5],
                    },
                },
            )
        }
    )
    mod_path = tmp_path / "modoverrides.lua"
    mod_path.write_bytes(
        ("-- canonical Mods\r\n" + mod.render().replace("\n", "\r\n")).encode()
    )
    assert ModOverrides.load(mod_path).render() == mod.render()

    settings = ModSettings(
        force_enabled=frozenset({"workshop-42"}),
        debug_print=True,
    )
    settings_path = tmp_path / "modsettings.lua"
    settings_path.write_bytes(
        ("-- settings\r\n" + settings.render().replace("\n", "\r\n")).encode()
    )
    assert ModSettings.load(settings_path).render() == settings.render()

    downloads = WorkshopDownloads(
        items=frozenset({42}),
        collections=frozenset({99}),
    )
    downloads_path = tmp_path / "dedicated_server_mods_setup.lua"
    downloads_path.write_bytes(
        (
            "#!/usr/bin/env luajit\r\n" + downloads.render().replace("\n", "\r\n")
        ).encode()
    )
    assert WorkshopDownloads.load(downloads_path).render() == downloads.render()

    empty = ModOverrides(
        entries={
            "empty": ModOverride(configuration_options={"mapping": {}, "sequence": []})
        }
    )
    mod_path.write_text(empty.render(), encoding="utf-8")
    loaded_empty = ModOverrides.load(mod_path)
    options = loaded_empty.entries["empty"].configuration_options
    assert options is not None
    assert options["mapping"] == {}
    assert options["sequence"] == {}


def test_worldgen_loader_upgrades_server_supported_v1_format(tmp_path: Path) -> None:
    path = tmp_path / "worldgenoverride.lua"
    path.write_text(
        """return {
    actualpreset = "SURVIVAL_TOGETHER",
    override_enabled = true,
    presetdata = { overrides = { { "day", "onlynight" } } },
    resources = { world_size = "small" },
    overrides = { wildfires = "never" },
}
""",
        encoding="utf-8",
    )

    loaded = WorldgenOverride.load(path)

    assert loaded.worldgen_preset == "SURVIVAL_TOGETHER"
    assert loaded.settings_preset == "SURVIVAL_TOGETHER"
    assert loaded.overrides.model_dump(exclude_unset=True) == {
        "day": "onlynight",
        "wildfires": "never",
        "world_size": "small",
    }

    path.write_text(
        'return { actualpreset = "SURVIVAL_TOGETHER", '
        "presetdata = { overrides = {} } }\n",
        encoding="utf-8",
    )
    assert WorldgenOverride.load(path).overrides == ForestOverrides()


def test_event_and_legacy_level_only_fields_stay_out_of_worldgen_models() -> None:
    forest_only = {"berrybush", "grass", "spiders", "weather"}
    assert forest_only.isdisjoint(QuagmireOverrides.model_fields)
    assert forest_only.isdisjoint(LavaArenaOverrides.model_fields)
    assert "roads" not in CaveOverrides.model_fields


def test_modded_level_supports_scalar_inference_and_explicit_model(
    tmp_path: Path,
) -> None:
    path = tmp_path / "leveldataoverride.lua"
    path.write_text(
        'return { id="MOD", name="Mod", desc="", location="modland", '
        'custom_settings_id="MOD_SETTINGS", custom_settings_name="Settings", '
        'custom_settings_desc="Settings description", '
        'custom_worldgen_id="MOD_WORLDGEN", custom_worldgen_name="Worldgen", '
        'custom_worldgen_desc="Worldgen description", '
        'overrides={ task_set="mod_taskset", mod_option="enabled" } }',
        encoding="utf-8",
    )

    inferred = LevelDataOverride.load(path)
    explicit = LevelDataOverride.load(path, overrides_type=ModdedWorldOverrides)

    assert isinstance(inferred.overrides, CustomWorldOverrides)
    assert inferred.custom_settings_id == "MOD_SETTINGS"
    assert inferred.custom_worldgen_id == "MOD_WORLDGEN"
    assert explicit.overrides == ModdedWorldOverrides(
        task_set="mod_taskset",
        mod_option="enabled",
    )
    path.write_text(
        'return { id="MOD", name="Mod", desc="", location="modland", '
        'overrides={ task_set="mod_taskset", nested={ enabled=true } } }',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        LevelDataOverride.load(path)

    path.write_text(
        'return { id="MOD", name="Mod", desc="", location="modland", '
        'overrides={ mod_option="enabled" } }',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="task_set"):
        LevelDataOverride.load(path)

    with pytest.raises(ValidationError, match="background_node_range"):
        LevelDataOverride(
            id="FOREST",
            name="Forest",
            desc="",
            location="forest",
            overrides=ForestOverrides(task_set="default"),
            background_node_range=(2, 1),
        )


@pytest.mark.parametrize(
    ("preset", "shard_name", "game_mode"),
    [
        (QUAGMIRE, "gorge", "quagmire"),
        (LAVAARENA, "xforge", "lavaarena"),
    ],
)
def test_legacy_event_level_is_detected_without_worldgen(
    tmp_path: Path,
    preset: RoomPreset,
    shard_name: str,
    game_mode: str,
) -> None:
    shard = preset.shards[shard_name]
    shard.save(tmp_path)
    (tmp_path / "worldgenoverride.lua").unlink()

    loaded = ShardConfig.load(tmp_path)

    assert loaded.level == shard.level
    assert loaded.world is None
    assert preset.build(token=SecretStr("event-token")).settings.game_mode == game_mode


def test_event_world_and_level_follow_lua_layering() -> None:
    settings = ShardSettings(is_master=True)
    gorge = QUAGMIRE.shards["gorge"]
    forge = LAVAARENA.shards["xforge"]
    assert gorge.level is not None
    assert gorge.world is not None
    assert forge.level is not None

    assert ShardConfig(settings=settings, world=gorge.world).world == gorge.world
    partial = WorldgenOverride(overrides=WorldOverrides())
    assert ShardConfig(settings=settings, world=partial, level=gorge.level).level
    assert ShardConfig(settings=settings, world=gorge.world, level=forge.level).level
    override = QuagmireOverrides(
        boons="often",
        branching="most",
        touchstone="always",
        wildfires="often",
        world_size="huge",
    )
    assert override.model_dump(exclude_unset=True) == {
        "boons": "often",
        "branching": "most",
        "touchstone": "always",
        "wildfires": "often",
        "world_size": "huge",
    }
    assert QuagmireOverrides(disease_delay="short").disease_delay == "short"
    with pytest.raises(ValidationError):
        QuagmireOverrides(disease_delay="medium")  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("game_mode", ["quagmire", "lavaarena"])
def test_event_game_mode_requires_complete_level_data(game_mode: str) -> None:
    with pytest.raises(ValidationError, match="requires level data"):
        ClusterConfig(
            settings=ClusterSettings(game_mode=game_mode),
            shards={"forest": ShardConfig(settings=ShardSettings())},
        )


def test_literal_call_loaders_accept_a_final_return_call(tmp_path: Path) -> None:
    path = tmp_path / "managed.lua"
    path.write_text('return ServerModSetup("42")\n', encoding="utf-8")
    assert WorkshopDownloads.load(path) == WorkshopDownloads(items=frozenset({42}))

    path.write_text("return EnableModError()\n", encoding="utf-8")
    assert ModSettings.load(path) == ModSettings(mod_errors=True)


def test_typed_lua_loaders_reject_dynamic_code(tmp_path: Path) -> None:
    world = tmp_path / "worldgenoverride.lua"
    world.write_text(
        'return { worldgen_preset = "MY_MOD_PRESET", overrides = {} }',
        encoding="utf-8",
    )
    assert isinstance(WorldgenOverride.load(world).overrides, WorldOverrides)

    world.write_text(
        'return { worldgen_preset = "survival_together", overrides = {} }',
        encoding="utf-8",
    )
    assert isinstance(WorldgenOverride.load(world).overrides, WorldOverrides)
    assert (
        WorldgenOverride.load(world, overrides_type=ForestOverrides).worldgen_preset
        == "survival_together"
    )
    assert isinstance(
        WorldgenOverride.load(world, overrides_type=ForestOverrides).overrides,
        ForestOverrides,
    )

    mod = tmp_path / "modoverrides.lua"
    mod.write_text(
        'return { ["local"] = { [enabled] = true } }',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="literal strings"):
        ModOverrides.load(mod)

    setup = tmp_path / "dedicated_server_mods_setup.lua"
    setup.write_text(
        'local id = "42"\nServerModSetup(id)\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="direct literal function calls"):
        WorkshopDownloads.load(setup)

    setup.write_text(
        "return ServerModSetup([[\\049]])\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="quoted string"):
        WorkshopDownloads.load(setup)


def test_empty_managed_lua_files_load_as_empty_models(tmp_path: Path) -> None:
    path = tmp_path / "empty.lua"
    path.write_text("-- intentionally empty\n", encoding="utf-8")

    assert ModOverrides.load(path) == ModOverrides()
    assert ModSettings.load(path) == ModSettings()
    assert WorkshopDownloads.load(path) == WorkshopDownloads()


def test_legacy_empty_workshop_setup_placeholder_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "dedicated_server_mods_setup.lua"
    path.write_text('ServerModSetup("")\n', encoding="utf-8")

    assert WorkshopDownloads.load(path) == WorkshopDownloads()
    assert mods.scan_setup(path) == ((), ())


def test_shard_save_preserves_unmanaged_world_and_explicitly_disables_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "worldgenoverride.lua"
    path.write_text("return { keep = true }\n", encoding="utf-8")
    path.chmod(0o640)
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    before = path.stat()

    ShardConfig(settings=ShardSettings(is_master=True)).save(tmp_path)

    after = path.stat()
    assert path.read_text(encoding="utf-8") == "return { keep = true }\n"
    assert after.st_mode & 0o777 == 0o640
    assert after.st_mtime_ns == before.st_mtime_ns

    ShardConfig(
        settings=ShardSettings(is_master=True),
        world=WorldgenOverride(enabled=False),
    ).save(tmp_path)
    assert (
        configuration.load_lua_table(path, "world override")["override_enabled"]
        is False
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("day", []),
        ("day", {}),
        ("day", "invalid"),
        ("extrastartingitems", 5),
        ("ocean_seastack", "default"),
        ("world_size", "insane"),
    ],
)
def test_worldgen_rejects_invalid_official_values(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ForestOverrides.model_validate({key: value})


@pytest.mark.parametrize(
    ("model", "key"),
    [
        (ForestOverrides, "acidrain_enabled"),
        (CaveOverrides, "wildfires"),
        (ForestOverrides, "unknown_option"),
    ],
)
def test_worldgen_rejects_unknown_and_cross_world_keys(
    model: type[ForestOverrides | CaveOverrides],
    key: str,
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate({key: "default"})


def test_recursive_lua_values_enforce_safe_integer_boundaries(tmp_path: Path) -> None:
    maximum = 2**53 - 1
    mod = ModOverride(
        enabled=True,
        configuration_options={"nested": [maximum, -maximum]},
    )

    path = tmp_path / "modoverrides.lua"
    path.write_text(ModOverrides(entries={"local": mod}).render(), encoding="utf-8")

    assert ModOverrides.load(path).entries["local"].configuration_options == {
        "nested": [maximum, -maximum]
    }
    with pytest.raises(ValidationError, match="less than or equal"):
        ModOverride(
            configuration_options={"nested": [{"too_large": 2**53}]},
        )


def test_mod_overrides_are_literal_safe_and_download_only_enabled_workshop() -> None:
    overrides = ModOverrides(
        client_mods_disabled=False,
        entries={
            "local-mod": ModOverride(
                enabled=True,
                configuration_options={
                    "nested": {"quote": 'a"b', "values": [1, False]}
                },
            ),
            "workshop-7": ModOverride(enabled=False),
            "workshop-42": ModOverride(enabled=True),
        },
    )
    rendered = overrides.render()

    assert '["enabled"] = false' in rendered
    assert '["enabled"] = true' in rendered
    assert '["quote"] = "a\\"b"' in rendered
    assert overrides.workshop_items == frozenset({42})

    with pytest.raises(ValidationError):
        ModOverride.model_validate({"configuration_options": {"nested": None}})
    with pytest.raises(ValidationError, match="finite"):
        ModOverride(configuration_options={"nested": float("nan")})
    with pytest.raises(ValidationError, match="unsafe DST mod name"):
        ModOverrides(entries={"../escape": ModOverride(enabled=True)})
    utf8_boundary = "界" * 85
    assert (
        utf8_boundary
        in ModOverrides(entries={utf8_boundary: ModOverride(enabled=True)}).entries
    )
    with pytest.raises(ValidationError, match="unsafe DST mod name"):
        ModOverrides(entries={"界" * 86: ModOverride(enabled=True)})

    omitted = ModOverrides(
        entries={"workshop-100": ModOverride(configuration_options={"answer": 42})}
    )
    assert '"enabled"' not in omitted.render()
    assert omitted.workshop_items == frozenset()


def test_mod_settings_render_commands_and_add_workshop_downloads() -> None:
    assert ModSettings().render() == ""

    settings = ModSettings(
        force_enabled=frozenset({"local-dev", "7", "workshop-42"}),
        debug_print=True,
        mod_errors=True,
        disable_mod_disabling=True,
        disable_local_mod_warning=True,
    )
    assert settings.render() == (
        'ForceEnableMod("7")\n'
        'ForceEnableMod("local-dev")\n'
        'ForceEnableMod("workshop-42")\n'
        "EnableModDebugPrint()\n"
        "EnableModError()\n"
        "DisableModDisabling()\n"
        "DisableLocalModWarning()\n"
    )
    assert settings.workshop_items == frozenset({7, 42})

    files = ClusterConfig(
        shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))},
        mod_settings=settings,
    ).files()
    assert files[Path("mods/modsettings.lua")] == settings.render()
    assert files[Path("mods/dedicated_server_mods_setup.lua")] == (
        'ServerModSetup("7")\nServerModSetup("42")\n'
    )


@pytest.mark.parametrize(
    "name",
    [
        "0",
        "007",
        "+7",
        "1e3",
        "0x10",
        "0x1p2",
        "nan",
        "-Infinity",
        str(2**64),
    ],
)
def test_mod_settings_reject_ambiguous_numeric_force_enabled_names(
    name: str,
) -> None:
    with pytest.raises(ValidationError, match="numeric ForceEnableMod"):
        ModSettings(force_enabled=frozenset({name}))


def test_configuration_containers_are_deeply_immutable() -> None:
    cluster = make_cluster()
    mod = ModOverride(configuration_options={"nested": [{"values": [1]}]})

    with pytest.raises(TypeError, match="immutable"):
        cast(dict[str, ShardConfig], cluster.shards).clear()
    with pytest.raises(TypeError, match="immutable"):
        cast(
            dict[str, ModOverride],
            cluster.shards["Master"].mods.entries,
        ).clear()
    assert mod.configuration_options is not None
    nested = cast(list[TestLuaValue], mod.configuration_options["nested"])
    with pytest.raises(TypeError, match="immutable"):
        nested.append(2)
    mapping = cast(dict[str, TestLuaValue], nested[0])
    with pytest.raises(TypeError, match="immutable"):
        mapping["other"] = 2


def test_every_renderer_revalidates_model_copy_updates() -> None:
    invalid_cluster = ClusterSettings().model_copy(update={"pvp": "yes"})
    invalid_shard = ShardSettings(is_master=True).model_copy(update={"server_port": 1})
    invalid_downloads = WorkshopDownloads().model_copy(update={"items": frozenset({0})})

    with pytest.raises(ValidationError):
        invalid_cluster.render()
    with pytest.raises(ValidationError):
        invalid_shard.render()
    with pytest.raises(ValidationError):
        invalid_downloads.render()


def test_generated_lua_executes_as_literal_tables(
    tmp_path: Path,
    luajit: str,
) -> None:
    world = tmp_path / "worldgenoverride.lua"
    world.write_text(
        WorldgenOverride.cave(overrides=CaveOverrides(day="default")).render(),
        encoding="utf-8",
    )
    mod = tmp_path / "modoverrides.lua"
    mod.write_text(
        ModOverrides(
            entries={
                "local-mod": ModOverride(
                    enabled=True,
                    configuration_options={"nested": [1, {"answer": 42}]},
                )
            }
        ).render(),
        encoding="utf-8",
    )
    checker = tmp_path / "check.lua"
    checker.write_text(
        "local world = dofile(arg[1])\n"
        "local mods = dofile(arg[2])\n"
        "assert(world.override_enabled == true)\n"
        "assert(world.settings_preset == 'DST_CAVE')\n"
        "assert(world.overrides.day == 'default')\n"
        "assert(mods['local-mod'].enabled == true)\n"
        "assert(mods['local-mod'].configuration_options.nested[2].answer == 42)\n",
        encoding="utf-8",
    )

    run_lua_process(luajit, checker, world, mod)
