"""Native configuration, room storage, presets, Lua and local script utilities.

Factories validate sparse JSON and preserve explicit ``None`` values.
``dump()`` masks credentials; request ``secrets=True`` to copy native data.
``save()`` requires an offline room and uses the Agent's room lock.
"""

from functools import partial

from ._native import (
    Configuration,
    Room,
    RoomStore,
    build_bundle,
    build_template,
    configuration_schema,
    fleet_room,
    generate_components,
    generate_modutil,
    parse_component,
    parse_lua_literal,
    parse_lua_return_table,
    parse_modutil,
    preset,
    preset_names,
    render_lua_literal,
    room_name,
    room_numbers,
    room_schedule,
    template_names,
    verify_bundle,
)

ClusterSettings = partial(Configuration, "ClusterSettings")
ShardSettings = partial(Configuration, "ShardSettings")
ShardConfig = partial(Configuration, "ShardConfig")
ClusterConfig = partial(Configuration, "ClusterConfig")
RoomPreset = partial(Configuration, "RoomPreset")
WorldgenOverride = partial(Configuration, "WorldgenOverride")
LevelDataOverride = partial(Configuration, "LevelDataOverride")
ModOverride = partial(Configuration, "ModOverride")
ModOverrides = partial(Configuration, "ModOverrides")
ModSettings = partial(Configuration, "ModSettings")
WorkshopDownloads = partial(Configuration, "WorkshopDownloads")
WorldOverrides = partial(Configuration, "WorldOverrides")
ForestOverrides = partial(Configuration, "ForestOverrides")
CaveOverrides = partial(Configuration, "CaveOverrides")
QuagmireOverrides = partial(Configuration, "QuagmireOverrides")
LavaArenaOverrides = partial(Configuration, "LavaArenaOverrides")
CustomWorldOverrides = partial(Configuration, "CustomWorldOverrides")
Policy = partial(Configuration, "Policy")
DailyWindow = partial(Configuration, "DailyWindow")
DeploymentOptions = partial(Configuration, "DeploymentOptions")

compose = Configuration.compose
parse_configuration = Configuration.parse
load_configuration = Configuration.load

__all__ = [
    "CaveOverrides",
    "ClusterConfig",
    "ClusterSettings",
    "Configuration",
    "CustomWorldOverrides",
    "DailyWindow",
    "DeploymentOptions",
    "ForestOverrides",
    "LavaArenaOverrides",
    "LevelDataOverride",
    "ModOverride",
    "ModOverrides",
    "ModSettings",
    "Policy",
    "QuagmireOverrides",
    "Room",
    "RoomPreset",
    "RoomStore",
    "ShardConfig",
    "ShardSettings",
    "WorkshopDownloads",
    "WorldOverrides",
    "WorldgenOverride",
    "build_bundle",
    "build_template",
    "compose",
    "configuration_schema",
    "fleet_room",
    "generate_components",
    "generate_modutil",
    "load_configuration",
    "parse_component",
    "parse_configuration",
    "parse_lua_literal",
    "parse_lua_return_table",
    "parse_modutil",
    "preset",
    "preset_names",
    "render_lua_literal",
    "room_name",
    "room_numbers",
    "room_schedule",
    "template_names",
    "verify_bundle",
]
