# `0x10030000` Reading Workflows

Start with runtime, then entities, actions, AI, and world generation; use the reference section last.

## `0x10031111` Purpose

Follow one player action or one world-generation run instead of starting with the large Prefab inventory.

Each path should end at a function call, table structure, or state-machine entry point.

## `0x10032000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/input.lua` | `TheInput = Input()` | Exposes engine input to Lua |
| `scripts/components/playercontroller.lua` | `Activate` / `OnUpdate` / `DoAction` | Registers controls, updates candidates, and routes a chosen action |
| `scripts/components/playeractionpicker.lua` | `DoGetMouseActions` | Builds mouse-action candidates |
| `scripts/componentactions.lua` | `EntityScript:CollectActions` | Dispatches component action collectors |
| `scripts/components/locomotor.lua` | `PushAction` | Moves into range or pushes the action immediately |
| `scripts/entityscript.lua` | `PushBufferedAction` / `PerformBufferedAction` | Starts and performs buffered actions |
| `scripts/stategraph.lua` | `StateGraphInstance:StartAction` | Selects an action state |
| `scripts/bufferedaction.lua` | `BufferedAction:Do` | Calls the selected action function |
| `scripts/gamelogic.lua` | `DoGenerateWorld` | Opens world generation from the runtime |
| `scripts/screens/worldgenscreen.lua` | `WorldGenScreen` | Crosses into the native world-generation worker |
| `scripts/worldgen_main.lua` | `GenerateNew` | Runs the worker-side generation path |
| `scripts/map/forest_map.lua` | `Generate` | Builds the map and invokes story generation |
| `scripts/map/storygen.lua` | `BuildStory` / `GenerationPipeline` | Builds and processes the story graph |

### `0x10032111` First Checkpoint

Start by finding `TheInput = Input()` in `scripts/input.lua`.

Then find the input handler registered by `PlayerController:Activate`.

`PlayerController:OnUpdate` calls `DoGetMouseActions` to refresh mouse-action candidates.

Do not imply that `TheInput` calls `CollectActions` directly; the controller and action picker sit between them.

## `0x10033000` Runtime and World-Generation Flow

~~~mermaid
flowchart TD
    A["input.lua<br/>TheInput"] --> B["playercontroller.lua<br/>Activate / OnUpdate"]
    B --> C["playeractionpicker.lua<br/>DoGetMouseActions"]
    C --> D["componentactions.lua<br/>EntityScript:CollectActions"]
    D --> E["BufferedAction candidate"]
    E --> F["playercontroller.lua<br/>DoAction"]
    F --> G["locomotor.lua<br/>PushAction"]
    G --> H["entityscript.lua<br/>PushBufferedAction"]
    H --> I["stategraph.lua<br/>StartAction"]
    I -->|action state| J["entityscript.lua<br/>PerformBufferedAction"]
    J --> K["bufferedaction.lua<br/>Do"]
    K --> L["Action.fn"]
    H -. instant action .-> K
~~~

~~~mermaid
flowchart TD
    A["gamelogic.lua<br/>DoGenerateWorld"] --> B["screens/worldgenscreen.lua<br/>WorldGenScreen"]
    B --> C["TheSim:GenerateNewWorld"]
    C -. native worker boundary .-> D["worldgen_main.lua<br/>GenerateNew"]
    D --> E["map/level.lua<br/>Level:ChooseTasks"]
    E --> F["map/forest_map.lua<br/>Generate"]
    F --> G["map/storygen.lua<br/>BuildStory"]
    G --> H["Story:GenerationPipeline"]
~~~

### `0x10033111` Execution Boundaries

- `input.lua` abstracts input but does not directly change world state.
- The action graph follows the master-simulation, non-instant branch; clients also use prediction and RPC paths.
- `BufferedAction:Do` enters the action side effect through `self.action.fn(self)`.
- `WorldGenScreen` calls `TheSim:GenerateNewWorld`, which crosses the native worker boundary.
- `worldgen_main.lua` runs in a separate generation context, not the normal game-frame loop.

## `0x10034111` Recommended Paths

- For actions, trace one concrete control through the controller and action picker instead of starting in `actions.lua`.
- For AI, start at `BrainWrangler:Update`, then open one concrete brain and behaviour tree.
- Follow a chosen action into `StateGraphInstance:StartAction`.
- For world generation, follow the worker path in the order shown above; `Level:ChooseTasks` runs before `forest_map.Generate`.
- For networking, inspect the server-authoritative object before `entityreplica.lua` and the classified prefab.

## `0x10035100` Verification

Run these commands from `dst-scripts`.

~~~bash
rg -n "TheInput = Input|function PlayerController:(Activate|OnUpdate|DoAction)\(" \
  scripts/input.lua \
  scripts/components/playercontroller.lua
rg -n "function PlayerActionPicker:DoGetMouseActions|function EntityScript:CollectActions" \
  scripts/components/playeractionpicker.lua \
  scripts/componentactions.lua
rg -n "PushAction|PushBufferedAction|StartAction|PerformBufferedAction|function BufferedAction:Do" \
  scripts/components/locomotor.lua \
  scripts/entityscript.lua \
  scripts/stategraph.lua \
  scripts/bufferedaction.lua
rg -n "DoGenerateWorld|GenerateNewWorld|GenerateNew|ChooseTasks|forest_map.Generate|BuildStory|GenerationPipeline" \
  scripts/gamelogic.lua \
  scripts/screens/worldgenscreen.lua \
  scripts/worldgen_main.lua \
  scripts/map/level.lua \
  scripts/map/forest_map.lua \
  scripts/map/storygen.lua
~~~

### `0x10035111` Next Step

Trace one successful action and one successful world-generation run.

When a search term appears in several files, identify the relevant execution context on the page.
