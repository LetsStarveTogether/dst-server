# `0x32020000` BufferedAction

`BufferedAction` turns a candidate into an action that can be queued, predicted, and failed.
It stores context while `EntityScript` manages its lifecycle.
The StateGraph selects animation state, and `BufferedAction:Do` calls `ACTIONS.*.fn`.

## `0x32021111` Purpose

Connect `BufferedAction(...)` to `EntityScript:PushBufferedAction` and `StateGraphInstance:StartAction`.
Then trace `EntityScript:PerformBufferedAction` into `BufferedAction:Do`.

## `0x32021211` `pre_action_cb` Boundary

`BufferedAction:Do` only checks `IsValid()` and calls `self.action.fn(self)`.
`ACTIONS.*.pre_action_cb` normally runs earlier in `PlayerController:DoAction` or another submission path.
It is not part of `BufferedAction:Do`.

## `0x32022000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/bufferedaction.lua` | `BufferedAction` | Store action context. |
| `scripts/bufferedaction.lua` | `BufferedAction:IsValid` | Validate context before execution. |
| `scripts/bufferedaction.lua` | `BufferedAction:Do` | Call `action.fn` and dispatch success or failure callbacks. |
| `scripts/entityscript.lua` | `EntityScript:PushBufferedAction` | Choose the WALKTO, instant, SG, or failure path. |
| `scripts/entityscript.lua` | `EntityScript:PerformBufferedAction` | Face, emit, and execute. |
| `scripts/entityscript.lua` | `EntityScript:GetBufferedAction` | Return the entity or locomotor action. |
| `scripts/stategraph.lua` | `StateGraphInstance:StartAction` | Select a state or execute. |
| `scripts/stategraphs/SGwilson.lua` | `ActionHandler(ACTIONS.*)` | Map server player actions to states. |
| `scripts/stategraphs/SGwilson_client.lua` | `ActionHandler(ACTIONS.*)` | Map predicted client actions to states. |

### `0x32022111` Submission Anchor

Start with `PushBufferedAction`.
It is the shortest place to see whether an action takes the WALKTO, instant, StateGraph, or failure path.

### `0x32022121` Execution Anchor

Then inspect `BufferedAction:Do` and `BufferedAction:IsValid`.
They confirm that candidate collection does not produce the final side effect.

## `0x32023000` Execution Flow

~~~mermaid
flowchart TD
    A["PlayerActionPicker / PlayerController creates BufferedAction"]
    A --> B["EntityScript:PushBufferedAction"]
    B --> C{"TestForStart / IsValid"}
    C -->|fail| D["PushEvent actionfailed"]
    C -->|WALKTO| E["PushEvent performaction + Succeed"]
    C -->|instant| F["PushEvent performaction + BufferedAction:Do"]
    C -->|ordinary action| G["StateGraphInstance:StartAction"]
    C -->|no SG| R["PushEvent startaction"]
    G -->|deststate| H["GoToState animation state"]
    G -->|no deststate| I["EntityScript:PerformBufferedAction"]
    H --> J["State frame or event calls PerformBufferedAction"]
    I --> K["BufferedAction:Do"]
    J --> K
    K --> L["ACTIONS.*.fn"]
    L --> M{"success"}
    M -->|true| N["OnUsedAsItem + Succeed"]
    M -->|false| O["Fail"]
~~~

### `0x32023111` Initial Submission

`PushBufferedAction` ignores a matching submission and keeps the current `self.bufferedaction`.

For a different submission, it fails and clears the current action.
It then calls `bufferedaction:TestForStart()`, which points to `BufferedAction:IsValid`.
When this check fails, it only emits `actionfailed` and returns without calling `bufferedaction:Fail()`.

### `0x32023121` WALKTO and Instant Actions

`ACTIONS.WALKTO` emits `performaction` and calls `Succeed()` without calling `BufferedAction:Do`.
An `action.instant` or `bufferedaction.options.instant` action emits `performaction`.
It then calls `bufferedaction:Do()` immediately.

### `0x32023131` StateGraph Path

Ordinary actions enter `self.sg:StartAction(bufferedaction)`.
A matching `actionhandlers[action]` with `deststate` enters that state through `GoToState`.
A matching handler without `deststate` calls `inst:PerformBufferedAction()` directly.
When no handler is available, `PushBufferedAction` emits `performaction` and fails the action.
When the entity has no `self.sg`, `PushBufferedAction` only emits `startaction` and does not call `BufferedAction:Do()`.

### `0x32023211` `PerformBufferedAction`

`PerformBufferedAction` faces the target and emits `performaction` before calling `bufferedaction:Do()`.
It keeps a local `bufferedaction` reference because code inside `Do()` can still inspect `inst.bufferedaction`.

### `0x32023221` `BufferedAction:Do`

`Do()` calls `IsValid()`, then `action.fn(self)`, and records the failure `reason`.
Success runs `OnUsedAsItem` and `Succeed()`.
Failure runs `Fail()`.
After a failed call, `EntityScript:PerformBufferedAction` also emits `actionfailed`.

### `0x32023231` Failure Matrix

Not every failed `BufferedAction:Do()` emits `actionfailed`.

| Failure point | Event and callback behaviour |
| --- | --- |
| `TestForStart()` inside `PushBufferedAction` | Emit only `actionfailed`, then return. |
| Instant `Do()` returns false | `performaction` fired; `Do()` calls `Fail()` without another `actionfailed`. |
| SG `PerformBufferedAction()` returns false | The caller emits `actionfailed`, then revisits `Fail()`. |
| No usable SG action handler | Emit `performaction`, then call `Fail()`. |

## `0x32024111` Stored Context

Core fields are `doer`, `target`, `initialtargetowner`, `action`, `invobject`, and `doerownsobject`.
Position fields are `pos`, `rotation`, `distance`, and `arrivedist`.
Other fields are `recipe`, `forced`, `autoequipped`, `skin`, `onsuccess`, `onfail`, and `options`.
It wraps `pos` as a `DynamicPosition`.

## `0x32024121` Execution-Time Validation

`BufferedAction:IsValid` checks more than whether the doer and target still exist.
`initialtargetowner` prevents execution after the target changes containers.
`doerownsobject` requires the item to remain owned by the doer.
`autoequipped` requires an empty active-item slot.
`pos.walkable_platform` must remain valid.
`self.validfn` participates in validation.
On the master simulation, `action.validfn` also participates.

## `0x32024211` Success and Failure Callbacks

`Succeed()` runs `onsuccess` and clears both `onsuccess` and `onfail`.
`Fail()` runs `onfail` and also clears both tables.
`AddSuccessAction` and `AddFailAction` populate these callback lists.
A failed `BufferedAction:Do` can call `Fail()` before `PerformBufferedAction` emits `actionfailed`.
The latter then revisits an already-cleared callback list.

## `0x32024311` Client Prediction

A predicting client usually enters through `PlayerController:DoAction` and `locomotor:PreviewAction`.
`EntityScript:PreviewBufferedAction` handles `ACTIONS.WALKTO` specially.
`bufferedaction.options.instant` or `action.instant` enters `PerformPreviewBufferedAction` directly.
With an SG, preview first tries `sg:PreviewAction(bufferedaction)`.
With an action-handler table, a missing handler for a non-instant action enters `previewaction`.

A failed handler condition or nil destination returns without that fallback.
That name is an SG state, not an entity event.
`EntityScript:PerformPreviewBufferedAction` calls `playercontroller:RemoteBufferedAction` and sets `ispreviewing`.
`RemoteBufferedAction` runs `buffaction.preview_cb()`.
`StateGraphInstance:StartAction` can also fast-forward animation frames during prediction.

## `0x32025100` Verification

~~~bash
rg -n "function EntityScript:PushBufferedAction|function EntityScript:PerformBufferedAction" scripts/entityscript.lua
rg -n "function EntityScript:GetBufferedAction" scripts/entityscript.lua
rg -n "function BufferedAction:Do|function BufferedAction:IsValid|AddFailAction|AddSuccessAction|Succeed|Fail" \
  scripts/bufferedaction.lua
rg -n "function StateGraphInstance:StartAction|actionhandlers|PerformPreviewBufferedAction" \
  scripts/stategraph.lua \
  scripts/entityscript.lua
rg -n "PreviewBufferedAction|RemoteBufferedAction|PreviewAction" \
  scripts/entityscript.lua \
  scripts/stategraph.lua \
  scripts/components/playercontroller.lua
rg -n "ActionHandler\\(ACTIONS\\.(CHOP|ATTACK|DEPLOY|PICKUP|WALKTO)" \
  scripts/stategraphs/SGwilson.lua \
  scripts/stategraphs/SGwilson_client.lua
~~~

### `0x32025111` Minimal Trace

Classify the branch in `PushBufferedAction`.
Read `StartAction` to see whether the SG takes control.
Finish with `PerformBufferedAction` and `BufferedAction:Do` to identify the `ACTIONS.*.fn` that causes the side effect.
