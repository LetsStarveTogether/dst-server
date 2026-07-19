# `0x42010000` Brain Runtime

This page follows Brain binding, behaviour-tree construction, and sleep-based `BrainManager` scheduling.
A Brain makes decisions and controls wake-up cadence, while a StateGraph handles action, animation, and event states.

## `0x42011100` Purpose

The minimum lifecycle is `EntityScript:SetBrain`, `Brain:_Start_Internal`, `BrainManager:Update`, and `BT:Update`.
First establish when the Brain is created, then follow how it pauses, sleeps, and wakes.

### `0x42011110` Brain and StateGraph Roles

`SetBrain` does not create a StateGraph, and `SetStateGraph` does not create a Brain.
Master-sim prefab code usually binds both to the same entity as independent decision and state-machine systems.

#### `0x42011111` Key Checks

- `EntityScript:SetBrain` saves `brainfn` in `scripts/entityscript.lua`.
- It creates `self.brain` when the entity can run it.
- `EntityScript:SetStateGraph` creates a `StateGraphInstance` through `LoadStateGraph`.
- `Brain:OnUpdate` in `scripts/brain.lua` only calls `DoUpdate` and `bt:Update`.
- World-state side effects continue through `behaviours/*.lua`, `locomotor`, `combat`, or an SG action handler.

## `0x42012000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/entityscript.lua` | `EntityScript:SetBrain` | Saves `brainfn` and conditionally creates a Brain |
| `scripts/entityscript.lua` | `EntityScript:RestartBrain` | Clears `_brainstopped` reasons before calling `brain:_Start_Internal()` |
| `scripts/entityscript.lua` | `_DisableBrain_Internal` | Stops and discards the Brain while the entity sleeps or is absent |
| `scripts/entityscript.lua` | `_EnableBrain_Internal` | Recreates the Brain with `brainfn()` when the entity returns |
| `scripts/brain.lua` | `Brain:_Start_Internal` | Calls `OnStart`, builds the `BT`, and registers with `BrainManager` |
| `scripts/brain.lua` | `BrainWrangler:Update` | Moves Brains among `updaters`, `tickwaiters`, and `hibernaters` |
| `scripts/brain.lua` | `Brain:ForceUpdate` | Marks the `BT` for update and returns the Brain to `updaters` |
| `scripts/brains/wilsonbrain.lua` | `WilsonBrain:OnStart` | Shows a small Brain rooted at `PriorityNode` |

### `0x42012110` Binding Entry

`SetBrain` first stores `self.brainfn = brainfn`.
When `sleepstatepending`, `IsInLimbo()`, or `IsAsleep()` is true, `SetBrain` sets `_braindisabled`.
It stores the function without constructing a Brain.
Otherwise it calls `brainfn()`, sets `self.brain.inst = self`, and enters `brain:_Start_Internal()`.

#### `0x42012111` Search Command

~~~bash
rg -n "SetBrain|RestartBrain|StopBrain|_DisableBrain_Internal|_EnableBrain_Internal|SetStateGraph" \
  scripts/entityscript.lua
~~~

## `0x42013000` Runtime Flow

~~~mermaid
flowchart TD
    A["Prefab master sim"]
    A --> B["EntityScript:SetBrain(brainfn)"]
    B --> C{"Can the entity run a Brain?"}
    C -- "No" --> D["Save brainfn and _braindisabled"]
    C -- "Yes" --> E["brainfn() creates a Brain"]
    E --> F["brain.inst = entity"]
    F --> G["Brain:_Start_Internal"]
    G --> H["Brain:OnStart builds the BT root"]
    H --> I["BrainManager:AddInstance"]
    I --> J["BrainWrangler:Update"]
    J --> K["Brain:OnUpdate"]
    K --> L["BT:Update"]
    L --> M{"BT:GetSleepTime()"}
    M -- "nil" --> N["Hibernate"]
    M -- "> GetTickTime()" --> O["Sleep in tickwaiters"]
    M -- "0 or a short interval" --> P["Remain in updaters"]
~~~

### `0x42013110` Startup

`SetBrain` stops any existing Brain before rebinding, so it replaces rather than layers behaviour trees.
`_Start_Internal` skips an already-started Brain and otherwise calls `OnStart`.

#### `0x42013111` Startup Conditions

- With `_brainstopped` set, `SetBrain` creates the Brain but does not start it.
- With `_braindisabled` set, `SetBrain` defers creation until `_EnableBrain_Internal` calls `brainfn()`.
- `Brain:Start` is a compatibility entry point whose source comment directs callers to `EntityScript:RestartBrain`.

### `0x42013210` Scheduling

`BrainManager` is the global `BrainWrangler()` instance and owns `instances`, `updaters`, `tickwaiters`, and `hibernaters`.
Each tick restores the current `tickwaiters[current_tick]` entries to `updaters`, then copies a safe update list.

#### `0x42013211` Scheduling Conditions

- A Brain runs `k:OnUpdate()` only when `k.inst.entity:IsValid()` and `not k.inst:IsAsleep()`.
- `sleep_amount == nil` sends it to `Hibernate`.
- `sleep_amount > GetTickTime()` sends it to `Sleep`.
- `sleep_amount <= GetTickTime()` leaves it in `updaters` for the next tick.

### `0x42013310` Stop, Pause, and Resume

`_Stop_Internal` calls `OnStop`, stops the `BT`, and removes the Brain from `BrainManager`.
`Pause` removes a running Brain from the scheduler without changing `stopped`.
`Resume` calls `AddInstance` when the Brain is not stopped.

#### `0x42013311` Wake-up Paths

- `Brain:ForceUpdate` calls `bt:ForceUpdate()` and `BrainManager:Wake(self)`.
- `EventNode:OnEvent` calls `self.inst.brain:ForceUpdate()` while the entity still has a Brain.
- Returning from sleep or limbo runs `_EnableBrain_Internal` and creates a new Brain instead of resuming the discarded object.

`BT:Update` runs when `BrainManager` next schedules the Brain, not inside `Brain:ForceUpdate`.

## `0x42014110` Brain Fields

`Brain = Class(...)` initializes `inst`, `currentbehaviour`, `behaviourqueue`, `events`, `thinkperiod`, `paused`, and `stopped`.
The active update path relies on `bt`, not `behaviourqueue`.

### `0x42014111` Field Defaults

- `self.stopped` starts as `true`.
- `self.paused` starts as `false`.
- A concrete Brain assigns `self.bt` in `OnStart`.

## `0x42014210` Behaviour-Tree Interface

`Brain:OnUpdate` calls optional `DoUpdate`, then calls `self.bt:Update()` when `self.bt` exists.
`PriorityNode`, `DoAction`, and other node semantics live in `behaviourtree.lua` and `behaviours/*.lua`.

### `0x42014211` Ownership Boundaries

- A Brain is not a behaviour tree.
- A Brain owns a `BT` instance.
- A `BT` owns a root node.
- A leaf can return status or call a component directly.

## `0x42015100` Verification

~~~bash
rg -n "SetBrain|RestartBrain|_Start_Internal|BrainWrangler:Update|Brain:OnUpdate|ForceUpdate" \
  scripts/entityscript.lua \
  scripts/brain.lua \
  scripts/behaviourtree.lua \
  scripts/brains/wilsonbrain.lua
~~~

### `0x42015110` Reading Order

Read `EntityScript:SetBrain`, then `Brain:_Start_Internal`, and finally `BrainWrangler:Update`.
This separates creation, startup, sleep, hibernation, and forced wake-up.

#### `0x42015111` Minimum Trace

In `scripts/brains/wilsonbrain.lua`, `OnStart` creates a `PriorityNode` and assigns `BT(self.inst, root)` to `self.bt`.
This is enough to verify the lifecycle without starting from a complex boss Brain.
