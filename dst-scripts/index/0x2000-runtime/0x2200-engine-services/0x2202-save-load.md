# `0x22020000` Save and Load

World saving moves from world and entity snapshots through component persistence to shard-index bookkeeping.

World loading moves from `ShardGameIndex` through save upgrades, world initialization, and entity restoration.

## `0x22021111` Save Scope

`SaveGame` scans `Ents` but saves only valid, persistent, unparented prefab entities with a `Transform`.

Each entity emits a record through `GetSaveRecord`, and each component can add persistent data through `OnSave`.

## `0x22021211` Load Boundary

`gamelogic.lua` decides whether to load or generate a world.

Existing world data comes from `ShardGameIndex:GetSaveData()` or `ShardGameIndex:GetSaveDataFile()`.

## `0x22022000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `SaveGame` | Server-side world-save entry point |
| `scripts/mainfunctions.lua` | `SerializeWorldSession` | Send chunked world data to `TheNet` |
| `scripts/networking.lua` | `SerializeUserSession` | Save a player session and optional `player_classified` |
| `scripts/entityscript.lua` | `GetSaveRecord` | Build one entity save record |
| `scripts/entityscript.lua` | `GetPersistData` | Collect component `OnSave` data |
| `scripts/entityscript.lua` | `SetPersistData` | Restore data through component `OnLoad` |
| `scripts/gamelogic.lua` | `DoLoadWorld` | Read world data from the shard index and initialize the game |
| `scripts/saveindex.lua` | `SaveIndex:Save` | Retained index entry that only calls its callback |
| `scripts/shardindex.lua` | `ShardIndex:Save` | Save world, server, and session indexes for a shard |
| `scripts/shardsaveindex.lua` | `ShardSaveIndex:GetShardIndex` | Cache shard indexes by slot |

### `0x22022111` Save Anchor

Find `SaveGame`, then inspect `TheNet:StartWorldSave()` and `TheNet:EndWorldSave()` as the save boundary.

### `0x22022211` Entity Anchor

`GetSaveRecord` writes position, platform, and prefab data.

`GetPersistData` merges each component's `OnSave` result into `data[k]`.

### `0x22022311` Load Anchor

`DoLoadWorld` calls `ShardGameIndex:GetSaveData(onload)`.

The `onload` callback runs `UpgradeSaveFile`, `LoadAssets`, and `DoInitGame`.

## `0x22023000` Save Flow

~~~mermaid
flowchart TD
    A["SaveGame"]
    A --> B["filter Ents"]
    B --> C["EntityScript:GetSaveRecord"]
    C --> D["EntityScript:GetPersistData"]
    D --> E["DataDumper per save section"]
    E --> F["SerializeWorldSession"]
    F --> G["ShardGameIndex:Save"]
    G --> H["ShardGameIndex:WriteTimeFile"]
~~~

### `0x22023111` World Snapshot

`SaveGame` includes encoded map data, roads, topology, and world components.

It also includes `world_network` and optional `shard_network` data.

For each referenced GUID that was saved, it writes `id` into that entity's record.

Unsaved references are logged instead.

### `0x22023211` Player Session

Player-session saving calls `player:GetSaveRecord()` and writes the character prefab into server metadata.

When `player_classified` exists, its entity is passed to `TheNet:SerializeUserSession()`.

### `0x22023311` World Load

Loading does not reverse directly out of `SaveGame`.

`LoadSlot` in `gamelogic.lua` asks `ShardGameIndex` for the save and eventually calls `DoInitGame`.

## `0x22024111` Component Save Data

A non-empty component table becomes `data[component_name]`.

Returned references are appended to the entity reference list.

The entity's own `OnSave` can add data and references after its components.

## `0x22024211` Component Load Data

`add_component_if_missing` lets the load path restore absent components.

`OnPreLoad` runs before component `OnLoad`, and `LoadPostPass` restores references in a second phase.

For example, `prefabs/gravestone.lua` uses `onloadpostpass` for mound data.

It passes `savedata.mounddata.data` to `inst.mound:LoadPostPass(...)`.

Nested restoration therefore requires the owner prefab's post-pass logic, not only the child entity's `OnLoad`.

## `0x22024311` Save Indexes

The load path creates `ShardGameIndex = ShardIndex()` in `gamelogic.lua`.

`ShardIndex:Save` writes `shardindex` through `TheSim:SetPersistentStringInClusterSlot` or `TheSim:SetPersistentString`.

`SaveIndex` remains separate from the shard index.

`SaveIndex:Save` only calls its callback and does not write an index file.

`SerializeWorldSession` writes the world body first.

World-generation overrides, the shard index, and the time file are updated afterward.

## `0x22025100` Verification

~~~bash
rg -n \
  -e "SaveGame" \
  -e "GetSaveRecord" \
  -e "GetPersistData" \
  -e "SetPersistData" \
  -e "LoadPostPass" \
  -e "DoLoadWorld" \
  -e "GetSaveData" \
  -e "ShardIndex:Save" \
  -e "SaveIndex:Save" \
  scripts/mainfunctions.lua \
  scripts/entityscript.lua \
  scripts/prefabs/gravestone.lua \
  scripts/gamelogic.lua \
  scripts/saveindex.lua \
  scripts/shardindex.lua \
  scripts/shardsaveindex.lua \
  scripts/networking.lua
~~~

### `0x22025111` Next Read

Trace the entity loop in `SaveGame`.

Inspect component `OnSave` calls in `EntityScript:GetPersistData`.

Then follow `DoLoadWorld` from `ShardGameIndex` to `DoInitGame`.
