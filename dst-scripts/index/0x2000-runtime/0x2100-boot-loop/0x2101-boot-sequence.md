# `0x21010000` Boot Sequence

Lua startup has three distinct stages.

`main.lua` prepares the environment, and `Start()` enters the frontend and loads `gamelogic.lua`.

`DoInitGame()` runs after world data is ready.

## `0x21011111` Purpose

This page separates startup side effects, the engine-facing `Start()`, and callbacks that reach `DoResetAction()`.

## `0x21012000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/main.lua` | `ModSafeStartup` | Clear filesystem aliases, load mods, and register global prefab data |
| `scripts/main.lua` | `KnownModIndex:Load` / `BeginStartupSequence` | Reach `ModSafeStartup` when mods are enabled |
| `scripts/mainfunctions.lua` | `Start` | Create `TheFrontEnd` and `require("gamelogic")` |
| `scripts/mainfunctions.lua` | `GlobalInit` | Load `global` and global event prefabs |
| `scripts/gamelogic.lua` | `OnFilesLoaded` / `DoResetAction` | Bridge nested load callbacks into reset routing |
| `scripts/gamelogic.lua` | `LoadSlot` | Check whether the current shard has a saved world |
| `scripts/gamelogic.lua` | `DoGenerateWorld` | Generate a world and pass it to `DoInitGame` |
| `scripts/gamelogic.lua` | `DoLoadWorld` / `DoLoadWorldFile` | Load the indexed world or a named save file |
| `scripts/gamelogic.lua` | `DoInitGame` | Validate and populate `savedata`, then run post-init work |
| `scripts/gamelogic.lua` | `ActivateWorld` | Unpause and restore the audio mix after player activation |

### `0x21012111` Primary Search

Find the `MODS_ENABLED` branch around `ModSafeStartup`.

When mods are disabled, `main.lua` calls it directly.

Otherwise, `KnownModIndex:Load` and `BeginStartupSequence` gate the call.

Then find `function Start` and note that Lua code in `main.lua` does not call it directly.

## `0x21013000` Runtime Flow

~~~mermaid
flowchart TD
    A["main.lua top level"]
    A --> B["require mainfunctions, scheduler, stategraph, brain, update"]
    A --> C{"MODS_ENABLED?"}
    C -->|no| D["ModSafeStartup"]
    C -->|yes| E["KnownModIndex:Load"]
    E --> F["BeginStartupSequence"]
    F --> D
    D --> AA["LoadPrefabFile and data registries"]
    D --> AB["GlobalInit when RUN_GLOBAL_INIT"]
    H["engine-facing Start()"] --> I["TheFrontEnd = FrontEnd()"]
    I --> J["require gamelogic"]
    J --> K["Profile and three index load callbacks"]
    K --> L["OnFilesLoaded"]
    L --> M["purchase-state callback"]
    M --> N["DoResetAction"]
    N --> O{"reset action"}
    O --> P["LoadAssets FRONTEND"]
    O --> Q["LOAD_SLOT: load or generate"]
    O --> R["LOAD_FILE: DoLoadWorldFile"]
    O --> S["TheNet:StartClient"]
    O --> Z["DO_DEMO: reset then generate"]
    Q --> T["DoLoadWorld or DoGenerateWorld"]
    Z --> T
    R --> U["LoadAssets BACKEND"]
    T --> U
    U --> V["DoInitGame"]
    V --> W["listen for playeractivated on success"]
    W --> X["OnPlayerActivated"]
    X --> Y["ActivateWorld"]
~~~

### `0x21013111` Top-Level Startup

`main.lua` requires the core modules first.

`ModSafeStartup()` then loads mods, translations, prefab registrations, data registries, and `TheGlobalInstance`.

When `RUN_GLOBAL_INIT` is true, `ModSafeStartup()` calls `GlobalInit()`.

### `0x21013121` `Start()` and Load Callbacks

`Start()` creates `TheFrontEnd` and loads `gamelogic.lua`.

At the end of `gamelogic.lua`, `Profile:Load` starts the nested load chain.

It calls `SaveGameIndex:Load`, `ShardSaveGameIndex:Load`, and `ShardGameIndex:Load` in order.

They reach `OnFilesLoaded()`.

`OnFilesLoaded()` calls `UpdateGamePurchasedState(OnUpdatePurchaseStateComplete)`.

It may first wait for an optional `Profile:Save()`.

`OnUpdatePurchaseStateComplete()` then calls `DoResetAction()`.

The load path is asynchronous, not a direct `Start -> LoadSlot` call.

### `0x21013131` World Activation

Once world data is ready, `DoInitGame()` populates the world.

If population does not produce a global error, it calls `ModManager:SimPostInit()` and `TheWorld:PostInit()`.

The `playeractivated` event later reaches `OnPlayerActivated()`.

That handler skips activation for seamless-swap targets and deactivated worlds.

Otherwise, it reaches `ActivateWorld()` directly or after a fade.

`DoInitGame()` does not call it directly.

## `0x21014111` Reset Actions

`DoResetAction()` selects a path from `RESET_ACTION` values.

Key cases include `RESET_ACTION.LOAD_FRONTEND`, `LOAD_SLOT`, `LOAD_FILE`, and `JOIN_SERVER`.

`LOAD_SLOT` calls `LoadSlot()` only when `ShardGameIndex` is non-empty.

An empty index is reset and sent directly to `DoGenerateWorld()`.

`LOAD_FILE` bypasses `LoadSlot()` and calls `DoLoadWorldFile()`.

`DO_DEMO` resets the master shard data and then calls `DoGenerateWorld()`.

Check `Settings.reset_action` before assuming that startup reaches `LoadSlot()`.

## `0x21014121` `savedata` Contract

`DoInitGame()` asserts `savedata.map`, its prefab, tiles, width, and height.

It also requires topology IDs, colours, edges, nodes, level type, overrides, and `savedata.ents`.

These assertions are the fastest anchors for the world-load data shape.

## `0x21015100` Verification

~~~bash
rg -n \
  -e "ModSafeStartup" \
  -e "function Start" \
  -e "function GlobalInit" \
  -e "local function DoResetAction" \
  -e "local function LoadSlot" \
  -e "local function DoLoadWorldFile" \
  -e "local function DoInitGame" \
  -e "local function OnFilesLoaded" \
  -e "local function OnUpdatePurchaseStateComplete" \
  -e "function UpdateGamePurchasedState" \
  -e "local function OnPlayerActivated" \
  -e "local function ActivateWorld" \
  scripts/main.lua \
  scripts/mainfunctions.lua \
  scripts/gamelogic.lua \
  scripts/upsell.lua
~~~

### `0x21015111` Next Read

Read the `ModSafeStartup()` branch in `main.lua`.

Then read `Start()` and `GlobalInit()` in `mainfunctions.lua`.

Finally, trace `Profile:Load()` through `DoResetAction()`, `LoadSlot()`, `DoInitGame()`, and `OnPlayerActivated()`.
