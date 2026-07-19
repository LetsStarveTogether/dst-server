# `0x43010000` AI Tracing Examples

Rabbit, Wilson, and Deerclops show complete paths across prefabs, Brains, behaviours, StateGraphs, and components.
A Brain chooses or advances behaviour, while a StateGraph responds to actions, events, movement intent, and attack state.

## `0x43011100` Purpose

An AI prefab must provide the components required by its SG and Brain before those systems use them.
`SetStateGraph` and `SetBrain` each need only their own prerequisites before the corresponding system first runs.
A Brain node may submit a `BufferedAction` through `DoAction` or drive `combat` and movement components directly.
The SG maps buffered actions, movement intent, and events to concrete states.

### `0x43011110` Examples

Rabbit covers escape, return-home, and food actions for a small creature.
Wilson shows a minimal Brain that wraps `ChaseAndAttack` in a player-control condition.
Deerclops shows a boss Brain combining `ActionNode`, `AttackWall`, `ChaseAndAttack`, `DoAction`, and `Wander`.

#### `0x43011111` Key Checks

- Prefabs call `SetStateGraph` and `SetBrain` independently.
- A `BufferedAction` from `DoAction` is normally executed by an SG action state.
- Behaviours such as `ChaseAndAttack` call `combat` and `locomotor` directly.
- A boss Brain uses the same runtime model as a simple Brain but has a larger root tree.

## `0x43012000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/prefabs/rabbit.lua` | `fn` | Adds `locomotor`, then binds `SGrabbit` and the rabbit Brain on the master sim |
| `scripts/brains/rabbitbrain.lua` | `RabbitBrain:OnStart` | Builds a `PriorityNode` root with a `.25` period |
| `scripts/stategraphs/SGrabbit.lua` | `actionhandlers` | Maps `ACTIONS.EAT` to `eat` and `ACTIONS.GOHOME` to `action` |
| `scripts/stategraphs/SGrabbit.lua` | `State "action"` | Calls `inst:PerformBufferedAction()` on entry |
| `scripts/brains/wilsonbrain.lua` | `WilsonBrain:OnStart` | Wraps `ChaseAndAttack` with a `CONTROL_PRIMARY` check |
| `scripts/prefabs/deerclops.lua` | `fn` | Binds `SGdeerclops` before the Deerclops Brain |
| `scripts/brains/deerclopsbrain.lua` | `DeerclopsBrain:OnStart` | Mixes events, combat, structure destruction, and wandering |
| `scripts/stategraphs/SGdeerclops.lua` | `ActionHandler(ACTIONS.HAMMER, "attack")` | Routes the hammer action from `DoAction(BaseDestroy)` into `attack` |

### `0x43012110` Rabbit Prefab Anchor

After `TheWorld.ismastersim`, the rabbit prefab adds server-side components.
Its source comment requires `locomotor` before the stategraph, followed by `inst:SetStateGraph("SGrabbit")` and `inst:SetBrain(brain)`.

#### `0x43012111` Rabbit Search Command

~~~bash
rg -n "locomotor|SetStateGraph|SetBrain|PushEvent\\(\"gohome\"\\)|PerformBufferedAction|ActionHandler" \
  scripts/prefabs/rabbit.lua \
  scripts/brains/rabbitbrain.lua \
  scripts/stategraphs/SGrabbit.lua
~~~

## `0x43013000` Rabbit Flow

~~~mermaid
flowchart TD
    A["prefabs/rabbit.lua master-sim fn"]
    A --> B["AddComponent(locomotor)"]
    B --> C["SetStateGraph(SGrabbit)"]
    C --> D["SetBrain(rabbitbrain)"]
    D --> E["RabbitBrain:OnStart"]
    E --> F["PriorityNode root"]
    F --> G{"Selected behaviour"}
    G -- "DoAction(EatFoodAction)" --> H["BufferedAction(ACTIONS.EAT)"]
    G -- "EventNode(gohome)" --> I["BufferedAction(ACTIONS.GOHOME)"]
    G -- "RunAway / Wander" --> J["Direct locomotor movement"]
    H --> K["SGrabbit ActionHandler EAT -> eat"]
    I --> L["SGrabbit ActionHandler GOHOME -> action"]
    K --> M["PerformBufferedAction"]
    L --> M
~~~

### `0x43013110` Rabbit Prefab Assembly

`prefabs/rabbit.lua` requires `brains/rabbitbrain`.
In the master-sim branch, it adds `locomotor` and `drownable` before binding `SGrabbit` and `RabbitBrain`.
It later adds components including `eater`, `inventoryitem`, `knownlocations`, `timer`, `health`, and `combat`.

#### `0x43013111` Assembly Checks

- `locomotor` precedes `SetStateGraph`.
- `SetBrain` follows `SetStateGraph`.
- The client pristine branch does not run a Brain.

### `0x43013120` Rabbit Brain Decisions

`RabbitBrain:OnStart` builds a `PriorityNode(..., .25)`.
Panic, electric-fence, and threat-escape branches run first.
They precede the `"gohome"` event, seasonal return-home branches, eating, and wandering.

#### `0x43013121` Behaviour Checks

- `GoHomeAction` returns `BufferedAction(inst, home, ACTIONS.GOHOME)`.
- `EatFoodAction` returns `BufferedAction(inst, target, ACTIONS.EAT)` after finding bait.
- `RunAway` and `Wander` move directly without `PerformBufferedAction`.

### `0x43013130` Rabbit StateGraph Execution

`SGrabbit.lua` maps `ACTIONS.EAT` to `eat` and `ACTIONS.GOHOME` to `action`.
`action.onenter` calls `inst:PerformBufferedAction()`, while `eat` waits for its timeout.

#### `0x43013131` StateGraph Checks

- The `locomote` event reads `locomotor:WantsToMoveForward()` and `WantsToRun()`.
- `run`, `run_loop`, and `hop` provide movement animation states.
- `trapped` calls `ClearBufferedAction()`.

### `0x43013210` Wilson Flow

`brains/wilsonbrain.lua` requires `behaviours/chaseandattack`.
Its `PriorityNode` root contains a `WhileNode` that checks `CONTROL_PRIMARY` and a fallback `ChaseAndAttack`.

#### `0x43013211` Wilson Boundary

- This file demonstrates Brain structure but not the complete player-input path.
- The input path continues through `input.lua`, `playercontroller`, `BufferedAction`, and `SGwilson`.
- `ChaseAndAttack` drives `combat` and `locomotor` directly.

### `0x43013310` Deerclops Prefab Assembly

In the master-sim branch, `prefabs/deerclops.lua` adds `locomotor` and binds `SGdeerclops`.
It then adds `knownlocations` and calls `SetBrain(brain)`.
The Brain comes from the file-level `require "brains/deerclopsbrain"`.

#### `0x43013311` Boss Assembly Checks

- `SetStateGraph("SGdeerclops")` precedes most gameplay components.
- `SetBrain(brain)` follows `knownlocations` because the Brain reads and writes known positions.
- `combat:SetRetargetFunction` and `SetKeepTargetFunction` exist before Brain startup.

### `0x43013320` Deerclops Brain Root

`DeerclopsBrain:OnStart` builds a `PriorityNode(..., 0.5)`.
`ActionNode` can push `"doicegrow"` at high priority.
Other branches use `AttackWall`, `Leash`, `FaceEntity`, and `ChaseAndAttack`.
For structure destruction, `BaseDestroy` returns `BufferedAction(inst, target, ACTIONS.HAMMER)`.
`DoAction` then sends it to `locomotor`.

#### `0x43013321` Boss Behaviour Checks

- `ActionNode` can call `PushEvent` or change a component field directly.
- `ChaseAndAttack` attempts attacks without creating a `BufferedAction`.
- `DoAction(BaseDestroy, "DestroyBase", true)` creates `ACTIONS.HAMMER`.

### `0x43013330` Deerclops StateGraph Execution

`SGdeerclops.lua` maps `ACTIONS.HAMMER` to `attack`.
The `attack` timeline checks `inst.bufferedaction.action == ACTIONS.HAMMER`.
It clears the buffered action and directly calls `Destroy(inst)` on the workable target.
Structure destruction therefore crosses Brain, `DoAction`, `locomotor`, the SG action handler, and the `workable` component.

#### `0x43013331` Boss StateGraph Checks

- `ActionHandler(ACTIONS.HAMMER, "attack")` is the structure-destruction entry point.
- The `doattack` event concerns the `combat` target and differs from `DoAction(BaseDestroy)`.
- `ACTIONS.HAMMER` uses special logic in the `attack` timeline instead of generic `PerformBufferedAction()`.

## `0x43014110` Trace Order

Find `require "brains/..."`, `SetStateGraph`, and `SetBrain` in the prefab.
Then inspect the Brain's `OnStart` and choose the next file from the selected node type.

### `0x43014111` Next-Hop Rules

- From `DoAction`, inspect its action function, `BufferedAction`, `locomotor:PushAction`, and the SG action handler.
- From `ChaseAndAttack`, inspect `combat` and `locomotor`.
- From `RunAway`, inspect threat search and `locomotor`.
- From `ActionNode`, inspect the wrapped Lua function.

## `0x43014210` Brain and StateGraph Boundary

A Brain does not call `SetStateGraph`, and an SG does not call `SetBrain`.
They cooperate through components, events, and buffered actions on the entity.

### `0x43014211` Common Misreads

- `inst.sg:HasStateTag` does not make a Brain part of the SG.
- `ActionHandler` does not make the SG responsible for selecting behaviour.
- `DoAction` does not mean the action has already executed.

## `0x43015100` Verification

~~~bash
rg -n "SetStateGraph|SetBrain|PriorityNode|DoAction|ChaseAndAttack|ActionHandler|PerformBufferedAction" \
  scripts/prefabs/rabbit.lua \
  scripts/brains/rabbitbrain.lua \
  scripts/stategraphs/SGrabbit.lua \
  scripts/brains/wilsonbrain.lua \
  scripts/prefabs/deerclops.lua \
  scripts/brains/deerclopsbrain.lua \
  scripts/stategraphs/SGdeerclops.lua
~~~

### `0x43015110` Reading Order

Start with rabbit because its short files cover `DoAction`, `RunAway`, `Wander`, and an SG action handler.
Use Wilson to confirm the minimal Brain shape, then Deerclops to extend the same model to boss behaviour.

#### `0x43015111` Minimum Trace

Rabbit's `EatFoodAction` creates `BufferedAction(ACTIONS.EAT)`.
`DoAction` sends it through `locomotor`, then the `SGrabbit` `eat` state calls `PerformBufferedAction()`.
