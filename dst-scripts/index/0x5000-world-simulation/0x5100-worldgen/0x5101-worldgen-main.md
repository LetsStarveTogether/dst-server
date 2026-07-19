# `0x51010000` Worldgen Entry

This page traces a new world from `GEN_PARAMETERS` to `savedata`.

The primary chain is `worldgen_main.lua -> forest_map.lua -> storygen.lua -> WorldSim -> savedata`.

## `0x51011111` Execution Model

`scripts/worldgen_main.lua` ends with `return LoadParametersAndGenerate(false)`.

The host therefore loads and immediately executes it in a dedicated worldgen environment.

It is not a persistent module in the `main.lua` update loop.

## `0x51011112` Scope

This path creates new worlds; save upgrades, cave retrofits, and runtime entity spawning happen elsewhere.

## `0x51012000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/worldgen_main.lua` | `LoadParametersAndGenerate` | Decodes `GEN_PARAMETERS` and configures DLC |
| `scripts/worldgen_main.lua` | `GenerateNew` | Builds the `Level`, selects tasks, and makes up to five generation attempts |
| `scripts/worldgen_main.lua` | `AddSetPeices` | Adds boon, trap, POI, and related set pieces to the level |
| `scripts/worldgen_main.lua` | `CheckMapSaveData` | Validates `savedata.map` and `savedata.ents` |
| `scripts/prefabswaps.lua` | `SelectPrefabSwaps` | Selects prefab substitutions from location and overrides |
| `scripts/worldentities.lua` | `AddWorldEntities` | Injects world-level entities after validation |
| `scripts/map/forest_map.lua` | `Generate` | Builds the story, bakes `WorldSim`, places entities, and encodes the map |
| `scripts/map/storygen.lua` | `BuildStory` | Creates a `Story` and runs `GenerationPipeline` |

### `0x51012111` Entry Point

`LoadParametersAndGenerate` handles input setup, while generation begins in `GenerateNew`.

### `0x51012112` Generation Sequence

`GenerateNew` constructs `Level(world_gen_data.level_data)`.

It then calls `level:ChooseTasks()`, `AddSetPeices(level)`, and `level:ChooseSetPieces()` in that order.

`PrefabSwaps.SelectPrefabSwaps(prefab, level.overrides)` processes map-prefab substitutions first.

`GenerateNew` then passes `level:GetTasksForLevel()` to `forest_map.Generate`.

### `0x51012211` Map Generation Boundary

`forest_map.Generate` converts `level.overrides` into `story_gen_params`.

It then calls `require("map/storygen")` and `BuildStory(tasks, story_gen_params, level)`.

### `0x51012212` Forest Map Responsibilities

`forest_map.Generate` calls `WorldSim:SetWorldSize`, commits worldgen, and converts the result to a tile map.

It then places entities, generates ocean content, and calls `WorldSim:GetEncodedMap`.

See `0x5104-forest-map-output.md` for its output structure.

## `0x51013000` Flow

~~~mermaid
flowchart TD
    A["host provides GEN_PARAMETERS"]
    A --> B["worldgen_main.LoadParametersAndGenerate"]
    B --> C["worldgen_main.GenerateNew"]
    C --> D["Level(world_gen_data.level_data)"]
    D --> E["level:ChooseTasks"]
    E --> F["AddSetPeices + level:ChooseSetPieces"]
    F --> G["forest_map.Generate"]
    G --> H["storygen.BuildStory"]
    H --> I["WorldSim bakes terrain and topology"]
    I --> J["PopulateVoronoi / object_layout / ocean"]
    J --> K["savedata.map + savedata.ents"]
    K --> L["CheckMapSaveData"]
    L --> M["worldentities.AddWorldEntities"]
~~~

### `0x51013111` Required Input

`GEN_PARAMETERS` must decode to `world_gen_data`, and `world_gen_data.level_data` must be present.

`world_gen_data.level_type` becomes `savedata.map.topology.level_type`.

### `0x51013112` Input Failures

`GEN_PARAMETERS == nil` triggers an assertion.

`level.location == nil` also triggers an assertion because `Level.location` selects the map prefab.

### `0x51013211` Ordering Constraint

`ChooseSetPieces` must follow `ChooseTasks` because `Level:ChooseSetPieces` asserts that `self.chosen_tasks` exists.

### `0x51013212` Retry Boundary

`GenerateNew` makes at most five calls to `forest_map.Generate`, so it retries at most four times.

After each non-final failure it runs `collectgarbage("collect")` and `WorldSim:ResetAll()` before retrying.

### `0x51013311` Bake and Encode

`BuildStory` creates the topology tree, and `WorldSim:WorldGen_Commit` commits generation.

`WorldSim:ConvertToTileMap` then creates the tile map.

`topology_save.root:SaveEncode` serializes topology.

### `0x51013312` Entity Output

`forest_map.Generate` collects room content, maze layouts, ocean population, and density prefabs in `entities`.

It then assigns that local table to `save.ents`.

## `0x51014111` `world_gen_data`

`level_data` constructs the `Level`, `level_type` reaches logs and topology, and `show_debug` controls `ShowDebug(savedata)`.

## `0x51014121` Required `savedata` Shape

`CheckMapSaveData` requires `savedata.map`, `map.prefab`, `map.tiles`, `map.width`, `map.height`, `map.topology`, and `ents`.

## `0x51014122` Metadata

Before validation, `GenerateNew` writes build data, `SEED`, and `level.id` to `savedata.meta`.

It also writes `WorldSim:GenerateSessionIdentifier()` and the save version.

## `0x51014131` Return Value

`GenerateNew` removes `savedata.ents` from the top-level serialization and dumps the remaining values with `DataDumper`.

It serializes each entity array separately.

It returns a dumped `data` table for the host, not the original `savedata` table.

## `0x51014211` Set Piece Inputs

`AddSetPeices` reads `boons`, `touchstone`, `traps`, `poi`, and `protected` from `level.overrides`.

Through `AddSingleSetPeice`, it selects layout names from `map/traps` and `map/pointsofinterest`.

It also selects from `map/protected_resources` and `map/boons`.

## `0x51014212` Source Spelling

The source names are `AddSetPeices` and `AddSingleSetPeice`.

Searches and documentation must preserve those spellings rather than normalize them to `Pieces`.

## `0x51015100` Verification

~~~bash
rg -n "LoadParametersAndGenerate|GenerateNew|AddSetPeices|CheckMapSaveData|BuildStory|function Generate" \
  scripts/worldgen_main.lua \
  scripts/map/forest_map.lua \
  scripts/map/storygen.lua
~~~

### `0x51015111` Minimum Trace

Read `LoadParametersAndGenerate` through `GenerateNew`.

Follow `BuildStory`, `WorldGen_Commit`, `PopulateVoronoi`, and `GetEncodedMap` inside `forest_map.Generate`.

Then return to `CheckMapSaveData` and `worldentities.AddWorldEntities`.

### `0x51015112` Checks

- `Level(world_gen_data.level_data)` is built from `level_data`.
- `level:ChooseTasks()` runs before set-piece assignment.
- `forest_map.Generate` returns `savedata`, not spawned runtime entities.
- `worldentities.AddWorldEntities(savedata)` follows `CheckMapSaveData(savedata)`.
- `GenerateNew` returns `DataDumper` output.
