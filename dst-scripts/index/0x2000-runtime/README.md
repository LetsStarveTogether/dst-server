# `0x20000000` Runtime

Read the Lua runtime in execution order.

Start with `main.lua`, continue through `Start()`, and finish with `update.lua` and `scheduler.lua`.

## `0x20001111` Scope

This section documents Lua-visible entry points, callbacks, state, and scheduling boundaries.

Engine callbacks are boundaries into Lua, not direct calls between Lua files.

## `0x20002000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/main.lua` | `ModSafeStartup` | Load mods, register resources, and create global entities |
| `scripts/mainfunctions.lua` | `Start` | Create `TheFrontEnd` and `require("gamelogic")` |
| `scripts/mainfunctions.lua` | `GlobalInit` | Load `global` and global event prefabs |
| `scripts/gamelogic.lua` | `DoResetAction` | Route `Settings.reset_action` to its next path |
| `scripts/gamelogic.lua` | `DoInitGame` | Populate the world from `savedata` and finish loading |
| `scripts/update.lua` | `Update` | Run Lua updates for an unpaused game tick |
| `scripts/update.lua` | `StaticUpdate` | Run static ticks, paused static components, and the static scheduler |
| `scripts/scheduler.lua` | `RunScheduler` | Drive normal coroutines and timed callbacks |
| `scripts/scheduler.lua` | `RunStaticScheduler` | Drive the static scheduler |

### `0x20002111` Primary Entries

Use `ModSafeStartup` for startup side effects.

Use `Start` for the `gamelogic.lua` boundary and `Update` for per-tick Lua order.

## `0x20003000` Runtime Map

~~~mermaid
flowchart TD
    A["engine loads main.lua"]
    A --> B["require core modules"]
    B --> C["ModSafeStartup"]
    C --> D["optional GlobalInit"]
    A --> E["engine calls Start"]
    E --> F["require gamelogic"]
    F --> G["Profile and index load callbacks"]
    G --> H["DoResetAction"]
    H --> I["load slot, frontend, or client join"]
    I --> J["DoInitGame when world data is ready"]
    A --> K["engine tick callbacks"]
    K --> L["update.lua: Update or StaticUpdate"]
    L --> M["scheduler, components, SGManager, BrainManager"]
~~~

### `0x20003111` Boundaries

`ModSafeStartup` calls `GlobalInit` only when `RUN_GLOBAL_INIT` is true.

`Start` does not call `GlobalInit`; it creates the frontend and loads `gamelogic.lua`.

`RunScheduler` does not call `SGManager:Update` or `BrainManager:Update`; `Update` invokes these phases separately.

## `0x20004111` Pages

- [Boot and Main Loop](0x2100-boot-loop/README.md)
- [Boot Sequence](0x2100-boot-loop/0x2101-boot-sequence.md)
- [Main Loop](0x2100-boot-loop/0x2102-main-loop.md)
- [Scheduler](0x2100-boot-loop/0x2103-scheduler.md)
- [Engine Services](0x2200-engine-services/README.md)
- [Engine Globals](0x2200-engine-services/0x2201-engine-globals.md)
- [Save and Load](0x2200-engine-services/0x2202-save-load.md)
- [Network Runtime](0x2200-engine-services/0x2203-network-runtime.md)
- [Runtime Foundations](0x2200-engine-services/0x2204-runtime-foundations.md)
- [World Settings Runtime](0x2200-engine-services/0x2205-world-settings-runtime.md)
- [Tooling and Debugging](0x2300-tooling/README.md)
- [Mods and Debugging](0x2300-tooling/0x2301-mods-debug.md)
- [Platform Tools](0x2300-tooling/0x2302-platform-tools.md)

## `0x20005100` Verification

~~~bash
rg -n "ModSafeStartup|function Start|function GlobalInit|local function DoResetAction|function Update|RunScheduler" \
  scripts/main.lua \
  scripts/mainfunctions.lua \
  scripts/gamelogic.lua \
  scripts/update.lua \
  scripts/scheduler.lua
~~~

### `0x20005111` Next Read

Confirm top-level requires and `ModSafeStartup()` side effects in `main.lua`.

Follow the profile and index callbacks in `gamelogic.lua`.

Then verify scheduler, component, StateGraph, and Brain order in `update.lua`.
