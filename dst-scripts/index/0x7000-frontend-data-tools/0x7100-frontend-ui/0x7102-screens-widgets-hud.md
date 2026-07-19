# `0x71020000` Screens, Widgets, and HUD

Screens own page lifecycle.
Widgets own the UI tree and focus recursion.
`PlayerHud` groups in-game controls under `Controls`.

## `0x71021111` Focused Input

Input reaches the top screen through `FrontEnd:OnControl`.
`Widget:OnControl` then recurses only into child widgets whose `focus` flag is set.

## `0x71021211` HUD as a Screen

`PlayerHud` inherits from `Screen`.
It presents status and coordinates map, chat, crafting, wardrobe, and Scrapbook screens.

## `0x71022000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/widgets/screen.lua` | `Screen` | Page base class derived from `Widget` |
| `scripts/widgets/screen.lua` | `Screen:OnBecomeActive` | Sets the UI root and restores focus |
| `scripts/widgets/widget.lua` | `Widget:AddChild` | Builds the UI tree |
| `scripts/widgets/widget.lua` | `Widget:OnControl` | Handles input through the focused subtree |
| `scripts/widgets/widget.lua` | `Widget:SetFocus` | Changes focus |
| `scripts/screens/playerhud.lua` | `PlayerHud` | In-game HUD screen |
| `scripts/widgets/controls.lua` | `Controls` | Root HUD widget collection |
| `scripts/widgets/controls.lua` | `Controls:ToggleMap` | Opens or closes the map screen |
| `scripts/components/playercontroller.lua` | `PlayerController:IsMapControlsEnabled` | Gates map opening |

### `0x71022111` Widget Anchor

Find `function Widget:OnControl` and verify the focus guard, focused-child loop, and `parent_scroll_list` fallback.
Only then determine which widget receives input.

### `0x71022211` HUD Anchor

`PlayerHud:CreateOverlays` creates overlays.
The `PlayerHud` constructor adds `Controls(self.owner)` to `self.root`.

## `0x71023000` Runtime Flow

~~~mermaid
flowchart TD
    A["top of FrontEnd screenstack"]
    A --> B["Screen:OnControl"]
    B --> C["Widget:OnControl"]
    C --> D{"focused child consumes?"}
    D -->|yes| E["return true"]
    D -->|no| J{"scroll control and parent_scroll_list?"}
    J -->|yes| K["delegate to parent list"]
    J -->|no| F["return false"]
    A --> G["PlayerHud when the in-game HUD is active"]
    G --> H["Controls"]
    H --> I["MapScreen / crafting menu / inventory / toast"]
~~~

### `0x71023111` Widget Recursion

Widgets do not broadcast controls to every child.
Only focused children receive recursive `OnControl` calls.
If none consumes a scroll control, the widget may delegate it to `parent_scroll_list`; otherwise it returns `false`.

### `0x71023211` Screen Activation

`Screen:OnBecomeActive` calls `TheSim:SetUIRoot(self.inst.entity)`.
It restores a valid `last_focus`, otherwise it tries `default_focus`.

### `0x71023311` HUD Controls

The `Controls` constructor creates action hints, toasts, status displays, inventory, and map and crafting roots.
`Controls:ToggleMap` checks the `quagmire` game mode, `no_minimap`, and `IsMapControlsEnabled()`.
`MapScreen` or `QuagmireRecipeBookScreen` opens only after those checks.
`Controls:OnUpdate` moves `playeractionhint` behind the player when an item has `golfclub_reticule`.
This keeps the hint clear of the target.

## `0x71024111` UI Tree

`Widget:AddChild` records parent-child hierarchy used by position, scale, visibility, updates, and input.

## `0x71024211` Opening the Map

The map is not a regular child drawn by `Controls`.
`Controls:ToggleMap` adds it to the frontend stack through `TheFrontEnd:PushScreen(MapScreen(self.owner))`.

## `0x71024311` Gameplay Boundary

`PlayerHud:IsCraftingBlockingGameplay` is deprecated and always returns `false`.
Input consumption depends on `PlayerHud:OnControl` and the screen stack.
`PlayerController:ShouldPlayerHUDControlBeIgnored` runs before HUD shortcuts.
It lets higher-priority mapped controller actions win.
The HUD is not authoritative server state, so gameplay outcomes must be traced to a component or RPC.

## `0x71025100` Verification

~~~bash
rg -n "function Widget:AddChild|function Widget:OnControl|function Widget:SetFocus|parent_scroll_list" \
  scripts/widgets/widget.lua

rg -n "function Screen:OnBecomeActive|function Screen:SetDefaultFocus" \
  scripts/widgets/screen.lua

rg -n "function PlayerHud:OnControl|self.controls = self.root:AddChild|function Controls:ToggleMap" \
  scripts/screens/playerhud.lua scripts/widgets/controls.lua

rg -n "function Controls:OnUpdate|playeractionhint|golfclub_reticule|SetScreenOffset" \
  scripts/widgets/controls.lua

rg -n "function PlayerController:IsMapControlsEnabled" \
  scripts/components/playercontroller.lua
~~~

### `0x71025111` Minimal Trace

Start with `CONTROL_MAP` and confirm that `PlayerHud:OnControl` calls `self.controls:ToggleMap()`.
Verify the game-mode, `no_minimap`, and `IsMapControlsEnabled()` checks.
Only then inspect the final `TheFrontEnd:PushScreen` or `TheFrontEnd:PopScreen` call.
