# `0x31010000` EntityScript

`EntityScript` is the Lua wrapper for a C++ entity.
It joins components, events, watchers, the StateGraph, the Brain, tasks, and persistence.

## `0x31011111` Purpose

This page identifies where entities come from.
It shows that `EntityScript` mainly coordinates registration and lifecycle boundaries.
Gameplay rules usually live in components, prefab initialization functions, StateGraphs, or Brains.

## `0x31012000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `CreateEntity` | Create an `EntityScript` after `TheSim:CreateEntity`. |
| `scripts/entityscript.lua` | `EntityScript = Class` | Initialize component, event, task, and replica containers. |
| `scripts/entityscript.lua` | `SetStateGraph` | Load `stategraphs/<name>` and create a `StateGraphInstance`. |
| `scripts/entityscript.lua` | `SetBrain` | Store `brainfn` and start it when sleep state allows. |
| `scripts/entityscript.lua` | `GetPersistData` | Gather component and entity persistence data. |
| `scripts/entityscript.lua` | `GetWetMultiplier`, `GetIsWet` | Check entity validity before reading wetness. |

### `0x31012111` Primary Anchor

Find `CreateEntity` in `mainfunctions.lua`.
Then inspect the constructor in `entityscript.lua` to confirm which fields the runtime initializes.

## `0x31013000` Runtime Flow

~~~mermaid
flowchart TD
    A["TheSim:CreateEntity"]
    A --> B["EntityScript(ent)"]
    B --> C["Ents[GUID] = scr"]
    B --> D["components / replica"]
    B --> E["event_listeners / event_listening"]
    B --> F["worldstatewatching / pendingtasks"]
    B --> G["SetStateGraph"]
    B --> H["SetBrain"]
    D --> I["GetPersistData / SetPersistData"]
~~~

### `0x31013111` Key Boundaries

- `CreateEntity` creates the Lua wrapper and registers it in `Ents`.
- `SetStateGraph` removes an existing SG from `SGManager` before creating the requested `StateGraphInstance`.
- `SetBrain` stores `brainfn`, while `sleepstatepending`, `IsInLimbo`, and `IsAsleep` determine whether it starts.
- For server/client paths, trace server authority first and then inspect the replica or classified projection.

## `0x31014111` Runtime Containers

- `components` stores local component instances, including client components such as `playercontroller`.
- `replica._` stores replica components, while the outer `replica` table uses a metatable to validate access.
- `event_listeners` records listeners of this entity.
- `event_listening` records sources this entity listens to.
- `worldstatewatching` records world state subscriptions whose actual registry is `TheWorld.components.worldstate`.
- `pendingtasks` stores the entity's scheduled task handles and is cleared by `CancelAllPendingTasks` during `Remove`.

## `0x31014121` StateGraph and Brain

- `SetStateGraph` calls `LoadStateGraph` and creates `StateGraphInstance(sg, self)`.
- `SGManager:AddInstance(self.sg, self:IsAsleep())` registers the SG with its initial sleep state.
- `SetBrain` stores a function named `brainfn`, not a permanent Brain instance.
- Sleep, limbo, or a `StopBrain` reason prevents immediate startup.

## `0x31014131` Persistence Order

- `GetSaveRecord` stores position and skin data before calling `GetPersistData`.
- `GetPersistData` calls component `OnSave` methods, omits empty tables, and gathers references.
- `SetPersistData` handles `add_component_if_missing` before the entity `OnPreLoad`.
- Component `OnLoad` methods run before the entity `OnLoad`.
- `LoadPostPass` is a separate second phase outside the main `SetPersistData` loop.

## `0x31014141` Wetness and Entity Validity

`GetWetMultiplier` returns `0` for an invalid entity before reading tags, components, or world state.
It remains a server-only helper because its later branches read authoritative components.
`GetIsWet` returns `false` for an invalid entity and supports clients through replica state.
These guards do not make other entity methods safe after removal.

## `0x31015100` Verification

~~~bash
rg -n "function CreateEntity|EntityScript = Class|SetStateGraph|SetBrain|GetPersistData|SetPersistData" \
  scripts/entityscript.lua \
  scripts/mainfunctions.lua
rg -n "GetWetMultiplier|GetIsWet|IsValid" scripts/entityscript.lua
rg -n 'AddComponent\("playercontroller"\)' scripts/prefabs/player_common.lua
~~~

### `0x31015111` Minimal Trace

Trace an ordinary prefab from `CreateEntity`.
Then trace a creature with both an SG and a Brain.
Finish with `GetSaveRecord` to see how component data is gathered.
