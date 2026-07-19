# `0x71010000` Frontend and Input

Control input reaches the frontend screen stack before `Input.oncontrol` handlers.
The active HUD screen then handles shortcuts for maps, chat, pause, and crafting.

## `0x71011111` Input Dispatch

`Input:OnControl` lives in `scripts/input.lua` and first calls `TheFrontEnd:OnControl(control, digitalvalue)`.
It calls `self.oncontrol:HandleEvent(...)` only when the frontend does not consume the input.

## `0x71011211` Frontend Handling

`FrontEnd:OnControl` maps `CONTROL_PRIMARY` to `CONTROL_ACCEPT` for the top screen.
It also handles `CONTROL_OPEN_DEBUG_CONSOLE`, `CONTROL_OPEN_DEBUG_MENU`, console logging, and debug-render toggles.

## `0x71012000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/input.lua` | `Input:OnControl` | Dispatches device input into Lua |
| `scripts/input.lua` | `Input:OnMouseButton` | Calls the frontend, then dispatches mouse-button handlers |
| `scripts/frontend.lua` | `FrontEnd:OnControl` | Lets the top screen consume controls |
| `scripts/frontend.lua` | `TheInput:AddKeyHandler` | Routes raw-key callbacks into the frontend |
| `scripts/frontend.lua` | `FrontEnd:PushScreen` | Pushes, focuses, and activates a screen |
| `scripts/frontend.lua` | `FrontEnd:PopScreen` | Removes a screen and restores focus |
| `scripts/widgets/screen.lua` | `Screen:OnBecomeActive` | Sets the UI root and restores focus |
| `scripts/widgets/widget.lua` | `Widget:OnControl` | Recurses only through focused child widgets |
| `scripts/screens/playerhud.lua` | `PlayerHud:OnControl` | Handles in-game HUD shortcuts |
| `scripts/components/playercontroller.lua` | `PlayerController:ShouldPlayerHUDControlBeIgnored` | HUD input gate |

### `0x71012111` Primary Anchor

Find `function Input:OnControl`, then inspect the `not TheFrontEnd:OnControl(...)` branch.

### `0x71012211` Screen Stack

`FrontEnd:PushScreen` appends to `self.screenstack`, deactivates the previous top screen, and activates the new one.
It then runs `FrontEnd:Update(0)` so focus and layout update immediately.

## `0x71013000` Runtime Flow

~~~mermaid
flowchart TD
    A["engine control callback"]
    A --> B["TheInput:OnControl"]
    B --> C{"TheFrontEnd:OnControl consumed input?"}
    C -->|yes| D["top Screen / focused Widget"]
    C -->|no| E["Input oncontrol handlers"]
    D --> F["PlayerHud:OnControl when HUD is active"]
    F --> G["pause / map / chat / crafting shortcuts"]
    F --> I["playercontroller ignore check"]
    G --> H["TheFrontEnd:PushScreen or HUD state change"]
~~~

### `0x71013111` Input Gate

`self.mouse_enabled` gates primary and secondary controls before frontend dispatch, but other controls still proceed.
When the frontend consumes input, `Input.oncontrol` does not receive it.

### `0x71013211` Frontend Gate

When the top screen's `OnControl` returns `true`, the frontend stops further dispatch.
HUDs, dialogs, and text fields can therefore block gameplay controls.
When `FrontEnd:IsControlsDisabled()` is true, the frontend returns `false` without letting the top screen consume input.
The `textProcessorWidget` branch ends forced text processing when the mouse clicks elsewhere.

### `0x71013311` HUD Shortcuts

`PlayerHud:OnControl` handles `CONTROL_PAUSE`, `CONTROL_MAP`, chat toggles, and crafting pin pagination.
It also asks `playercontroller:ShouldPlayerHUDControlBeIgnored` whether the HUD should swallow an input.
`CONTROL_MAP` calls `Controls:ToggleMap` on release.
`CONTROL_OPEN_CRAFTING` opens or closes `CraftingMenuHUD` on press.

## `0x71014111` `FrontEnd` State

`screenstack` owns screen lifecycle.
`PushScreen` calls `AddChild`, `MoveToFront`, assigns default focus, and runs `Update(0)`.
`PopScreen` calls `OnBecomeInactive`, `OnDestroy`, and `RemoveChild`, then restores the new top screen.
`PopScreen(screen)` can also remove a non-top screen from `screenstack`.

## `0x71014211` `Input` Events

`Input` maintains event sets such as `oncontrol`, `onmousebutton`, and `onkeydown` outside the frontend screen stack.
Only `oncontrol` is suppressed by a `true` result from `FrontEnd:OnControl`.
`Input:OnMouseButton` ignores the result from `FrontEnd:OnMouseButton`.
`Input:OnRawKey` always dispatches `onkey` plus `onkeydown` or `onkeyup`.

## `0x71014311` `PlayerHud` Structure

`PlayerHud` inherits from `Screen` and adds `Controls(self.owner)` to `self.root`.
`Controls` then groups inventory, status, map, crafting menu, and toast widgets.

## `0x71015100` Verification

~~~bash
rg -n "function Input:OnControl|not TheFrontEnd:OnControl|self.oncontrol:HandleEvent" \
  scripts/input.lua

rg -n "function Input:OnMouseButton|function Input:OnRawKey|AddKeyHandler" \
  scripts/input.lua scripts/frontend.lua

rg -n "function FrontEnd:OnControl|function FrontEnd:PushScreen|function FrontEnd:PopScreen" \
  scripts/frontend.lua

rg -n "function PlayerHud:OnControl|ShouldPlayerHUDControlBeIgnored|CONTROL_MAP|CONTROL_PAUSE|CONTROL_TOGGLE_SAY" \
  scripts/screens/playerhud.lua scripts/components/playercontroller.lua
~~~

### `0x71015111` Minimal Trace

Trace `CONTROL_MAP` through `Input:OnControl`, `FrontEnd:OnControl`, `PlayerHud:OnControl`, and `Controls:ToggleMap`.
An open map should end at `TheFrontEnd:PopScreen`.
A closed map reaches `TheFrontEnd:PushScreen(MapScreen(self.owner))` only when map controls and the game mode allow it.
