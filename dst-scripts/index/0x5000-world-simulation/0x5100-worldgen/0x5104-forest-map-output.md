# `0x51040000` Forest Map Output

This page explains how `scripts/map/forest_map.lua` turns a story graph into saveable map data.

It connects the entry path in `0x5101` to layout placement in `0x5103`.

## `0x51041111` Output

`forest_map.Generate` returns a `save` table.

`save.map` contains encoded tiles, topology, roads, dimensions, overrides, generated densities, and `world_tile_map`.

`save.ents` contains worldgen entity coordinates and save records.

`save.world_network.persistdata` contains initial season data and optional weather data.

## `0x51041112` Runtime Boundary

`forest_map.Generate` creates save input but does not instantiate runtime entities; save loading owns that step.

After validation, `GenerateNew` calls `AddWorldEntities` before serializing the result.

On load, `DoInitGame` calls `PopulateWorld`, which reinjects required world records on the master simulation.

`PopulateWorld` then iterates `savedata.ents` and passes each record to `SpawnSaveRecord`.

## `0x51042000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/map/forest_map.lua` | `Generate` | Produces topology, tiles, entities, and the save map |
| `scripts/map/forest_map.lua` | `TranslateWorldGenChoices` | Converts overrides into prefab densities and runtime overrides |
| `scripts/map/forest_map.lua` | `ValidateGroundTile` | Normalizes noise and non-land tiles into usable ground |
| `scripts/map/storygen.lua` | `BuildStory` | Returns topology data and the `Story` instance |
| `scripts/map/graphnode.lua` | `Node:ConvertGround` | Places static layouts |
| `scripts/map/graphnode.lua` | `Node:PopulateVoronoi` | Places density-based prefabs |
| `scripts/map/ocean_gen.lua` | `PopulateOcean` | Populates the ocean |
| `scripts/map/ocean_gen_config.lua` | module table | Configures ocean prefill set pieces |
| `scripts/map/bunch_spawner.lua` | `BunchSpawnerInit` / `BunchSpawnerRun` | Runs bunch-spawner passes |
| `scripts/map/archive_worldgen.lua` | `AncientArchivePass` | Runs archive worldgen |
| `scripts/tiledefs.lua` | `TileManager.AddTile` | Registers `WORLD_TILES` and surface, minimap, and turf properties |
| `scripts/worldtiledefs.lua` | `GetTileInfo` | Caches and exposes ground properties |
| `scripts/tilegroups.lua` | `TileGroupManager` extensions | Classifies land, ocean, impassable, and noise tiles |
| `scripts/gamelogic.lua` | `DoInitGame` / `PopulateWorld` | Loads the map and iterates `savedata.ents` |
| `scripts/worldentities.lua` | `AddWorldEntities` | Adds required world records before spawning |
| `scripts/mainfunctions.lua` | `SpawnSaveRecord` | Instantiates one serialized entity record |

## `0x51043000` Flow

~~~mermaid
flowchart TD
    A["forest_map.Generate"]
    A --> B["copy level.overrides"]
    B --> C["story_gen_params"]
    C --> D["BuildStory(tasks, story_gen_params, level)"]
    D --> E["initialize WorldSim and Voronoi"]
    E --> F["storygen:AddRegionsToMainland"]
    F --> G["WorldGen_Commit + ConvertToTileMap"]
    G --> H["SaveEncode topology"]
    H --> I["ConvertGround + static layouts"]
    I --> J["PopulateVoronoi + prefab densities"]
    J --> K["ocean + bunch + archive passes"]
    K --> L["validate required_prefabs"]
    L --> M["GetEncodedMap + world_tile_map"]
    M --> N["season/weather persistdata + roads"]
    N --> O["return save"]
~~~

### `0x51043111` Parameter Normalization

`Generate` asserts that `level.overrides` exists and deep-copies it into `current_gen_params`.

It maps `start_location`, `islands`, `branching`, `loop`, and `layout_mode` into `story_gen_params`.

It copies `has_ocean` and related graph options into `story_gen_params`, while resolving `world_size` separately.

### `0x51043112` Map Size

`world_size` becomes `min_size`; on non-PS4 platforms both the default and `large` use `425`, while `tiny` uses `1`.

Both `map_width` and `map_height` are set to `min_size`.

### `0x51043211` Story Output

`BuildStory(tasks, story_gen_params, level)` returns `topology_save` and `storygen`.

`topology_save.root` drives `SaveEncode`, `ConvertGround`, `PopulateVoronoi`, and required-prefab collection.

### `0x51043212` WorldSim Commit

`WorldSim:WorldGen_InitializeNodePoints()` initializes node positions, followed by `WorldSim:WorldGen_VoronoiPass(100)`.

Region callbacks add positions with `WorldGen_AddNewPositions` and run `WorldGen_VoronoiPass(50)`.

If `WorldSim:WorldGen_Commit()` fails, `Generate` returns `nil`.

`GenerateNew` makes at most five total generation attempts.

### `0x51043311` Topology and Tile Encoding

`topology_save.root:SaveEncode({width=map_width, height=map_height}, save.map.topology)` serializes topology.

`WorldSim:CreateNodeIdTileMap(save.map.topology.ids)` builds the node-id tile map.

`WorldSim:GetEncodedMap(join_islands)` returns `tiles`, `tiledata`, `nav`, `adj`, and `nodeidtilemap`.

### `0x51043312` Tile Registration

`save.map.world_tile_map = GetWorldTileMap()` depends on registered tiles.

`tiledefs.lua` registers LAND, NOISE, OCEAN, and IMPASSABLE ranges through `TileManager.RegisterTileRange`.

It contains 78 `TileManager.AddTile` calls.

`worldtiledefs.lua` provides the ground-property cache, asset tables, footstep lookup, and `GetTileInfo`.

`tilegroups.lua` provides `IsLandTile`, `IsOceanTile`, `IsImpassableTile`, `IsNoiseTile`, and `IsShallowOceanTile`.

### `0x51043411` Static Layouts

`topology_save.root:GlobalPrePopulate` runs before `topology_save.root:ConvertGround` enters `Node:ConvertGround`.

`Node:ConvertGround` processes `countstaticlayouts` and `terrain_contents_extra.static_layouts`.

It writes those layouts to `entities` through `object_layout.Convert`.

### `0x51043421` Density Prefabs

`TranslateWorldGenChoices(current_gen_params)` converts selected overrides into `translated_prefabs`.

`topology_save.root:PopulateVoronoi` places `distributeprefabs` and `countprefabs`.

`save.map.generated.densities` records generation density.

When a `translated_prefabs` multiplier is below `1`, a later pass removes excess generated entities.

### `0x51043431` Ocean Pass

Ocean processing runs only when `story_gen_params.has_ocean` is true.

It calls `Ocean_SetWorldForOceanGen`, `Ocean_PlaceSetPieces`, and `Ocean_ConvertImpassibleToWater` first.

It then calls `PopulateOcean` and `MonkeyIsland_GenerateDocks`.

`storygen.ocean_population` comes from `Story:ProcessOceanContent`.

### `0x51043441` Postprocessing

`BunchSpawnerInit` and `BunchSpawnerRun` follow primary entity placement, and `AncientArchivePass` follows the bunch pass.

`topology_save.root:GlobalPostPopulate` runs after the ocean pass.

`forest_map.Generate` also removes entities placed on impassable visual tiles.

### `0x51043511` Required Prefabs

Validation combines `level.required_prefabs` with `topology_save.root:GetRequiredPrefabs()`.

It also includes room `required_prefabs` from `storygen.ocean_population`.

A required prefab disabled by a `never` override only produces a disabled message.

Any other missing required prefab makes `Generate` return `nil`.

### `0x51043521` Start Location

`save.ents` must contain at least one of `spawnpoint_multiplayer`, `multiplayer_portal`, `quagmire_portal`, or `lavaarena_portal`.

Otherwise `Generate` prints `PANIC: No start location!` and returns `nil` unless validation is skipped.

### `0x51043531` Season and Roads

`season_start` selects the initial season.

`SEASONS[start_season](start_season)` produces `seasons` and optional `weather` data.

These values populate `save.world_network.persistdata`.

`WorldSim:GetRoad` supplies `save.map.roads`.

## `0x51044100` Verification

~~~bash
rg -n "local function Generate|BuildStory|WorldGen_Commit|ConvertGround|PopulateVoronoi" \
  scripts/map/forest_map.lua
rg -n "PopulateOcean|GetEncodedMap|world_tile_map|No start location" \
  scripts/map/forest_map.lua

rg -n "TileManager\\.AddTile|RegisterTileRange|GetTileInfo|IsLandTile|IsOceanTile|IsImpassableTile" \
  scripts/tiledefs.lua \
  scripts/worldtiledefs.lua \
  scripts/tilemanager.lua \
  scripts/tilegroups.lua

rg -n "DoInitGame|PopulateWorld|AddWorldEntities|SpawnSaveRecord|savedata\.ents" \
  scripts/worldgen_main.lua \
  scripts/gamelogic.lua \
  scripts/worldentities.lua \
  scripts/mainfunctions.lua
~~~

### `0x51044111` Checks

- `Generate` derives `story_gen_params` from `level.overrides`.
- `BuildStory` runs before `WorldGen_Commit`.
- `ConvertGround` runs before `PopulateVoronoi`.
- `story_gen_params.has_ocean` gates the ocean pass.
- Required-prefab validation runs before `save.ents = entities`.
- `GetEncodedMap` writes tiles, navigation, adjacency, and the node-id tile map.
- Initial season data populates `save.world_network.persistdata`.
- `PopulateWorld` injects required world records before iterating `savedata.ents`.
- `SpawnSaveRecord` is the load-side prefab instantiation boundary.
