# `0x51000000` World Generation

This directory traces worldgen from input parameters to `savedata.map` and `savedata.ents`.

## `0x51001111` Scope

The path starts in `scripts/worldgen_main.lua` and continues through `scripts/map/*`.

It also covers `scripts/tiledefs.lua`, `scripts/worldtiledefs.lua`, `scripts/tilemanager.lua`, and `scripts/tilegroups.lua`.

See [Save and Load](../../0x2000-runtime/0x2200-engine-services/0x2202-save-load.md) for save upgrades.

Runtime world state belongs in [World State](../0x5200-world-state/README.md).

Component updates and prefab behaviour belong in `0x6000-gameplay-systems` or `0x8000-reference`.

## `0x51002111` Pages

- [Worldgen Entry](0x5101-worldgen-main.md)
- [Levels, Tasks, and Rooms](0x5102-levels-tasks-rooms.md)
- [Layouts and Set Pieces](0x5103-layouts.md)
- [Forest Map Output](0x5104-forest-map-output.md)

## `0x51002211` Chapter Boundaries

`0x5101` covers parameters, retries, DLC setup, save validation, and world-entity injection.

`0x5102` covers presets, task sets, tasks, rooms, and the story graph.

`0x5103` covers graph layout, static layouts, set pieces, and object placement.

`0x5104` covers how `forest_map.Generate` bakes topology, tiles, entities, ocean content, roads, and season data.

## `0x51003111` Reading Order

Start with `0x5101-worldgen-main.md` for `GEN_PARAMETERS -> GenerateNew -> forest_map.Generate`.

Continue with `0x5102-levels-tasks-rooms.md` to follow `Level:ChooseTasks` into `Story:GenerateNodesFromTask`.

Read `0x5103-layouts.md` to follow set pieces and static layouts into `entities`.

Finish with `0x5104-forest-map-output.md` for `WorldSim`, ocean generation, roads, encoding, and `savedata` fields.

Use [Reference](../../0x8000-reference/README.md) for the complete file inventory.
