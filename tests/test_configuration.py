from __future__ import annotations

import json
import os
import subprocess  # ruff:ignore[suspicious-subprocess-import]
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr, ValidationError

from dst_server.cluster import (
    CaveOverrides,
    ClusterConfig,
    ClusterSettings,
    CustomPreset,
    CustomWorldOverrides,
    ForestOverrides,
    LavaArenaOverrides,
    LevelDataOverride,
    ModOverride,
    ModOverrides,
    ModSettings,
    QuagmireOverrides,
    RoomPreset,
    ShardConfig,
    ShardSettings,
    WorkshopDownloads,
    WorldgenOverride,
    WorldOverrides,
    discover,
    mods,
)
from dst_server.cluster import config as configuration
from dst_server.cluster.presets import (
    ENDLESS,
    FOREST_CAVES,
    FOREST_ONLY_NIGHT,
    LAVAARENA,
    LIGHTS_OUT_GENERATION,
    QUAGMIRE,
    compose,
    shard,
)

type TestLuaValue = (
    str | bool | int | float | list[TestLuaValue] | dict[str, TestLuaValue]
)


class UnsupportedWorldOverrides(WorldOverrides):
    value: object


class ModdedWorldOverrides(WorldOverrides):
    task_set: str
    mod_option: str


def source_customize_contract(luajit: str) -> dict[str, object]:
    root = Path(__file__).parents[1]
    result = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        [luajit, str(root / "tests/lua/customize_contract.lua"), str(root)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    return cast(dict[str, object], json.loads(result.stdout))


def schema_definition(
    schema: dict[str, object],
    node: dict[str, object],
) -> dict[str, object]:
    reference = node.get("$ref")
    if reference is None:
        return node
    definitions = cast(dict[str, dict[str, object]], schema["$defs"])
    return definitions[cast(str, reference).rsplit("/", 1)[1]]


def schema_types(
    schema: dict[str, object],
    node: dict[str, object],
) -> set[str]:
    definition = schema_definition(schema, node)
    variants = definition.get("anyOf")
    if variants is not None:
        return set().union(
            *(
                schema_types(schema, variant)
                for variant in cast(list[dict[str, object]], variants)
            )
        )
    return {cast(str, definition["type"])}


def test_ini_defaults_are_omitted_but_explicit_defaults_are_preserved(
    tmp_path: Path,
) -> None:
    assert ClusterSettings().render() == ""
    server_ini = tmp_path / "server.ini"
    server_ini.write_text("", encoding="utf-8")
    loaded_shard = ShardSettings.load(server_ini)
    assert loaded_shard.is_master is True
    assert loaded_shard.model_fields_set == set()
    assert loaded_shard.render() == ""
    assert ShardSettings().render(multi_shard=True) == ("[SHARD]\nis_master = true\n")
    assert ShardSettings(is_master=True).encode_user_path is False
    assert "encode_user_path" not in ShardSettings(is_master=True).render()
    settings = ClusterSettings(
        max_snapshots=6,
        offline_cluster=False,
        max_players=16,
        pause_when_empty=False,
        cluster_password=SecretStr(""),
    )
    expected = (
        "[MISC]\n"
        "max_snapshots = 6\n"
        "\n"
        "[NETWORK]\n"
        "cluster_password = \n"
        "offline_cluster = false\n"
        "\n"
        "[GAMEPLAY]\n"
        "max_players = 16\n"
        "pause_when_empty = false\n"
    )

    assert settings.render() == expected
    path = tmp_path / "cluster.ini"
    path.write_text(expected.lower(), encoding="utf-8")
    loaded = ClusterSettings.load(path)

    assert loaded == settings
    assert loaded.model_fields_set == settings.model_fields_set
    assert loaded.render() == expected


def test_ini_loader_rejects_unknown_input(tmp_path: Path) -> None:
    path = tmp_path / "cluster.ini"
    path.write_text("[NETWORK]\nfuture_option = true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown DST INI option"):
        ClusterSettings.load(path)


@pytest.mark.parametrize("game_mode", ["endless", "wilderness"])
def test_ini_loader_warns_and_preserves_deprecated_game_modes(
    tmp_path: Path,
    game_mode: str,
) -> None:
    path = tmp_path / "cluster.ini"
    source = f"[GAMEPLAY]\ngame_mode = {game_mode}\n"
    path.write_text(source, encoding="utf-8")

    with pytest.warns(FutureWarning, match=f"game_mode='{game_mode}' is deprecated"):
        settings = ClusterSettings.load(path)

    assert settings.game_mode == game_mode
    assert settings.render() == source


def test_ini_loader_preserves_mod_game_mode(tmp_path: Path) -> None:
    path = tmp_path / "cluster.ini"
    source = "[GAMEPLAY]\ngame_mode = modded_mode\n"
    path.write_text(source, encoding="utf-8")

    settings = ClusterSettings.load(path)

    assert settings.game_mode == "modded_mode"
    assert settings.render() == source


def test_shard_loader_discards_legacy_authentication_port(tmp_path: Path) -> None:
    path = tmp_path / "server.ini"
    path.write_text("[STEAM]\nauthentication_port = 8766\n", encoding="utf-8")

    settings = ShardSettings.load(path)

    assert settings.model_fields_set == set()
    assert settings.render() == ""
    assert "authentication_port" not in settings.model_dump()


def test_ini_models_reject_non_utf8_text() -> None:
    with pytest.raises(ValidationError):
        ClusterSettings(cluster_name="\ud800")
    with pytest.raises(ValidationError):
        ShardSettings(name="\ud800")


def test_ini_loader_matches_native_boolean_and_network_parsing(tmp_path: Path) -> None:
    path = tmp_path / "cluster.ini"
    path.write_text(
        "[NETWORK]\n"
        "connection_timeout = 42\n"
        "internet_broadcasting_enabled = FALSE\n"
        "idle_timeout = 7\n"
        "[GAMEPLAY]\n"
        "pvp = TrUe\n",
        encoding="utf-8",
    )

    settings = ClusterSettings.load(path)

    assert settings.connection_timeout == 42
    assert settings.internet_broadcasting_enabled is False
    assert settings.idle_timeout == 7
    assert settings.pvp is True
    path.write_text("[GAMEPLAY]\npvp = yes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="true or false"):
        ClusterSettings.load(path)


def test_tick_rate_one_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "cluster.ini"
    path.write_text("[NETWORK]\ntick_rate = 1\n", encoding="utf-8")

    settings = ClusterSettings.load(path)

    assert settings.tick_rate == 1
    assert settings.render() == "[NETWORK]\ntick_rate = 1\n"
    for tick_rate in (0, 61):
        with pytest.raises(ValidationError):
            ClusterSettings(tick_rate=tick_rate)


def test_native_misc_booleans_load_and_render_when_explicit(tmp_path: Path) -> None:
    assert ClusterSettings(use_alternate_gc=False, mods_enabled=True).render() == (
        "[MISC]\nuse_alternate_gc = false\nmods_enabled = true\n"
    )
    path = tmp_path / "cluster.ini"
    path.write_text(
        "[MISC]\nuse_alternate_gc = TRUE\nmods_enabled = false\n",
        encoding="utf-8",
    )

    settings = ClusterSettings.load(path)

    assert settings.use_alternate_gc is True
    assert settings.mods_enabled is False
    assert settings.render() == (
        "[MISC]\nuse_alternate_gc = true\nmods_enabled = false\n"
    )


def test_network_language_and_unsupported_dns_are_typed() -> None:
    settings = ClusterSettings(cluster_language="zht", override_dns="1.1.1.1")

    assert settings.render() == (
        "[NETWORK]\ncluster_language = zht\noverride_dns = 1.1.1.1\n"
    )
    with pytest.raises(ValidationError):
        ClusterSettings(cluster_language="ja")  # ty: ignore[invalid-argument-type]


def test_native_ini_numeric_boundaries() -> None:
    defaults = ClusterSettings()

    assert defaults.connection_timeout == 8000
    assert defaults.internet_broadcasting_enabled is True
    assert defaults.idle_timeout == 1800
    assert (
        ShardSettings(
            is_master=False,
            name="Secondary",
            id=2**32 - 1,
        ).id
        == 2**32 - 1
    )
    with pytest.raises(ValidationError):
        ClusterSettings(max_snapshots=0)
    with pytest.raises(ValidationError):
        ClusterSettings.model_validate({"idle_timeout": 0.5})
    with pytest.raises(ValidationError):
        ShardSettings(is_master=False, name="Secondary", id=2**32)


def test_shared_configuration_types_preserve_constraints() -> None:
    with pytest.raises(ValidationError, match="NUL"):
        ClusterSettings(cluster_key=SecretStr("bad\nsecret"))
    with pytest.raises(ValidationError, match="254"):
        ClusterSettings(cluster_password=SecretStr("x" * 255))
    cluster_key = ShardSettings(
        is_master=True,
        cluster_key=SecretStr("x" * 255),
    ).cluster_key
    assert cluster_key is not None
    assert cluster_key.get_secret_value() == "x" * 255
    with pytest.raises(ValidationError):
        WorldgenOverride(worldgen_preset="bad\npreset")


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
    assert 'worldgen_preset = "MY_MOD_PRESET"' in custom.render()
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
    assert 'worldgen_preset = "\u017fURVIVAL_TOGETHER"' in unicode_custom.render()


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


def test_internal_worldgen_topology_overrides_are_typed_and_sparse() -> None:
    forest = WorldgenOverride.forest(
        overrides=ForestOverrides(
            has_ocean=False,
            layout_mode="RestrictNodesByKey",
            wormhole_prefab="tentacle_pillar",
        )
    ).render()

    assert '["has_ocean"] = false' in forest
    assert '["layout_mode"] = "RestrictNodesByKey"' in forest
    assert '["wormhole_prefab"] = "tentacle_pillar"' in forest
    assert (
        "    overrides = {},"
        in WorldgenOverride.cave(overrides=CaveOverrides(has_ocean=None)).render()
    )
    with pytest.raises(ValidationError):
        ForestOverrides(layout_mode="unknown")  # ty: ignore[invalid-argument-type]


def test_world_override_models_match_bundled_customize_source(
    luajit: str,
) -> None:
    contract = source_customize_contract(luajit)
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

        assert set(model.model_fields) == set(options) | misc_keys
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
    assert settings.render() == "[GAMEPLAY]\npvp = true\n"
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
    with pytest.raises(ValueError, match="double-quoted string"):
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
    assert mods.setup_downloads(path) == ((), ())


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
    assert "override_enabled = false" in path.read_text(encoding="utf-8")


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


def test_recursive_lua_values_enforce_safe_integer_boundaries() -> None:
    maximum = 2**53 - 1
    mod = ModOverride(
        enabled=True,
        configuration_options={"nested": [maximum, -maximum]},
    )

    assert str(-maximum) in ModOverrides(entries={"local": mod}).render()
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


def make_cluster() -> ClusterConfig:
    settings = ClusterSettings(
        cluster_name="SDK configuration test",
        offline_cluster=True,
        lan_only_cluster=True,
        autosaver_enabled=False,
        master_ip="127.0.0.1",
        cluster_key=SecretStr("test-cluster-key"),
        max_players=1,
    )
    master = ShardConfig(
        settings=ShardSettings(
            is_master=True,
            id=1,
            master_server_port=27016,
            server_port=10999,
        ),
        world=WorldgenOverride.forest(overrides=ForestOverrides(day="onlyday")),
        mods=ModOverrides(entries={"workshop-42": ModOverride(enabled=True)}),
    )
    caves = ShardConfig(
        settings=ShardSettings(
            is_master=False,
            name="Caves",
            id=2,
            master_server_port=27017,
            server_port=11000,
        ),
        world=WorldgenOverride.cave(),
    )
    return ClusterConfig(
        settings=settings,
        shards={"Master": master, "Caves": caves},
        token=SecretStr("test-token"),
        adminlist="KU_admin\n",
        whitelist="KU_friend\n",
        blocklist="76561198000000000\n",
        downloads=WorkshopDownloads(
            items=frozenset({8}),
            collections=frozenset({99}),
        ),
    )


def test_cluster_cascade_saves_complete_tree_and_preserves_game_files(
    tmp_path: Path,
) -> None:
    cluster = make_cluster()
    (tmp_path / "Master" / "save").mkdir(parents=True)
    (tmp_path / "Master" / "save" / "world").write_text(
        "keep",
        encoding="utf-8",
    )
    (tmp_path / "Master" / "leveldataoverride.lua").write_text(
        'return { id = "SDK", name = "SDK", desc = "", '
        'location = "forest", overrides = { task_set = "default" } }',
        encoding="utf-8",
    )
    written = cluster.save(tmp_path)

    assert written
    assert (
        (tmp_path / "cluster.ini")
        .read_text(encoding="utf-8")
        .startswith("[SHARD]\nshard_enabled = true\n")
    )
    assert "is_master = true" in (tmp_path / "Master" / "server.ini").read_text(
        encoding="utf-8"
    )
    assert "is_master = false" in (tmp_path / "Caves" / "server.ini").read_text(
        encoding="utf-8"
    )
    assert (tmp_path / "cluster_token.txt").read_text(encoding="utf-8") == (
        "test-token\n"
    )
    assert (tmp_path / "adminlist.txt").read_text(encoding="utf-8") == "KU_admin\n"
    assert (tmp_path / "whitelist.txt").read_text(encoding="utf-8") == "KU_friend\n"
    assert (tmp_path / "blocklist.txt").read_text(encoding="utf-8") == (
        "76561198000000000\n"
    )
    assert (tmp_path / "cluster_token.txt").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "cluster.ini").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "Master" / "server.ini").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "mods" / "ugc").is_dir()
    assert (tmp_path / "mods" / "dedicated_server_mods_setup.lua").read_text(
        encoding="utf-8"
    ) == ('ServerModSetup("8")\nServerModSetup("42")\nServerModCollectionSetup("99")\n')
    assert (tmp_path / "Master" / "save" / "world").read_text(
        encoding="utf-8"
    ) == "keep"
    assert (tmp_path / "Master" / "leveldataoverride.lua").read_text(
        encoding="utf-8"
    ) == (
        'return { id = "SDK", name = "SDK", desc = "", '
        'location = "forest", overrides = { task_set = "default" } }'
    )
    assert {shard.name for shard in discover(tmp_path)} == {"Master", "Caves"}
    assert all((tmp_path / name).is_file() for name in configuration.PERMISSION_FILES)

    (tmp_path / "cluster_token.txt").write_text("preserve-token\n", encoding="utf-8")
    (tmp_path / "mods" / "modsettings.lua").write_text(
        'ForceEnableMod("local-dev-mod")\n',
        encoding="utf-8",
    )
    ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        downloads=cluster.downloads,
    ).save(tmp_path)
    assert (tmp_path / "cluster_token.txt").read_text(
        encoding="utf-8"
    ) == "preserve-token\n"
    assert (tmp_path / "adminlist.txt").read_text(encoding="utf-8") == "KU_admin\n"
    assert (tmp_path / "whitelist.txt").read_text(encoding="utf-8") == "KU_friend\n"
    assert (tmp_path / "blocklist.txt").read_text(encoding="utf-8") == (
        "76561198000000000\n"
    )
    assert (tmp_path / "mods" / "modsettings.lua").read_text(encoding="utf-8") == (
        'ForceEnableMod("local-dev-mod")\n'
    )

    ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        adminlist="",
        downloads=cluster.downloads,
    ).save(tmp_path)
    assert (tmp_path / "adminlist.txt").read_text(encoding="utf-8") == ""

    ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        downloads=cluster.downloads,
        mod_settings=ModSettings(),
    ).save(tmp_path)
    assert (tmp_path / "mods" / "modsettings.lua").read_text(encoding="utf-8") == ""


def test_cluster_load_edit_save_round_trip(tmp_path: Path) -> None:
    original = make_cluster()
    original.save(tmp_path)
    (tmp_path / "cluster_token.txt").write_bytes(b"test-token\r\n")
    (tmp_path / "adminlist.txt").write_bytes(b"KU_admin\r\n")
    (tmp_path / "whitelist.txt").write_bytes(b"KU_friend\r\n")
    (tmp_path / "blocklist.txt").write_bytes(b"76561198000000000\r\n")

    loaded = ClusterConfig.load(tmp_path)

    assert loaded.files() == original.files()
    master_world = loaded.shards["Master"].world
    cave_world = loaded.shards["Caves"].world
    assert master_world is not None
    assert cave_world is not None
    assert isinstance(master_world.overrides, ForestOverrides)
    assert isinstance(cave_world.overrides, CaveOverrides)

    edited = loaded.replace(
        settings=loaded.settings.replace(pvp=True),
        adminlist="",
    )
    edited.save(tmp_path)
    reloaded = ClusterConfig.load(tmp_path)

    assert reloaded.settings.pvp is True
    assert reloaded.adminlist == ""

    (tmp_path / "adminlist.txt").write_bytes(b"KU_admin\rKU_other\n")
    with pytest.raises(ValidationError, match="permission files"):
        ClusterConfig.load(tmp_path)


def test_cluster_load_rejects_missing_multi_shard_enable_flag(
    tmp_path: Path,
) -> None:
    make_cluster().save(tmp_path)
    cluster_ini = tmp_path / "cluster.ini"
    source = cluster_ini.read_text(encoding="utf-8")
    marker = "shard_enabled = true\n"
    assert marker in source
    cluster_ini.write_text(source.replace(marker, "", 1), encoding="utf-8")

    with pytest.raises(ValueError, match="shard_enabled = true is required"):
        ClusterConfig.load(tmp_path)

    cluster_ini.write_text(
        source.replace(marker, "shard_enabled = false\n", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="cannot be false"):
        ClusterConfig.load(tmp_path)


def test_cluster_load_rejects_implicit_multi_shard_role(tmp_path: Path) -> None:
    make_cluster().save(tmp_path)
    server_ini = tmp_path / "Master" / "server.ini"
    source = server_ini.read_text(encoding="utf-8")
    marker = "is_master = true\n"
    assert marker in source
    server_ini.write_text(source.replace(marker, "", 1), encoding="utf-8")

    with pytest.raises(ValueError, match="is_master is required"):
        ClusterConfig.load(tmp_path)


def test_preserved_modsettings_keep_their_existing_downloads(tmp_path: Path) -> None:
    shards = {"Master": ShardConfig(settings=ShardSettings(is_master=True))}
    ClusterConfig(
        shards=shards,
        mod_settings=ModSettings(force_enabled=frozenset({"workshop-123"})),
    ).save(tmp_path)

    ClusterConfig(shards=shards).save(tmp_path)

    assert (tmp_path / "mods" / "modsettings.lua").read_text(encoding="utf-8") == (
        'ForceEnableMod("workshop-123")\n'
    )
    assert (tmp_path / "mods" / "dedicated_server_mods_setup.lua").read_text(
        encoding="utf-8"
    ) == 'ServerModSetup("123")\n'

    ClusterConfig(shards=shards, mod_settings=ModSettings()).save(tmp_path)
    assert (tmp_path / "mods" / "modsettings.lua").read_text(encoding="utf-8") == ""
    assert (tmp_path / "mods" / "dedicated_server_mods_setup.lua").read_text(
        encoding="utf-8"
    ) == ""


@pytest.mark.parametrize("value", ["KU_one\0KU_two", "KU_one\r\nKU_two", "\ud800"])
def test_cluster_rejects_invalid_permission_file_text(value: str) -> None:
    with pytest.raises(ValidationError, match="permission files"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))},
            whitelist=value,
        )


def test_permission_files_preserve_blank_lines_and_unset_metadata(
    tmp_path: Path,
) -> None:
    cluster = make_cluster()
    cluster = ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        adminlist="\nKU_admin\n\n",
        downloads=cluster.downloads,
    )
    cluster.save(tmp_path)
    admin = tmp_path / "adminlist.txt"
    assert admin.read_bytes() == b"\nKU_admin\n\n"

    admin.chmod(0o640)
    os.utime(admin, ns=(1_000_000_000, 1_000_000_000))
    before = admin.stat()
    ClusterConfig(
        settings=cluster.settings,
        shards=cluster.shards,
        downloads=cluster.downloads,
    ).save(tmp_path)

    after = admin.stat()
    assert admin.read_bytes() == b"\nKU_admin\n\n"
    assert after.st_mode & 0o777 == 0o640
    assert after.st_mtime_ns == before.st_mtime_ns


def test_mod_prepare_preserves_generated_downloads(tmp_path: Path) -> None:
    cluster = tmp_path / "cluster"
    make_cluster().save(cluster)
    install = tmp_path / "install"
    install.mkdir()

    assert mods.prepare(install, cluster) == (8, 42)
    assert (cluster / "mods" / "dedicated_server_mods_setup.lua").read_text(
        encoding="utf-8"
    ) == ('ServerModSetup("8")\nServerModSetup("42")\nServerModCollectionSetup("99")\n')


def test_mod_prepare_preserves_setup_lua_and_only_prepends_missing_items(
    tmp_path: Path,
) -> None:
    cluster = tmp_path / "cluster"
    shard = cluster / "Master"
    mods_path = cluster / "mods"
    shard.mkdir(parents=True)
    mods_path.mkdir()
    (shard / "modoverrides.lua").write_text(
        'return { ["workshop-42"] = { enabled = true } }',
        encoding="utf-8",
    )
    setup = mods_path / "dedicated_server_mods_setup.lua"
    original = (
        "#!/usr/bin/env luajit\r\n"
        'local collection = "99"\r\n'
        "ServerModCollectionSetup(collection)\r\n"
        '--[[\r\nServerModSetup("42")\r\n]]\r\n'
        'ServerModSetup("8"); return ServerModSetup("9")\r\n'
    )
    setup.write_text(original, encoding="utf-8")
    install = tmp_path / "install"
    install.mkdir()

    assert mods.prepare(install, cluster) == (8, 9, 42)
    shebang, body = original.split("\n", 1)
    expected = f'{shebang}\nServerModSetup("42")\n{body}'
    assert setup.read_bytes() == expected.encode()
    assert mods.prepare(install, cluster) == (8, 9, 42)
    assert setup.read_bytes() == expected.encode()


def test_mod_override_parser_accepts_empty_files_and_rejects_truthy_numbers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "modoverrides.lua"
    path.write_text("-- no overrides\n", encoding="utf-8")
    assert mods.workshop_ids((path,)) == ()

    path.write_text(
        'return { ["workshop-42"] = { enabled = 1 } }',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="literal boolean"):
        mods.workshop_ids((path,))


def test_mod_file_parsers_enforce_static_workshop_boundaries(tmp_path: Path) -> None:
    override = tmp_path / "modoverrides.lua"
    override.write_text(
        'return { ["workshop-18446744073709551616"] = { enabled = true } }',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="uint64"):
        mods.workshop_ids((override,))

    override.write_text(
        'return { [("workshop-" .. "42")] = { enabled = true } }',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="string"):
        mods.workshop_ids((override,))

    invalid_overrides = (
        ("return { client_mods_disabled = 1 }", "literal boolean"),
        ('return { ["local"] = true }', "literal tables"),
        (
            'return { ["workshop-42"] = { enabled = true, enabled = false } }',
            "duplicate key",
        ),
        (
            'return { ["workshop-42"] = {}, ["workshop-42"] = {} }',
            "duplicate key",
        ),
    )
    for content, message in invalid_overrides:
        override.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            mods.workshop_ids((override,))

    setup = tmp_path / "dedicated_server_mods_setup.lua"
    setup.write_text(
        'ServerModSetup("18446744073709551616")\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="uint64"):
        mods.setup_downloads(setup)


@pytest.mark.parametrize(
    ("shards", "message"),
    [
        (
            {
                "one": ShardConfig(settings=ShardSettings(is_master=False)),
            },
            "exactly one master",
        ),
        (
            {
                "one": ShardConfig(settings=ShardSettings(is_master=True)),
                "two": ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "exactly one master",
        ),
        (
            {
                "mods": ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "unsafe DST shard",
        ),
        (
            {
                "cluster.ini": ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "unsafe DST shard",
        ),
        (
            {
                "\ud800": ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "unsafe DST shard",
        ),
        (
            {
                "x" * 256: ShardConfig(settings=ShardSettings(is_master=True)),
            },
            "unsafe DST shard",
        ),
    ],
)
def test_cluster_rejects_invalid_topology(
    shards: dict[str, ShardConfig],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        ClusterConfig(shards=shards)


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


def test_cluster_rejects_token_line_injection() -> None:
    with pytest.raises(ValidationError, match="cluster tokens"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))},
            token=SecretStr("first\nsecond"),
        )


@pytest.mark.parametrize("token", ["contains space", "tab\there", "令牌"])
def test_cluster_rejects_tokens_the_game_would_silently_filter(token: str) -> None:
    with pytest.raises(ValidationError, match="printable non-space ASCII"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))},
            token=SecretStr(token),
        )


def test_cluster_save_refuses_unmanaged_shard_directory(tmp_path: Path) -> None:
    (tmp_path / "OldCaves").mkdir()

    with pytest.raises(ValueError, match="would remain active"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))}
        ).save(tmp_path)


def test_cluster_save_preflights_managed_paths_before_replacing_files(
    tmp_path: Path,
) -> None:
    cluster = tmp_path / "cluster"
    mods_path = cluster / "mods"
    mods_path.mkdir(parents=True)
    (cluster / "cluster.ini").write_text("original", encoding="utf-8")
    (mods_path / "ugc").write_text("conflict", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))}
        ).save(cluster)

    assert (cluster / "cluster.ini").read_text(encoding="utf-8") == "original"


def test_cluster_save_rejects_shard_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    cluster = tmp_path / "cluster"
    target = tmp_path / "outside"
    cluster.mkdir()
    target.mkdir()
    (cluster / "cluster.ini").write_text("original", encoding="utf-8")
    (cluster / "Master").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="cannot be a symlink"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))}
        ).save(cluster)

    assert list(target.iterdir()) == []
    assert (cluster / "cluster.ini").read_text(encoding="utf-8") == "original"


def test_cluster_save_rejects_symlink_root_before_reading_files(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    mods_path = outside / "mods"
    mods_path.mkdir(parents=True)
    (mods_path / "modsettings.lua").write_text("", encoding="utf-8")
    (mods_path / "dedicated_server_mods_setup.lua").write_text(
        "invalid lua !",
        encoding="utf-8",
    )
    root = tmp_path / "cluster"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="root cannot be a symlink"):
        ClusterConfig(
            shards={"Master": ShardConfig(settings=ShardSettings(is_master=True))}
        ).save(root)


def test_multi_shard_requires_explicit_non_conflicting_network() -> None:
    master = ShardConfig(settings=ShardSettings(is_master=True))
    caves = ShardConfig(settings=ShardSettings(is_master=False, name="Caves"))

    with pytest.raises(ValidationError, match="cluster_key"):
        ClusterConfig(shards={"Master": master, "Caves": caves})

    with pytest.raises(ValidationError, match="cluster_key"):
        ClusterConfig(
            settings=ClusterSettings(shard_enabled=True),
            shards={"Master": master},
        )

    with pytest.raises(ValidationError, match="UDP port"):
        ClusterConfig(
            settings=ClusterSettings(
                master_ip="127.0.0.1",
                cluster_key=SecretStr("key"),
            ),
            shards={"Master": master, "Caves": caves},
        )

    with pytest.raises(ValidationError, match="cluster_key"):
        ClusterConfig(
            settings=ClusterSettings(
                master_ip="127.0.0.1",
                cluster_key=SecretStr("key"),
            ),
            shards={
                "Master": master,
                "Caves": caves.replace(
                    settings=caves.settings.replace(cluster_key=SecretStr(""))
                ),
            },
        )


def test_shard_ids_follow_master_and_secondary_roles() -> None:
    with pytest.raises(ValidationError, match="master shard id must be 1"):
        ShardSettings(is_master=True, id=2)
    with pytest.raises(ValidationError, match="secondary shard ids must be at least 2"):
        ShardSettings(is_master=False, name="Caves", id=1)

    ClusterConfig(
        settings=ClusterSettings(
            master_ip="127.0.0.1",
            cluster_key=SecretStr("key"),
        ),
        shards={
            "Master": ShardConfig(settings=ShardSettings(is_master=True, id=1)),
            "Caves": ShardConfig(
                settings=ShardSettings(
                    is_master=False,
                    name="Caves",
                    id=2,
                    master_server_port=27017,
                    server_port=11000,
                )
            ),
        },
    )


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

    subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]
        (luajit, str(checker), str(world), str(mod)),
        check=True,
    )
