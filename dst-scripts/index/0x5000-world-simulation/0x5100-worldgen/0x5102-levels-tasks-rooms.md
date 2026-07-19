# `0x51020000` Levels, Tasks, and Rooms

This page traces how preset data becomes topology nodes through `Level -> Task -> Room -> Story Graph`.

## `0x51021111` Data Layers

`Level` selects a task set, and `Task` defines room choices plus lock-and-key relationships.

`Room` defines tiles, tags, prefab distribution, and static layouts.

`Story` turns those definitions into `Graph` nodes for `WorldSim`.

## `0x51021112` File Boundary

`scripts/map/levels.lua` loads and registers presets through `AddLevel` and `AddWorldGenLevel`.

The `Level` class is defined in `scripts/map/level.lua`.

## `0x51022000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/map/levels.lua` | `AddWorldGenLevel` | Registers worldgen presets |
| `scripts/map/levels.lua` | `AddLevel` | Registers presets visible to the frontend and settings |
| `scripts/map/level.lua` | `Level` | Wraps level data and stores overrides |
| `scripts/map/level.lua` | `Level:ChooseTasks` | Selects tasks from `overrides.task_set` |
| `scripts/map/tasksets.lua` | `AddTaskSet` / `GetGenTasks` | Registers and retrieves task sets |
| `scripts/map/tasksets/forest.lua` | `AddTaskSet("default")` | Defines the default forest task set |
| `scripts/map/tasksets/caves.lua` | `AddTaskSet("cave_default")` | Defines the default cave task set |
| `scripts/map/tasks.lua` | `AddTask` | Registers task definitions |
| `scripts/map/task.lua` | `Task` | Stores task fields and normalizes locks and keys |
| `scripts/map/rooms.lua` | `AddRoom` | Registers room definitions |
| `scripts/map/storygen.lua` | `Story:GenerateNodesFromTask` | Converts task rooms into graph nodes |

### `0x51022111` Preset Registration

Search `AddWorldGenLevel` for `worldgenlist` registration and `AddLevel` for `levellist` registration.

### `0x51022112` Runtime Construction

Both `AddWorldGenLevel` and `AddLevel` call `Level(data)`.

Field behaviour is therefore defined by `map/level.lua`, not the registry alone.

### `0x51022121` Task Selection

`function Level:ChooseTasks()` asserts `self.overrides["task_set"] ~= nil`, then calls `tasksets.GetGenTasks(task_set)`.

### `0x51022122` Selected Tasks

`ChooseTasks` merges task-set data into `self`.

It then forms `self.chosen_tasks` from `self.tasks`, `self.optionaltasks`, and mod hooks.

### `0x51022131` Task-Set Registry

`tasksets.lua` (`scripts/map/tasksets.lua`) registers task collections rather than individual tasks.

Search it for `AddTaskSet`, `GetGenTasks`, and `require("map/tasksets/forest")`.

### `0x51022132` Task-Set Data

`GetGenTasks(id)` returns a deep copy from a mod task set or the built-in `taskgrouplist`.

Fields such as `tasks`, `optionaltasks`, `valid_start_tasks`, and `numoptionaltasks` come from that copy.

### `0x51022211` Task Contract

Search `scripts/map/tasks.lua` for `AddTask`, `room_choices`, `background_room`, `locks`, and `keys_given`.

These fields are primary inputs to `Story:GenerateNodesFromTask`.

### `0x51022212` Task Instances

`AddTask(name, data)` stores `Task(name, data)` in `taskdefinitions`.

`Level:EnqueueATask` later copies definitions returned by `tasks.GetTaskByName(taskname)`.

### `0x51022221` Room Contract

Search `scripts/map/rooms.lua` for `AddRoom` and `contents`; storygen copies `contents` into node `terrain_contents`.

### `0x51022222` Room Instances

`AddRoom` stores room data in `rooms[name]`.

`Story:GetRoom` calls `deepcopy(Rooms.GetRoomByName(roomname))` and then applies the `RoomPreInit` mod hook.

## `0x51023000` Flow

~~~mermaid
flowchart TD
    A["map/levels.lua AddWorldGenLevel"]
    A --> B["Level(data)"]
    B --> C["Level:ChooseTasks"]
    C --> D["tasksets.GetGenTasks(overrides.task_set)"]
    D --> E["merge task-set fields into Level"]
    E --> F["Level:EnqueueATask"]
    F --> G["tasks.GetTaskByName"]
    G --> H["Story:GenerateNodesFromTask"]
    H --> I["Story:GetRoom"]
    I --> J["Graph(task.id)"]
    J --> K["task_node:AddNode"]
    K --> L["node.data.terrain_contents"]
~~~

### `0x51023111` Required Inputs

`overrides.task_set` is mandatory, and `ChooseTasks` asserts if it is absent.

It also asserts when `tasksets.GetGenTasks(task_set)` returns `nil`.

This can indicate a preset that depends on a disabled mod.

### `0x51023112` Mod Hooks

`ChooseTasks` invokes `TaskSetPreInit`, `TaskSetPreInitAny`, `LevelPreInit`, and `LevelPreInitAny`.

The final task list is therefore not purely static preset data.

### `0x51023211` Task Copies

`EnqueueATask` uses `deepcopy(task)`.

`deepcopy` ensures `ApplyModsToTasks` and `GetOverridesForTasks` mutate only this generation's copy.

### `0x51023212` Task Fields

`room_choices` sets explicit room counts, and `background_room` supplies the background node template.

`room_bg` supplies the task graph's default ground.

`locks` and `keys_given` feed `LinkNodesByKeys` or `RestrictNodesByKey`.

`region_id` adds the task to `region_tasksets`.

`AddRegionsToMainland` later connects eligible non-`mainland` regions.

It skips the reserved `ruins_island` and `vault_island` ids.

### `0x51023311` Room Expansion

`entrance_room` may be pushed onto the room-choice stack first, followed by the requested copies from `room_choices`.

### `0x51023312` Node Fields

Each room reaches `task_node:AddNode` with `type`, `task`, `name`, `value`, and `tags`.

The node also receives `terrain_contents`, `terrain_contents_extra`, and `required_prefabs`.

### `0x51023411` Task Connections

`Story:GenerateNodesForRegion` calls `GenerateNodesFromTask` for each task.

`GenerateNodesForRegion` selects `RestrictNodesByKey` for the matching `layout_mode`.

Other modes fall back to `LinkNodesByKeys`.

### `0x51023412` Start and Loops

`_FindStartingTask` prefers an unlocked task, and `_AddPlayerStartNode` inserts the player start.

A successful `loop_percent` roll lets `SeperateStoryByBlanks` add separator nodes for a loop.

## `0x51024111` Level Fields

`GenerateNew` uses `location` as the map prefab, while `forest_map.Generate` copies `overrides` into `current_gen_params`.

`Story:GenerationPipeline` and `AddBGNodes` use `background_node_range` and `blocker_blank_room_name`.

## `0x51024121` Task Fields

`GenerateNodesFromTask` expands `room_choices`, and `Graph(task.id, {...})` receives `set_pieces` and `random_set_pieces`.

`RunTaskSubstitution` uses `substitutes` to modify `distributeprefabs`.

## `0x51024131` Room Fields

Only the `entrance_room` and `room_choices` paths invoke `contents.fn`.

`Story:GetRoom` deep-copies the room and runs `RoomPreInit`, then `contents.fn` runs before the stack push.

After the room is popped, task substitution can rewrite `contents.distributeprefabs`.

`GetExtrasForRoom` converts `tags` into extra prefabs, `static_layouts`, extra tags, or global tags.

## `0x51024211` Common Misread: `Level`

`levels.lua` registers presets; `level.lua` defines the `Level` class.

## `0x51024221` Common Misread: Spawning

Rooms only write `terrain_contents` to topology nodes.

Runtime coordinates are produced later by `PopulateVoronoi`, `object_layout`, and ocean processing in `forest_map.Generate`.

## `0x51025100` Verification

~~~bash
rg -n "AddWorldGenLevel|AddLevel|function Level:ChooseTasks|function Level:EnqueueATask" \
  scripts/map/levels.lua \
  scripts/map/level.lua

rg -n "AddTaskSet|GetGenTasks|AddTask|AddRoom|GenerateNodesFromTask|GenerateNodesForRegion" \
  scripts/map/levels.lua \
  scripts/map/level.lua \
  scripts/map/tasksets.lua \
  scripts/map/tasksets/forest.lua \
  scripts/map/tasksets/caves.lua \
  scripts/map/tasks.lua \
  scripts/map/task.lua \
  scripts/map/rooms.lua \
  scripts/map/storygen.lua
~~~

### `0x51025111` Minimum Trace

Find a preset in `map/levels.lua`, then follow its `task_set` through `ChooseTasks`.

Inspect the task's `room_choices` in `map/tasks.lua`.

Finish at `task_node:AddNode` in `map/storygen.lua`.

### `0x51025112` Checks

- `overrides.task_set` is the task-selection entry point.
- `tasksets.GetGenTasks` returns a deep copy.
- `AddTask` constructs `Task(name, data)`.
- `Story:GetRoom` deep-copies each room.
- `node.data.terrain_contents` comes from room `contents`.
- `LinkNodesByKeys` or `RestrictNodesByKey` runs after each task graph is built.
