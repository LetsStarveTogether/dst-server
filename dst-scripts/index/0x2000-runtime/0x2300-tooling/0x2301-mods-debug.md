# `0x23010000` Mods and Debugging

This page separates mod environments and post-init hooks from console and debug commands.

Debug commands are tooling entry points, not part of the normal gameplay flow.

## `0x23011111` Mod Injection

`modutil.lua` does not directly rewrite every prefab, component, or StateGraph.

Most post-init Add APIs register functions in `env.postinitfns`.

Other APIs directly update registries for actions, component actions, or tiles.

The runtime executes registered functions at call sites of `ModManager:GetPostInitFns()`.

## `0x23011211` Debug Boundary

`consolecommands.lua` defines `c_*` console entries, and `debugcommands.lua` defines many `d_*` entries.

They often call `DebugSpawn`, `GetDebugEntity`, and `SetDebugEntity`, but ordinary runtime paths do not.

## `0x23012000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/main.lua` | `KnownModIndex:Load` / `BeginStartupSequence` | Gate `ModSafeStartup` on mod-index startup |
| `scripts/modindex.lua` | `KnownModIndex = ModIndex()` | Create the global mod-index object |
| `scripts/mods.lua` | `ModWrangler:LoadMods` / `CreateEnvironment` | Select mods and create their environments |
| `scripts/mods.lua` | `ModManager:GetPostInitFns` | Retrieve registered post-init functions |
| `scripts/modutil.lua` | `InsertPostInitFunctions` / `AddPrefabPostInit` | Install APIs and register prefab hooks |
| `scripts/modutil.lua` | `AddComponentPostInit` | Register a component post-init function |
| `scripts/modutil.lua` | `AddStategraphPostInit` | Register a StateGraph post-init function |
| `scripts/debugtools.lua` | `debugstack` / `dumptable` / `instrument_userdata` | Inspect runtime data |
| `scripts/knownerrors.lua` | `known_assert` | Map a known error key to a user-visible error |
| `scripts/consolecommands.lua` | `c_spawn` | Spawn a prefab from the console |
| `scripts/debugcommands.lua` | `d_*` | Provide debug commands |
| `scripts/debughelpers.lua` | `debug.getinfo` | Support introspection helpers |

### `0x23012111` Mod Index Anchor

In `main.lua`, the `KnownModIndex:Load` callback starts `BeginStartupSequence`, whose callback reaches `ModSafeStartup`.

### `0x23012211` Post-Init Anchor

Find `env.postinitfns` to see which Add APIs register deferred functions.

Do not assume that every Add API uses this table.

### `0x23012311` Debug Anchor

Find `function c_spawn`; it calls `DebugSpawn(prefab)` and may call `SetDebugEntity(inst)`.

## `0x23013000` Mod Hook Flow

~~~mermaid
flowchart TD
    A["KnownModIndex:Load"]
    A --> B["BeginStartupSequence"]
    B --> C["ModSafeStartup"]
    C --> D["ModWrangler:LoadMods"]
    D --> E["CreateEnvironment"]
    E --> F["InsertPostInitFunctions"]
    F --> G["env.postinitfns"]
    G --> H["ModManager:GetPostInitFns"]
    H --> I["prefab / component / StateGraph hook point"]
~~~

### `0x23013111` Mod Environment

`ModWrangler:LoadMods()` selects enabled mods and calls `CreateEnvironment()` for each one.

`CreateEnvironment()` calls `modutil.InsertPostInitFunctions(env, isworldgen, isfrontend)`.

The `InsertPostInitFunctions` call receives the environment and its world-generation and frontend flags.

The available API set therefore varies between world-generation, frontend, and game contexts.

### `0x23013211` Hook Execution

`mainfunctions.lua` retrieves prefab post-init functions during prefab load and creation.

`stategraph.lua` retrieves StateGraph post-init functions during StateGraph construction.

After `entityscript.lua` constructs a component, `AddComponent` retrieves its post-init functions.

It calls `ModManager:GetPostInitFns("ComponentPostInit", name)` and runs the results in order.

### `0x23013311` Debug Execution

`c_spawn` and many `d_*` commands mutate the world and should not be used as evidence for player action paths.

`consolecommands.lua` loads during normal startup.

`debugcommands.lua` and `debugkeys.lua` load only when `CHEATS_ENABLED` is true.

## `0x23014111` `env.postinitfns`

`PrefabPostInit`, `PrefabPostInitAny`, `ComponentPostInit`, and `StategraphPostInit` use separate buckets.

World-generation, game, simulation, shader, recipe, level, task-set, task, and room hooks also have separate buckets.

Named hooks use nested tables, while Any hooks use arrays.

## `0x23014211` `KnownModIndex`

`KnownModIndex = ModIndex()` appears at the end of `modindex.lua`.

`mods.lua` uses it for mod metadata, enablement, configuration, and compatibility.

## `0x23014311` Debug Helpers and Known Errors

`debughelpers.lua` wraps `debug.getinfo`, upvalues, and entity debug strings.

`debugtools.lua` supplies `debugstack`, `debugstack_oneline`, `dumptable`, and `instrument_userdata`.

`knownerrors.lua` supplies `ERRORS` and `known_assert` for stable error keys and messages.

World-mutating entries are concentrated in `consolecommands.lua` and `debugcommands.lua`.

## `0x23015100` Verification

~~~bash
rg -n \
  -e "KnownModIndex:Load" \
  -e "BeginStartupSequence" \
  -e "ModSafeStartup" \
  -e "LoadMods" \
  -e "CreateEnvironment" \
  -e "InsertPostInitFunctions" \
  -e "GetPostInitFns" \
  scripts/main.lua \
  scripts/mods.lua \
  scripts/modutil.lua \
  scripts/modindex.lua \
  scripts/mainfunctions.lua \
  scripts/stategraph.lua \
  scripts/debugtools.lua \
  scripts/knownerrors.lua \
  scripts/consolecommands.lua \
  scripts/debugcommands.lua \
  scripts/debughelpers.lua
rg -n "AddPrefabPostInit|AddComponentPostInit|AddStategraphPostInit|c_spawn|DebugSpawn|CHEATS_ENABLED" \
  scripts/main.lua \
  scripts/entityscript.lua \
  scripts/mods.lua \
  scripts/modutil.lua \
  scripts/modindex.lua \
  scripts/mainfunctions.lua \
  scripts/stategraph.lua \
  scripts/debugtools.lua \
  scripts/knownerrors.lua \
  scripts/consolecommands.lua \
  scripts/debugcommands.lua \
  scripts/debughelpers.lua
~~~

### `0x23015111` Next Read

Read `KnownModIndex:Load` in `main.lua`.

Follow mod-environment creation in `mods.lua`, then inspect Add APIs in `modutil.lua`.

Finish at their `ModManager:GetPostInitFns` consumers.
