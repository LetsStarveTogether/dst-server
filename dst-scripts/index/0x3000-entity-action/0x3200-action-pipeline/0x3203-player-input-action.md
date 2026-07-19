# `0x32030000` Player Input to Action

The path starts at `input.lua`, then enters `playercontroller.lua` and `playeractionpicker.lua`.
It continues through `componentactions.lua`, `bufferedaction.lua`, and the stategraph or action function.
Mouse input is the main path, with branches for client prediction and server-authoritative execution.

## `0x32031111` Purpose

Trace a click through target selection, candidates, `BufferedAction`, and RPC or preview.
Finish at the server `ACTIONS.*.fn`.

## `0x32031211` Input Boundary

`input.lua` provides the mouse position, HUD and world entities under the cursor, and control state.
`PlayerController` turns that state into `LMBaction`, `RMBaction`, RPCs, or locomotor preview.
Component side effects still occur through the server-authoritative action path.

## `0x32032000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/input.lua` | `TheInput:GetWorldPosition` | Read the mouse world position. |
| `scripts/input.lua` | `TheInput:GetWorldEntityUnderMouse` | Read the entity under the mouse. |
| `scripts/input.lua` | `TheInput:GetHUDEntityUnderMouse` | Detect HUD blocking. |
| `scripts/components/playercontroller.lua` | `PlayerController:OnUpdate` | Cache `LMBaction` and `RMBaction`. |
| `scripts/components/playercontroller.lua` | `PlayerController:OnLeftClick` | Submit a left click. |
| `scripts/components/playercontroller.lua` | `PlayerController:OnRightClick` | Submit a right click. |
| `scripts/components/playercontroller.lua` | `PlayerController:DoAction` | Choose the submission path. |
| `scripts/components/playercontroller.lua` | `PlayerController:OnRemoteControllerActionButtonPoint` | Rebuild a controller point action on the server. |
| `scripts/components/playeractionpicker.lua` | `DoGetMouseActions` | Calculate left- and right-click candidates. |
| `scripts/componentactions.lua` | `EntityScript:CollectActions` | Collect candidates by action type. |
| `scripts/bufferedaction.lua` | `BufferedAction` | Store action context. |
| `scripts/entityscript.lua` | `EntityScript:PushBufferedAction` | Enter authoritative action execution. |
| `scripts/stategraphs/SGwilson.lua` | `ActionHandler(ACTIONS.*)` | Map server player actions to states. |
| `scripts/stategraphs/SGwilson_client.lua` | `ActionHandler(ACTIONS.*)` | Map predicted client actions to states. |

### `0x32032111` PlayerController Anchor

Find `LMBaction` and `RMBaction`.
`OnUpdate` refreshes them through `playeractionpicker:DoGetMouseActions()`.
Click handlers read them through `GetLeftMouseAction()` and `GetRightMouseAction()`.

### `0x32032121` Input Anchor

Find `GetWorldPosition`, `GetWorldEntityUnderMouse`, and `GetHUDEntityUnderMouse`.
They explain why a click over UI does not enter the world action path.

## `0x32033000` Input and Execution Flow

~~~mermaid
flowchart TD
    A["Input: mouse/control state"]
    A --> B["PlayerController:OnUpdate"]
    B --> C["PlayerActionPicker:DoGetMouseActions"]
    C --> D["LMBaction / RMBaction cache"]
    D --> E{"OnLeftClick / OnRightClick"}
    E --> F["GetLeftMouseAction / GetRightMouseAction"]
    F --> G["PlayerController:DoAction"]
    G --> H{"Execution side"}
    H -->|mastersim| I["locomotor:PushAction"]
    H -->|client without locomotor| J["non_preview_cb -> RPC"]
    H -->|predicting client| K["locomotor:PreviewAction + preview_cb -> RPC"]
    I --> L["EntityScript:PushBufferedAction"]
    J --> M["server OnRemote* rebuilds action"]
    K --> M
    M --> L
    L --> N["SGwilson / SGwilson_client ActionHandler"]
    N --> O["PerformBufferedAction"]
    O --> P["BufferedAction:Do"]
    P --> Q["ACTIONS.*.fn / component side effect"]
~~~

### `0x32033111` Action Cache

Outside controller mode, `OnUpdate` stores `DoGetMouseActions()` results in `self.LMBaction` and `self.RMBaction`.
Controller mode clears mouse actions and calls `UpdateControllerTargets(dt)`.

### `0x32033121` Mouse Target Resolution

Without a position, `DoGetMouseActions` reads `TheInput:GetWorldPosition()`.
It also reads `TheInput:GetWorldEntityUnderMouse()`.
It returns early when an entity is under the HUD.
During AOE targeting, the reticule supplies the position instead of the ordinary mouse world point.

### `0x32033211` Left Click

Left click first handles dragging, placement, repeated AOE use, double-click actions, and map targets.
The ordinary path reads `GetLeftMouseAction()`.
When no left-click action exists, it creates `BufferedAction(self.inst, nil, ACTIONS.WALKTO, nil, position)`.

### `0x32033221` Right Click

Right click first cancels placement or AOE targeting.
The ordinary path reads `GetRightMouseAction()`.
Without a right-click action, it may return the active item, begin AOE targeting, or send an empty `RPC.RightClick`.

### `0x32033311` `PlayerController:DoAction`

On a non-predicting client without a locomotor, `DoAction` may call `non_preview_cb` before local validation.
This early call occurs when the action has no `pre_action_cb`.
It then validates the action objects, busy state, and duplicate actions.
On that same path, when `pre_action_cb` exists, it and `non_preview_cb` run later, after validation and automatic equipment.
The function then handles attack retargeting, highlighting, automatic equipment, and held actions.
The master simulation calls `locomotor:PushAction(buffaction, true)`.
A non-predicting client without a locomotor sends through `non_preview_cb`.
A movable predicting client calls `locomotor:PreviewAction(buffaction, true)`.

### `0x32033321` Other Input Paths

Mouse input is not the only action source.
`PlayerController:DoActionButton` handles the action button.
Controller attack and controller action paths also create a `BufferedAction` or send an RPC.
Inventory tile and map action paths do the same.
These paths converge on `DoAction`, `locomotor:PreviewAction`, `OnRemote*`, or `RemoteBufferedAction`.
The boat cannon branch in `OnRemoteControllerActionButtonPoint` passes the player explicitly.
The call is `GetCannonAimActions(self.inst, position, false)`.
The golf charging branch calls `GetGolfAimActions(position, false)`.
When tracing controller cannon use, verify the picker signature instead of assuming the mouse arguments.

### `0x32033331` RPC and Prediction

Client click paths assign `preview_cb` or `non_preview_cb` to `act`.
These callbacks send `RPC.LeftClick`, `RPC.RightClick`, `RPC.ActionButton`, and related messages.
Server `OnRemote*` handlers rebuild the action from the action code, target, position, and mod name.

## `0x32034111` Input Sources

`TheInput:GetHUDEntityUnderMouse()` is the final UI filter before entering world actions.
`TheInput:GetWorldEntityUnderMouse()` provides an entity target.
`TheInput:GetWorldPosition()` or `TheSim:ProjectScreenPos` provides a point.

## `0x32034211` Candidate Cache

`GetLeftMouseAction()` only returns `self.LMBaction`.
`GetRightMouseAction()` only returns `self.RMBaction`.
`PlayerActionPicker:DoGetMouseActions()` performs the actual candidate calculation.

## `0x32034311` Server Authority

The local authoritative path normally advances through `locomotor:PushAction`.
Server RPC handlers converge on the same kind of authoritative execution.
The final checkpoints are `EntityScript:PushBufferedAction`, `StateGraphInstance:StartAction`, and `BufferedAction:Do`.

## `0x32035100` Verification

~~~bash
rg -n "LMBaction|RMBaction|DoGetMouseActions|GetLeftMouseAction|GetRightMouseAction" \
  scripts/components/playercontroller.lua
rg -n "OnLeftClick|OnRightClick|function PlayerController:DoAction" scripts/components/playercontroller.lua
rg -n "OnRemoteControllerActionButtonPoint|GetCannonAimActions|GetGolfAimActions" \
  scripts/components/playercontroller.lua \
  scripts/components/playeractionpicker.lua
rg -n "GetWorldPosition|GetWorldEntityUnderMouse|GetHUDEntityUnderMouse|IsControlPressed" scripts/input.lua
rg -n "GetLeftClickActions|GetRightClickActions|GetSceneActions|GetUseItemActions|GetPointActions|CollectActions" \
  scripts/components/playeractionpicker.lua \
  scripts/componentactions.lua
rg -n "PushBufferedAction|PerformBufferedAction|BufferedAction:Do|StateGraphInstance:StartAction" \
  scripts/entityscript.lua \
  scripts/bufferedaction.lua \
  scripts/stategraph.lua
rg -n "ActionHandler\\(ACTIONS\\." scripts/stategraphs/SGwilson.lua scripts/stategraphs/SGwilson_client.lua
~~~

### `0x32035111` Minimal Trace

Read `PlayerController:OnUpdate` to see when `LMBaction` and `RMBaction` refresh.
Follow `OnLeftClick` or `OnRightClick` into submission.
Use `DoAction` to distinguish master simulation, non-predicting client, and predicting client paths.
Finish at `PushBufferedAction` and `BufferedAction:Do`.
