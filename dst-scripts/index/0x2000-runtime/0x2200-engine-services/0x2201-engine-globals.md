# `0x22010000` Engine Globals

This page traces engine-injected objects and their Lua wrappers.

It focuses on `TheSim`, `TheNet`, `TheInput`, and entities.

## `0x22011111` Purpose

`TheSim` and `TheNet` are engine-injected.

`scripts/input.lua` constructs `TheInput = Input()` as the Lua input wrapper.

`mainfunctions.lua` and `networking.lua` wrap engine services for Lua callers.

## `0x22011211` Key Boundary

From `TheSim:CreateEntity()`, continue to `EntityScript(ent)` and `Ents[guid]` to reach the Lua lifecycle.

At `TheNet:GetIsServer()`, verify whether the following write requires server authority.

## `0x22012000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/main.lua` | `KnownModIndex:Load` | Load the mod index before mod-safe startup |
| `scripts/main.lua` | `require("globalvariableoverrides")` | Load global-variable overrides |
| `scripts/main.lua` | `require("standardcomponents")` | Load default component callbacks |
| `scripts/mainfunctions.lua` | `CreateEntity` | Wrap `TheSim:CreateEntity()` in `EntityScript` |
| `scripts/mainfunctions.lua` | `SavePersistentString` | Call `TheSim:SetPersistentString` through one wrapper |
| `scripts/mainfunctions.lua` | `ReplicateEntity` | Find an entity in `Ents` by GUID and start replication |
| `scripts/standardcomponents.lua` | `DefaultBurntFn` | Provide reusable default component behaviour |
| `scripts/globalvariableoverrides.lua` | intentionally blank | Keep the primary override file empty |
| `scripts/input.lua` | `TheInput` | Update input and collect candidate actions |
| `scripts/networking.lua` | `SerializeUserSession` | Wrap player-session serialization through `TheNet` |

### `0x22012111` Entity Anchor

`CreateEntity` creates the engine entity, reads its GUID, constructs `EntityScript`, and registers it in `Ents`.

### `0x22012211` Input Anchor

`TheInput` connects frontend input and HUD interaction to later component-action collection.

### `0x22012311` Network Anchor

`SerializeUserSession` calls `player:GetSaveRecord()` before passing the record to `TheNet:SerializeUserSession()`.

Player-session persistence therefore uses the same entity save structure.

### `0x22012411` Default Component Anchor

`standardcomponents.lua` defines reusable functions such as `DefaultIgniteFn`, `DefaultBurnFn`, and `DefaultBurntFn`.

Prefabs and components opt into these functions; the engine does not invoke them automatically.

## `0x22013000` Runtime Boundary

~~~mermaid
flowchart TD
    A["C++ engine services"]
    A --> B["TheSim / TheNet"]
    A --> C["input callbacks"]
    B --> D["Lua wrapper functions"]
    C --> E["input.lua constructs TheInput"]
    D --> F["gameplay and UI"]
    E --> F
~~~

### `0x22013111` `TheSim` to Entities

`TheSim:CreateEntity()` returns the low-level entity.

Components, events, tasks, and persistence belong to the `EntityScript` created around it.

### `0x22013211` `TheNet` and Authority

Check server authority before writing world state.

For example, `SaveGame` returns immediately on a client and prints a disabled message.

### `0x22013311` `TheInput` and Intent

Input produces intent and candidate actions.

World changes still require server-side action, component, or RPC handling.

## `0x22014111` `Ents`

`Ents[guid]` maps engine GUIDs to `EntityScript` objects.

`OnRemoveEntity` clears `Ents`, tasks, update queues, the Brain, and the StateGraph.

## `0x22014211` Persistence Wrapper

`SavePersistentString` is a thin wrapper around `TheSim:SetPersistentString`.

Full world saves instead use `SerializeWorldSession` and finish through `ShardGameIndex`.

## `0x22014311` Manager Cleanup

Entity removal notifies both `BrainManager` and `SGManager`.

Entity, AI, and StateGraph lifecycles therefore share the GUID removal path.

## `0x22014411` Global Overrides

The tracked primary `globalvariableoverrides.lua` contains only `-- Intentionally blank`.

Platform or environment variants exist nearby, but the main startup path requires `globalvariableoverrides`.

## `0x22015100` Verification

~~~bash
rg -n "CreateEntity|OnRemoveEntity|SavePersistentString|ReplicateEntity|TheSim|TheNet|TheInput" \
  scripts/main.lua \
  scripts/mainfunctions.lua \
  scripts/standardcomponents.lua \
  scripts/globalvariableoverrides.lua \
  scripts/input.lua \
  scripts/networking.lua
~~~

### `0x22015111` Next Read

Read `CreateEntity` and `OnRemoveEntity`.

Inspect one `TheNet:GetIsServer` branch, then confirm that `TheInput` only submits intent.
