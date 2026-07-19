# `0x41020000` CommonStates

`scripts/stategraphs/commonstates.lua` appends reusable definitions to a concrete graph's `states` table.
It is a state factory, not a runtime scheduler.

## `0x41021111` Purpose

CommonStates keeps creature graphs short by supplying shared states such as sleep, frozen, and corpse states.
For example, `SGrabbit.lua` defines `idle`, `eat`, `hop`, and `run` locally, then appends shared states.

## `0x41021121` Boundary

Most CommonStates helpers only mutate the supplied `states` table.
Event entry points often come from `CommonHandlers.On*`, while transitions still use `inst.sg:GoToState(...)`.

## `0x41022000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/stategraphs/commonstates.lua` | `CommonStates.AddIdle` | Adds idle and its looping timeout |
| `scripts/stategraphs/commonstates.lua` | `CommonStates.AddRunStates` | Adds `run_start`, `run`, and `run_stop` |
| `scripts/stategraphs/commonstates.lua` | `CommonStates.AddSleepStates` | Adds `sleep`, `sleeping`, and `wake` |
| `scripts/stategraphs/commonstates.lua` | `CommonStates.AddFrozenStates` | Adds `frozen` and `thaw` |
| `scripts/stategraphs/commonstates.lua` | `CommonStates.AddCombatStates` | Adds `hit`, `attack`, and `death` |
| `scripts/stategraphs/commonstates.lua` | `CommonStates.AddInitState` | Adds the default `init` entry state |
| `scripts/stategraphs/SGrabbit.lua` | `CommonStates.Add*` | Shows the helpers in a small creature graph |

### `0x41022111` Helper Anchors

Search for `CommonStates.AddRunStates` and `CommonStates.AddCombatStates` to inspect the common movement and combat patterns.

### `0x41022121` Consumer Anchor

Search `scripts/stategraphs/SGrabbit.lua` for `CommonStates.AddSleepStates` and `AddFrozenStates`.
Also find `AddInitState` to see all helpers append to the same `states` table.

## `0x41023000` Construction Flow

~~~mermaid
flowchart TD
    A["Concrete SG creates a states table"]
    A --> B["Define graph-specific State objects"]
    B --> C["Call CommonStates.Add* helpers"]
    C --> D["table.insert(states, State{...})"]
    D --> E["return StateGraph(name, states, events, defaultstate, actionhandlers)"]
    E --> F["StateGraph constructor indexes states"]
    F --> G["GoToState resolves state.name at runtime"]
~~~

### `0x41023111` Definition Phase

CommonStates never creates a `StateGraphInstance`.
It appends states before `StateGraph(...)` runs.
The constructor indexes them by `v.name`, which selects the final value for duplicate names.

### `0x41023211` Event Phase

`CommonHandlers.OnSleep()` and `OnFreeze()` return `EventHandler` objects.
The concrete SG puts them in its global `events` table, where `StateGraphInstance:HandleEvent` can reach them as fallbacks.

### `0x41023311` Timed States

`AddIdle` uses the current animation length as its timeout when it does not push an animation.
`AddSimpleActionState` defaults to `TimeEvent(time, performbufferedaction)`.

## `0x41024111` Movement States

`CommonStates.AddRunStates` adds `run_start`, `run`, and `run_stop`.
`animover` moves `run_start` into `run`.
Its `onenter` calls `locomotor:RunForward()` and sets a timeout from the animation length.
`run_stop` stops movement and returns to idle after `animqueueover`.

## `0x41024211` Combat States

`CommonStates.AddCombatStates` adds `hit`, `attack`, and `death`.
`attack.onenter` calls `components.combat:StartAttack()`.
It saves the target in `inst.sg.statemem.target` for the timeline's attack.

## `0x41024311` Entry State

`CommonStates.AddInitState` adds `init`.
It enters `corpse_idle` when `inst.is_corpse` is set and otherwise enters the default state.

## `0x41025100` Verification

~~~bash
rg -n "CommonStates.AddRunStates|CommonStates.AddCombatStates|CommonStates.AddInitState|CommonHandlers.On" \
  scripts/stategraphs/commonstates.lua \
  scripts/stategraphs/SGrabbit.lua
~~~

### `0x41025111` Minimum Trace

Read the CommonStates calls at the end of `SGrabbit.lua`, then open the matching helpers.
`return StateGraph("rabbit", states, events, "init", actionhandlers)` consumes their state names.

### `0x41025112` Sample Check

Trace rabbit's `OnFreeze()` to `CommonHandlers.OnFreeze()`, then confirm that `AddFrozenStates` adds `frozen` and `thaw`.
