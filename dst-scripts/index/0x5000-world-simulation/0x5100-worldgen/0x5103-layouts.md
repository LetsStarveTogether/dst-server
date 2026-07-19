# `0x51030000` Layouts and Set Pieces

This page traces static layouts from worldgen selection tables into `savedata.ents`.

It also separates graph, static, and object layout responsibilities.

## `0x51031111` Placement Boundary

`AddSetPeices` records layout names in `level.set_pieces`.

`storygen.lua` writes those names to node `countstaticlayouts`.

`Node:ConvertGround` then calls `object_layout.Convert`.

`object_layout.ReserveAndPlaceLayout` performs space reservation and entity placement.

## `0x51031112` Layout Types

`map/layout.lua` positions graph nodes with a force-directed algorithm.

`map/static_layout.lua` converts Tiled modules into layout tables.

`map/object_layout.lua` places those tables inside map nodes or at explicit coordinates.

## `0x51032000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/map/layout.lua` | `layout.run` | Positions graph nodes |
| `scripts/map/static_layout.lua` | `Get` | Converts Tiled static layouts into layout tables |
| `scripts/map/layouts.lua` | `Layouts` | Registers general static layouts |
| `scripts/map/traps.lua` | `Layouts` / `Sandbox` | Registers trap layouts and random choices |
| `scripts/map/pointsofinterest.lua` | `Layouts` / `Sandbox` | Registers POI layouts and random choices |
| `scripts/map/protected_resources.lua` | `Layouts` / `Sandbox` | Registers protected-resource layouts and random choices |
| `scripts/map/boons.lua` | `Layouts` / `Sandbox` | Registers boon layouts and random choices |
| `scripts/map/maze_layouts.lua` | `AllLayouts` | Registers maze-room layouts |
| `scripts/worldgen_main.lua` | `AddSingleSetPeice` | Selects a layout name from a choice file |
| `scripts/map/level.lua` | `Level:ChooseSetPieces` | Assigns layout names to tasks |
| `scripts/map/storygen.lua` | `Story:InsertAdditionalSetPieces` | Writes node `countstaticlayouts` |
| `scripts/map/graphnode.lua` | `Node:ConvertGround` | Runs `object_layout.Convert` |
| `scripts/map/object_layout.lua` | `ReserveAndPlaceLayout` | Reserves space and writes `entities` |
| `scripts/gamelogic.lua` | `DoInitGame` / `PopulateWorld` | Loads the map and iterates `savedata.ents` |
| `scripts/worldentities.lua` | `AddWorldEntities` | Adds required world records before spawning |
| `scripts/mainfunctions.lua` | `SpawnSaveRecord` | Instantiates one serialized entity record |

### `0x51032111` Graph Layout Search

Search `RunForceDirected`, `ForceDirected`, and `layout = {run=` for node positioning rather than prefab placement.

### `0x51032112` Graph Layout Role

`map/layout.lua` defines the global `layout` table and computes topology-node positions.

It neither reads `map/layouts.lua` nor calls `ReserveAndPlaceLayout`.

### `0x51032211` Static Layout Search

Search `ConvertStaticLayoutToLayout` and `return { Get =` to find the Tiled-to-layout conversion path.

### `0x51032212` Static Layout Conversion

`ConvertStaticLayoutToLayout` reads tile layers and object groups.

It produces `layout.type = LAYOUT.STATIC`, `ground_types`, `ground`, and `layout.layout`.

`map/layouts.lua` registers layouts from `map/static_layouts/...`.

It loads them through `StaticLayout.Get("map/static_layouts/...")`.

`GROUND_TYPES` maps Tiled tile indexes to `WORLD_TILES`.

Ground meaning therefore also depends on registration in `tiledefs.lua` and `worldtiledefs.lua`.

### `0x51032311` Object Layout Search

Search `LayoutForDefinition`, `ConvertLayoutToEntitylist`, and `ReserveAndPlaceLayout`.

Together with `Convert` and `Place`, they form the layout-to-coordinate path.

### `0x51032312` Object Layout Placement

`LayoutForDefinition` checks `map/layouts`, `map/traps`, and `map/protected_resources` first.

It then checks `map/boons`, `map/maze_layouts`, and `map/pointsofinterest`.

`ConvertLayoutToEntitylist` expands `layout.layout`, `layout.areas`, `layout.defs`, and `layout.count`.

`ReserveAndPlaceLayout` uses `WorldSim:ReserveSpace` unless it receives an explicit position, then writes through `add_entity.fn`.

For layouts with `ground`, explicit positions call `world:SetTile`, while automatic placement passes tile data to `WorldSim:ReserveSpace`.

## `0x51033000` Flow

~~~mermaid
flowchart TD
    A["AddSingleSetPeice in worldgen_main.lua"]
    A --> B["GetRandomFromLayouts"]
    B --> C["level.set_pieces[name]"]
    C --> D["Level:ChooseSetPieces"]
    D --> E["task.set_pieces / task.random_set_pieces"]
    E --> F["Story:InsertAdditionalSetPieces"]
    F --> G["node.data.terrain_contents.countstaticlayouts"]
    G --> H["forest_map.Generate"]
    H --> I["topology_save.root:ConvertGround"]
    I --> J["Node:ConvertGround"]
    J --> K["object_layout.Convert"]
    K --> L["ReserveAndPlaceLayout"]
    L --> M["savedata.ents"]
~~~

### `0x51033111` Choice Source

`AddSingleSetPeice` calls `require(choicefile)` for the `choicefile` module, which must contain a `Sandbox` table.

`GetRandomFromLayouts` selects one layout name from a `Sandbox` area.

### `0x51033112` Area Filtering

`GetAreasForChoice` matches the selected area to task `room_bg`; `Any` and `Rare` can bypass a specific ground match.

### `0x51033211` Task Assignment

Normal selections enter `task.set_pieces`, while `required_setpieces` and random selections enter `task.random_set_pieces`.

`Level:GetTasksForLevelSetPieces` removes tasks whose `level_set_piece_blocker` is true.

`required_setpieces` and `numrandom_set_pieces` choose only from that filtered list.

`AddSingleSetPeice` uses the same filtered list when it builds `choicedata.tasks`.

The later `self.set_pieces` loop does not recheck `level_set_piece_blocker`.

### `0x51033212` Assignment Limit

For each layout name, assignment removes the chosen task from that name's candidates.

The loop stops when `count` reaches zero or no eligible task remains.

### `0x51033311` Story Node Marking

For `task.set_pieces`, `Story:InsertAdditionalSetPieces` excludes entrance, blank, and impassable nodes.

When `restrict_to` is `background`, it further limits placement to background nodes.

It then writes `task.nodes[choice].data.terrain_contents.countstaticlayouts[name] = 1`.

### `0x51033312` Random Set Pieces

For `task.random_set_pieces`, the function excludes only entrance nodes and `NODE_TYPE.Blank`.

It does not apply `restrict_to` or reject impassable tiles.

It writes the selected name to the same `countstaticlayouts` table.

### `0x51033411` Static Layout Execution

`forest_map.Generate` calls `topology_save.root:ConvertGround`.

`Node:ConvertGround` in `graphnode.lua` calls `obj_layout.Convert(self.id, k, add_fn)` for each entry.

At the node level, the source field is `self.data.terrain_contents.countstaticlayouts`.

### `0x51033412` Tag-Derived Layouts

`Node:ConvertGround` also processes the `static_layouts` in `terrain_contents_extra.static_layouts`.

`Story:GetExtrasForRoom` derives those layouts from room tags.

### `0x51033421` Entity Writer

The `add_fn` created by `Node:ConvertGround` ends at `PopulateWorld_AddEntity`.

Maze and ocean placement pass the `add_fn` defined in `forest_map.Generate`.

Both paths append prefabs to the worldgen `entities` table.

### `0x51033422` Runtime Boundary

Successful placement contributes to `savedata.ents`, but entities are spawned only when the save is loaded.

`DoInitGame` calls `PopulateWorld`, which reinjects required world records after retrofitting on the master simulation.

`PopulateWorld` then iterates `savedata.ents` and passes each record to `SpawnSaveRecord`.

`SpawnSaveRecord` calls `SpawnPrefab` and restores the record through `SetPersistData`.

## `0x51034111` `countstaticlayouts`

`Story:InsertAdditionalSetPieces` creates or updates `terrain_contents.countstaticlayouts`.

`Node:ConvertGround` executes it.

## `0x51034121` `distributeprefabs`

`Story:RunTaskSubstitution` rewrites `contents.distributeprefabs`.

`forest_map.Generate` then handles density placement through `PopulateVoronoi`.

## `0x51034211` Layout Definition Fields

`areas` expands area placeholders, and `defs` resolves abstract objects to prefab candidates.

`layout` stores static coordinates, while `count` asks `LAYOUT_FUNCTIONS[layout.type]` for positions.

## `0x51034221` Space Reservation Fields

`ground`, `ground_types`, `start_mask`, `fill_mask`, and `layout_position` affect `WorldSim:ReserveSpace`.

`SafeFromDisconnect` forwards the affected tiles to `WorldSim:MakeSafeFromDisconnect`.

## `0x51034231` Placement APIs

`Convert` finds a position inside a node by passing the node id, layout name, and `add_fn` to `ReserveAndPlaceLayout`.

`Place` handles maze, ocean, and other explicit tile coordinates.

It uses `"POSITIONED"` as the node id and supplies a position.

## `0x51034311` Source Spelling

The source names are `AddSetPeices` and `AddSingleSetPeice`; `rg "AddSetPieces"` does not find the real entry points.

## `0x51034321` Function Location

`GetRandomFromLayouts` is local to `scripts/worldgen_main.lua`, not `scripts/map/storygen.lua`.

## `0x51035100` Verification

~~~bash
rg -n "GetRandomFromLayouts|AddSingleSetPeice|ChooseSetPieces|InsertAdditionalSetPieces" \
  scripts/worldgen_main.lua \
  scripts/map/level.lua \
  scripts/map/storygen.lua

rg -n "countstaticlayouts|Node:ConvertGround|LayoutForDefinition|ReserveAndPlaceLayout" \
  scripts/map/graphnode.lua \
  scripts/map/object_layout.lua

rg -n "ConvertStaticLayoutToLayout|StaticLayout.Get|layout = \\{run" \
  scripts/map/static_layout.lua \
  scripts/map/layouts.lua \
  scripts/map/layout.lua

rg -n "Sandbox|Layouts|AllLayouts" \
  scripts/map/traps.lua \
  scripts/map/pointsofinterest.lua \
  scripts/map/protected_resources.lua \
  scripts/map/boons.lua \
  scripts/map/maze_layouts.lua

rg -n "DoInitGame|PopulateWorld|AddWorldEntities|SpawnSaveRecord|savedata\.ents" \
  scripts/gamelogic.lua \
  scripts/worldentities.lua \
  scripts/mainfunctions.lua
~~~

### `0x51035111` Minimum Trace

Start at `AddSingleSetPeice` in `scripts/worldgen_main.lua`, then follow `Level:ChooseSetPieces`.

Continue through `Story:InsertAdditionalSetPieces`, `Node:ConvertGround`, and `ReserveAndPlaceLayout`.

### `0x51035112` Checks

- `GetRandomFromLayouts` is in `worldgen_main.lua`.
- `map/layout.lua` only handles graph layout.
- `map/static_layout.lua` converts Tiled data into layout tables.
- `map/layouts.lua` registers static layouts through `StaticLayout.Get`.
- `object_layout` converts layout names into entity coordinates.
- `LayoutForDefinition` checks `layouts`, `traps`, `protected_resources`, `boons`, `maze_layouts`, and `pointsofinterest`.
- Static layouts write to `savedata.ents` during worldgen.
