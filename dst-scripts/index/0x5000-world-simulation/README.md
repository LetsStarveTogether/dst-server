# `0x50000000` World Simulation

This section separates world generation from runtime world state.

## `0x50001111` Scope

Read worldgen first to see how presets become saved maps.

Then read world state to see how the loaded world evolves.

## `0x50002000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/worldgen_main.lua` | `GenerateNew` | Starts world generation |
| `scripts/map/levels.lua` | `AddWorldGenLevel` | Registers worldgen presets |
| `scripts/map/level.lua` | `Level` | Wraps a preset and selects a task set |
| `scripts/map/storygen.lua` | `Story:GenerateNodesFromTasks` | Builds the topology graph |
| `scripts/map/forest_map.lua` | `Generate` | Bakes `WorldSim` output and writes entities |
| `scripts/components/worldstate.lua` | `SetVariable` | Projects runtime events into world state |

### `0x50002111` Primary Entry

Start at `GenerateNew` to trace the dedicated worldgen path before runtime world components exist.

## `0x50003000` Data Flow

~~~mermaid
flowchart TD
    A["worldgen parameters"]
    A --> B["worldgen_main.GenerateNew"]
    B --> C["Level(level_data)"]
    C --> D["Task / Room"]
    D --> E["storygen graph"]
    E --> F["forest_map + WorldSim"]
    F --> G["savedata.map / savedata.ents"]
    G --> H["save load + world prefab assembly"]
    H --> I["runtime worldstate"]
~~~

### `0x50003111` Navigation Rule

This index covers relationships and entry points; use `0x8000-reference` for exhaustive file lists.

## `0x50004111` Pages

- [World Generation](0x5100-worldgen/README.md)
- [Worldgen Entry](0x5100-worldgen/0x5101-worldgen-main.md)
- [Levels, Tasks, and Rooms](0x5100-worldgen/0x5102-levels-tasks-rooms.md)
- [Layouts and Set Pieces](0x5100-worldgen/0x5103-layouts.md)
- [Forest Map Output](0x5100-worldgen/0x5104-forest-map-output.md)
- [World State](0x5200-world-state/README.md)
- [Weather and Seasons](0x5200-world-state/0x5201-weather-seasons.md)
- [Caves, Ocean, and Ruins](0x5200-world-state/0x5202-caves-ocean-ruins.md)

## `0x50005100` Verification

~~~bash
rg -n "GenerateNew|AddWorldGenLevel|function Level:ChooseTasks|GenerateNodesFromTasks|function Generate|SetVariable" \
  scripts/worldgen_main.lua \
  scripts/map/levels.lua \
  scripts/map/level.lua \
  scripts/map/storygen.lua \
  scripts/map/forest_map.lua \
  scripts/components/worldstate.lua
~~~

### `0x50005111` Minimum Trace

Trace `GenerateNew` through `GenerateNodesFromTasks` and `forest_map.Generate`.

Treat save loading as the boundary before runtime `worldstate` projection.
