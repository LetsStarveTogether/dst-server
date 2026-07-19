# `0x42020000` Behaviour Tree

This page explains the three phases of `BT:Update`, node status, composite nodes, and common leaf behaviours.
A behaviour tree can create a `BufferedAction` for `locomotor` or call `combat`, `locomotor`, and entity events directly.

## `0x42021100` Purpose

A Brain's `OnStart` usually creates a root node and wraps it with `BT(self.inst, root)`.
Each Brain update runs `Visit`, `SaveStatus`, and `Step` in that order.

### `0x42021110` Status Values

`scripts/behaviourtree.lua` defines the string statuses `SUCCESS`, `FAILED`, `READY`, and `RUNNING`.

#### `0x42021111` Update Phases

- `Visit` chooses the node's status for the current tick.
- `SaveStatus` copies `status` into `lastresult`.
- `Step` resets a non-`RUNNING` node or advances its active subtree.
- `GetTreeSleepTime` returns the minimum sleep time among running nodes.

## `0x42022000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/behaviourtree.lua` | `BT:Update` | Runs `Visit()`, `SaveStatus()`, then `Step()` |
| `scripts/behaviourtree.lua` | `PriorityNode:Visit` | Selects the highest-priority successful or running child |
| `scripts/behaviourtree.lua` | `SequenceNode:Visit` | Stops at the first failed or running child |
| `scripts/behaviourtree.lua` | `ParallelNode:Visit` | Fails on any failure and succeeds when all children finish |
| `scripts/behaviourtree.lua` | `EventNode:OnEvent` | Triggers the node, wakes the Brain, and resets parent priority timing |
| `scripts/behaviours/doaction.lua` | `DoAction:Visit` | Gets a `BufferedAction`, installs callbacks, and calls `PushAction` |
| `scripts/behaviours/chaseandattack.lua` | `ChaseAndAttack:Visit` | Drives `combat` and `locomotor` directly |
| `scripts/behaviours/runaway.lua` | `RunAway:Visit` | Finds a threat, moves away, and sets a sleep time |

### `0x42022110` Base Node

`BehaviourNode` is the base class, and its default `Visit` fails so concrete nodes must override it.
`GetTreeSleepTime` recurses into `RUNNING` children and also uses the current node's own sleep time.
A `RUNNING` leaf can therefore contribute sleep time without descendants.

#### `0x42022111` Search Command

~~~bash
rg -n "BT:Update|BehaviourNode|PriorityNode|SequenceNode|ParallelNode|EventNode|WhileNode|GetTreeSleepTime" \
  scripts/behaviourtree.lua
~~~

## `0x42023000` Update Flow

~~~mermaid
flowchart TD
    A["Brain:OnUpdate"]
    A --> B["BT:Update"]
    B --> C["root:Visit"]
    C --> D{"Node status"}
    D -- "SUCCESS / FAILED" --> E["SaveStatus, then Step resets"]
    D -- "RUNNING" --> F["Keep the running subtree"]
    F --> G["Leaf Sleep or period"]
    E --> H["BT:GetSleepTime"]
    G --> H
    H --> I["BrainManager schedules the next update"]
    C --> J["DoAction / ChaseAndAttack / RunAway"]
    J --> K["locomotor / combat / BufferedAction / entity event"]
~~~

### `0x42023110` PriorityNode

After its `period` expires, `PriorityNode` visits children in order.
It stores the first `SUCCESS` or `RUNNING` child index in `self.idx`.
Before the next evaluation time, it visits only the currently running child.

#### `0x42023111` Priority Conditions

- A Brain with `period = 0` can reevaluate every update.
- `EventNode` can clear its parent `PriorityNode.lasttime` for high-priority reevaluation on the next Brain update.
- A running `PriorityNode:GetSleepTime` returns the next reevaluation interval.

### `0x42023210` Sequence, Selector, and Parallel Nodes

`SequenceNode` stops on `FAILED` or `RUNNING`, while `SelectorNode` stops on `SUCCESS` or `RUNNING`.
`ParallelNode` visits multiple children, fails when any child fails, succeeds when all finish, and otherwise remains running.

#### `0x42023211` WhileNode Structure

`WhileNode(cond, name, node)` returns a `ParallelNode`.
It contains `ConditionNode(cond, name)` and the work node instead of defining a separate class.
The condition is therefore checked on every update and interrupts the work node when it fails.

### `0x42023310` DoAction

`DoAction.getactionfn` returns a `BufferedAction` or `nil`.
For an action, `DoAction:Visit` registers success and failure callbacks.
It calls `self.inst.components.locomotor:PushAction(action, shouldrun)` and becomes `RUNNING`.
Later callbacks, timeout, or action invalidation set `SUCCESS` or `FAILED`.

#### `0x42023311` DoAction Boundary

- `DoAction` does not call `PerformBufferedAction` directly.
- The SG action state usually calls `PerformBufferedAction`.
- `DoAction` requires `locomotor` on the entity.

### `0x42023320` ChaseAndAttack

`ChaseAndAttack` is not a `BufferedAction` wrapper.
It calls `combat:ValidateTarget()`, `combat:TryAttack()`, and `locomotor:GoToPoint()`.
It also calls `locomotor:Stop()` and decides whether to succeed, fail, or keep pursuing.

#### `0x42023321` Chase Conditions

- A dead target produces `SUCCESS`.
- An invalid target, excessive distance, or pursuit timeout produces `FAILED`.
- Continued pursuit calls `self:Sleep(.125)`.

### `0x42023330` RunAway

`RunAway` finds a threat and either calls `homeseeker:GoHome(true)` or escapes through `locomotor:RunInDirection()` or `WalkInDirection()`.
The directional branch returns `SUCCESS` after reaching a safe distance.

#### `0x42023331` Escape Conditions

- No threat produces `FAILED`.
- An invalid threat stops movement and produces `FAILED`.
- Continued escape calls `self:Sleep(.25)`.

## `0x42024110` Sleep-Time Propagation

`BT:GetSleepTime` delegates to the root's `GetTreeSleepTime`, unless `BT.forceupdate` makes it return `0`.
`GetTreeSleepTime` recurses only through `RUNNING` children.
When no sleep time exists, it returns `nil` and `BrainManager` moves the Brain to `hibernaters`.

### `0x42024111` Sleep Fields

- `BehaviourNode:Sleep(t)` sets `nextupdatetime`.
- `BT:ForceUpdate()` makes the next sleep time `0`.
- A running non-`ConditionNode` leaf returns its remaining sleep time.
- `PriorityNode:GetSleepTime` uses `period` and `lasttime`.

## `0x42024210` EventNode

`EventNode` listens to an entity event from construction time.
On an event, it stores `triggered` and `data`, then calls `brain:ForceUpdate()`.
It also forces its parent `PriorityNode` to reevaluate.

### `0x42024211` Event Check

Use the `"gohome"` event in `rabbitbrain.lua`.
`prefabs/rabbit.lua` pushes `PushEvent("gohome")` to nearby rabbits.
`EventNode(self.inst, "gohome", DoAction(...))` then tries the return-home action on the next Brain update.

## `0x42025100` Verification

~~~bash
rg -n "DoAction:Visit|ChaseAndAttack:Visit|RunAway:Visit|Sleep\\(|PushAction|TryAttack|RunInDirection" \
  scripts/behaviourtree.lua \
  scripts/behaviours/doaction.lua \
  scripts/behaviours/chaseandattack.lua \
  scripts/behaviours/runaway.lua
~~~

### `0x42025110` Reading Order

Read `BT:Update`, then `PriorityNode:Visit`, then one leaf.
Check whether the leaf creates a `BufferedAction` or drives components directly.

#### `0x42025111` Minimum Trace

Use `DoAction` for the `BufferedAction` path and `ChaseAndAttack` for the direct-component path.
Together they cover the two commonly confused side-effect models.
