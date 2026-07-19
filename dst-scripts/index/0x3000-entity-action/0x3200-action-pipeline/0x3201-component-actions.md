# `0x32010000` ComponentActions

This page follows how action candidates are collected.
Candidate collection is distinct from `ACTIONS.*.fn` execution.

## `0x32011111` Purpose

Trace left- and right-click candidates from `PlayerActionPicker` to `EntityScript:CollectActions`.
Include scene, item, point, and equipped-item paths.

## `0x32011211` Execution Boundary

`componentactions.lua` appends `ACTIONS.*` candidates to an `actions` array.
`components/playeractionpicker.lua` sorts candidates and wraps them as `BufferedAction` objects.
Side effects occur later through `bufferedaction.lua` and `actions.lua`.

## `0x32012000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/components/playeractionpicker.lua` | `GetLeftClickActions` | Dispatch left-click candidate collection. |
| `scripts/components/playeractionpicker.lua` | `GetRightClickActions` | Dispatch right-click candidate collection. |
| `scripts/components/playeractionpicker.lua` | `GetGolfAimActions` | Return stop or charge actions while golf aiming. |
| `scripts/components/playeractionpicker.lua` | `GetSceneActions` | Collect `SCENE` actions. |
| `scripts/components/playeractionpicker.lua` | `GetUseItemActions` | Collect `USEITEM` actions. |
| `scripts/components/playeractionpicker.lua` | `GetPointActions` | Collect `POINT` actions. |
| `scripts/components/playeractionpicker.lua` | `GetEquippedItemActions` | Collect `EQUIPPED` actions. |
| `scripts/components/playeractionpicker.lua` | `GetInventoryActions` | Collect `INVENTORY` actions. |
| `scripts/componentactions.lua` | `COMPONENT_ACTIONS` | Register built-in component action collectors. |
| `scripts/componentactions.lua` | `AddComponentAction` | Register mod component action collectors. |
| `scripts/componentactions.lua` | `EntityScript:CollectActions` | Call matching component collectors. |
| `scripts/componentactions.lua` | `EntityScript:IsActionValid` | Check candidate visibility through `ISVALID`. |
| `scripts/actions.lua` | `ACTIONS` | Define the actions referenced by candidates. |

### `0x32012111` PlayerActionPicker Anchor

Start with `GetLeftClickActions` and `GetRightClickActions`.
They choose among `SCENE`, `USEITEM`, `POINT`, `EQUIPPED`, and `INVENTORY`.

### `0x32012121` ComponentActions Anchor

Then inspect `COMPONENT_ACTIONS` and `EntityScript:CollectActions`.
For mod actions, also inspect `AddComponentAction`, `modactioncomponents`, and `CheckModComponentActions`.

## `0x32013000` Collection Flow

~~~mermaid
flowchart TD
    A["PlayerController updates the mouse target"]
    A --> B["PlayerActionPicker:DoGetMouseActions"]
    B --> C{"Left or right click"}
    C --> D["GetLeftClickActions"]
    C --> E["GetRightClickActions"]
    D --> F{"Choose action type"}
    E --> F
    F --> G["Non-component candidates"]
    F --> H["SCENE / USEITEM / POINT / EQUIPPED / INVENTORY"]
    H --> I["EntityScript:CollectActions"]
    I --> J["COMPONENT_ACTIONS[actiontype][component]"]
    J --> K["Collector appends ACTIONS.*"]
    G --> L["override / steering / cannon / golf / inherent / point special"]
    K --> M["SortActionList wraps BufferedAction candidates"]
    L --> M
~~~

### `0x32013111` Mouse Target Collection

`DoGetMouseActions` handles HUD blocking, AOE reticules, line-of-sight filtering, and the mouse target.
`PlayerController` caches its results as `LMBaction` and `RMBaction`.

### `0x32013121` Left Click

Left-click candidates include steering, boat cannon aiming, golf aiming, the active item, and forced checks or attacks.
Other sources include equipped, scene, and point actions.
On walkable ground without a target, the picker tries equipped `POINT` actions and `pointspecialactionsfn`.

### `0x32013131` Right Click

Right click first handles `disable_right_click` and `rightclickoverride`.
It then checks steering, boat cannon aiming, golf aiming, container widgets, active items, equipped actions, and scene actions.
It then handles walkable peripheral targets and AOE or reticule point actions.
Right click is not a separate action type; collectors receive `right = true`.

### `0x32013211` Component Collection

`CollectActions(actiontype, ...)` first selects `COMPONENT_ACTIONS[actiontype]`.
For each `self.actioncomponents` entry, it resolves a name through `ACTION_COMPONENT_NAMES` and invokes its collector.
The mod branch iterates `self.modactioncomponents` and reads the same action type from `MOD_COMPONENT_ACTIONS`.

### `0x32013221` Other Candidate Sources

Not every candidate comes from `COMPONENT_ACTIONS`.
`PlayerActionPicker` can add steering and boat cannon actions directly.
It also adds forced `LOOKAT`, forced `ATTACK`, `DROP`, and `WALKTO` fallbacks.
It can also add `inherentsceneaction`, `inherentscenealtaction`, `pointspecialactionsfn`, and `doubleclickactionsfn`.
`componentactions.lua` is one candidate source, not the entire system.

## `0x32014111` `COMPONENT_ACTIONS`

The first key is the action type, and the second is the component name whose value is a collector.

| Action type | Collector signature | Source |
| --- | --- | --- |
| `SCENE` | `fn(inst, doer, actions, right)` | `COMPONENT_ACTIONS.SCENE` in `scripts/componentactions.lua`. |
| `USEITEM` | `fn(inst, doer, target, actions, right)` | `COMPONENT_ACTIONS.USEITEM` |
| `POINT` | `fn(inst, doer, pos, actions, right, target)` | `COMPONENT_ACTIONS.POINT` |
| `EQUIPPED` | `fn(inst, doer, target, actions, right)` | `COMPONENT_ACTIONS.EQUIPPED` |
| `INVENTORY` | `fn(inst, doer, actions, right)` | `COMPONENT_ACTIONS.INVENTORY` in `scripts/componentactions.lua`. |
| `ISVALID` | `fn(inst, action, right)` | `COMPONENT_ACTIONS.ISVALID` in `scripts/componentactions.lua`. |

## `0x32014211` Component Name Mapping

Entities store action component IDs.
`CollectActions` resolves each ID with `ACTION_COMPONENT_NAMES[v]`.
`EntityScript:AddComponent(name)` writes the set through `RegisterComponentActions(name)`.
`EntityScript:RemoveComponent(name)` clears it through `UnregisterComponentActions(name)`.
Network entities synchronize these IDs through `actionreplica.actioncomponents` and `actionreplica.modactioncomponents`.

## `0x32014311` Candidate Visibility

`EntityScript:IsActionValid(action, right)` requires right-click input when `action.rmb` is truthy.
The `Action` constructor normalizes `data.rmb = false` to `nil`.
The function then runs validators from `COMPONENT_ACTIONS.ISVALID`.
At least one built-in or mod validator must return `true`; otherwise the default is `false`.
This checks whether an action can appear as a candidate, not the execution-time rules in `BufferedAction:IsValid()`.

## `0x32014411` Sorting, Filtering, and Wrapping

After collectors append `ACTIONS.*`, `PlayerActionPicker` calls `SortActionList`.
It sorts by descending `Action.priority`.
It then applies the active `actionfilter` from the highest-priority `actionfilterstack` entry.
Only then does it wrap each candidate as a `BufferedAction`.
Entity targets populate `target`, while point or `Vector3` targets populate `pos`.
Some candidates contain only the doer and action.
Area actions such as `CASTAOE` also receive distance constraints during wrapping.

## `0x32014511` Mod Registration and Replication

`AddComponentAction` writes collectors to `MOD_COMPONENT_ACTIONS`.
It also maintains `MOD_ACTION_COMPONENT_NAMES` and `MOD_ACTION_COMPONENT_IDS`.
`RegisterComponentActions(name)` adds built-in or mod action component IDs when an entity gains a component.
`UnregisterComponentActions(name)` removes those IDs with the component.
`actionreplica.actioncomponents` and `actionreplica.modactioncomponents` carry the network state.

## `0x32014611` Client-Requested Context

The server does not trust a remote `BufferedAction` directly.
`PlayerController:OnRemoteLeftClick` and `OnRemoteRightClick` set `CLIENT_REQUESTED_ACTION`.
They derive it from the action code and mod name.
They then rerun `DoGetMouseActions(position, target)` and clear the context.
Collectors use this context for rowing failures and plant registry research.
They also use it to choose deployment actions for different point targets.
When client and server disagree, inspect `actions.lua`, `playercontroller.lua`, and `componentactions.lua`.

## `0x32014711` Carnival Golf Example

`EQUIPPED.golfclub` adds `GOLF_START_AIMING` for a right-clicked, unoccupied `golfable` target.
`GetGolfAimActions` returns `GOLF_STOP_AIMING` for a right click while aiming.
It returns `GOLF_START_CHARGING` for a left click when an aimed target exists.
It suppresses ordinary candidates while charging, so these actions do not depend only on the component collector.
`USEITEM.terraformer` adds `TERRAFORM_REMOVE` for a target tagged `terraformerremoveable`.
Execution returns to `actions.lua`.
It calls `golfclub:StartAiming`, `golfclub_reticule:StartCharging`, or `terraformerremoveable:TryToRemove`.

## `0x32014811` Pet and Open Crafting Boundaries

`COMPONENT_ACTIONS.SCENE.crittertraits` handles the `ACTIONS.PET` candidate.

It adds the action only on the non-right-click branch when the target follows the doer.

The target must also lack a replica container.
The `nopet` tag does not filter `ACTIONS.PET` candidates.
`ACTIONS.OPEN_CRAFTING.fn` requires a `builder` and passes a prototyper unless the target has `hideprototyperaction`.
Temporary crafting entries such as a Carnival golf tee need both stages.
Candidates come from `componentactions.lua`, while execution guards remain in `actions.lua`.

## `0x32015100` Verification

~~~bash
rg -n "DoGetMouseActions|GetLeftClickActions|GetRightClickActions|GetGolfAimActions|GetSceneActions" \
  scripts/components/playeractionpicker.lua
rg -n "GetUseItemActions|GetPointActions|GetEquippedItemActions|GetInventoryActions" \
  scripts/components/playeractionpicker.lua
rg -n "COMPONENT_ACTIONS|AddComponentAction|EntityScript:CollectActions" \
  scripts/componentactions.lua \
  scripts/entityscript.lua
rg -n "EntityScript:IsActionValid|RegisterComponentActions|modactioncomponents|CLIENT_REQUESTED_ACTION" \
  scripts/componentactions.lua \
  scripts/entityscript.lua
rg -n "ACTIONS =|Action\\(|\\.rmb|\\.validfn" scripts/actions.lua
rg -n "SortActionList|actionfilter|BufferedAction\\(" scripts/components/playeractionpicker.lua
rg -n "GOLF_START_AIMING|GOLF_START_CHARGING|TERRAFORM_REMOVE|golfclub|terraformerremoveable" \
  scripts/componentactions.lua \
  scripts/components/playeractionpicker.lua \
  scripts/actions.lua
rg -n "ACTIONS\\.PET|OPEN_CRAFTING|hideprototyperaction|nopet" \
  scripts/componentactions.lua \
  scripts/actions.lua \
  scripts/prefabs/critters.lua
~~~

### `0x32015111` Minimal Trace

Use `PlayerActionPicker` to identify the action type.
Use `CollectActions` to find the component collector.
Treat `ACTIONS.*` as a candidate definition until the execution path reaches `ACTIONS.*.fn`.
