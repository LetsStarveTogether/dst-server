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
| `scripts/components/pointofinterest.lua` | `_CommonIndicator` | Registers local world markers with the HUD |
| `scripts/screens/mapscreen.lua` | `ProcessRMBDecorations_TOSS_MAP` | Draws previews for the selected map action |
| `scripts/screens/redux/optionsscreen.lua` | `_BuildSettings` / `_BuildAdvancedSettings` | Platform-dependent settings |
| `scripts/widgets/redux/modstab.lua` | `ModsTab` / `DoFocusHookups` | Mod buttons and controller focus |

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
`Controls:ToggleMap` selects `QuagmireRecipeBookScreen` for `quagmire` and otherwise checks `no_minimap`.
Opening either screen requires `IsMapControlsEnabled()`; closing an open map does not repeat that input gate.
`Controls:OnUpdate` moves `playeractionhint` behind the player when an item has `golfclub_reticule`.
This keeps the hint clear of the target.

## `0x71024111` UI Tree

`Widget:AddChild` records parent-child hierarchy used by position, scale, visibility, updates, and input.

## `0x71024211` Opening the Map

The map is not a regular child drawn by `Controls`.
`Controls:ToggleMap` adds it to the frontend stack through `TheFrontEnd:PushScreen(MapScreen(self.owner))`.
`PlayerController:PullUpMap` preserves the active inventory item when it is the selected `maptarget`.
`MapScreen:ProcessRMBDecorations_TOSS_MAP` reads `rmb.invobject` for its decoration callbacks.
This lets a map action preview follow the actual action item, including one held by the cursor.

## `0x71024311` Gameplay Boundary

`PlayerHud:IsCraftingBlockingGameplay` is deprecated and always returns `false`.
Input consumption depends on `PlayerHud:OnControl` and the screen stack.
`PlayerController:ShouldPlayerHUDControlBeIgnored` runs before HUD shortcuts.
It lets higher-priority mapped controller actions win.
The HUD is not authoritative server state, so gameplay outcomes must be traced to a component or RPC.

## `0x71024411` POI Visibility

`PointOfInterest` creates local marker, stand, and ring entities through `_CommonIndicator`.
When a local HUD exists, it registers them through `PlayerHud:AddPOIIndicator` and unregisters them on removal.
`PlayerHud:Toggle` hides or shows these entities with the HUD.
New indicators also start hidden when the HUD is hidden.
`PointOfInterest:OnUpdate` additionally checks `Profile:GetPOIDisplay()` and HUD visibility for its marker.

## `0x71024511` Platform Settings and Mod Controls

`OptionsScreen` exposes the crash-report checkbox on Steam or Rail only when outside gameplay.
Its callback immediately changes `TheSim:SetCrashReportsEnabled` using the current engine setting.
The automatic-mod-download option appears only when `CanDownloadWorkshopMods()` is true.
That helper delegates to the engine when available and defaults to true if the engine method is absent.
`ModsTab` also hides its clean-all button when Workshop downloading is unavailable.
Controller focus points to the first available item in `allmodsmenu`.
Both quick-join screens pass the selected server's `row` with its GUID to `TheNet:JoinServerResponse`.

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

rg -n "AddPOIIndicator|RemovePOIIndicator|poiindicators|Profile:GetPOIDisplay" \
  scripts/screens/playerhud.lua scripts/components/pointofinterest.lua

rg -n "PullUpMap|maptarget ~= activeitem|ProcessRMBDecorations_TOSS_MAP|rmb.invobject" \
  scripts/components/playercontroller.lua scripts/screens/mapscreen.lua

rg -n "show_crashreports|GetCrashReportsEnabled|SetCrashReportsEnabled|CanDownloadWorkshopMods|allmodsmenu" \
  scripts/screens/redux/optionsscreen.lua scripts/widgets/redux/modstab.lua scripts/modindex.lua

rg -n "JoinServerResponse" scripts/screens/quickjoinscreen.lua scripts/screens/redux/quickjoinscreen.lua
~~~

### `0x71025111` Minimal Trace

Start with `CONTROL_MAP` and confirm that `PlayerHud:OnControl` calls `self.controls:ToggleMap()`.
Verify the game-mode, `no_minimap`, and `IsMapControlsEnabled()` checks.
Only then inspect the final `TheFrontEnd:PushScreen` or `TheFrontEnd:PopScreen` call.
