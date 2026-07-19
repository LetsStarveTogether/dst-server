# `0x70000000` Frontend, Data, and Tools

This section connects the frontend stack, HUD, crafting UI, static data, media effects, and debugging tools.
Classify each Lua file as input handling, screen management, presentation, registration, or an authoritative request.

## `0x70001111` Purpose and Entry Points

`scripts/frontend.lua` and `scripts/input.lua` are the runtime entry points.
`scripts/tuning.lua`, `scripts/recipes.lua`, and `scripts/strings.lua` are data entry points.
`scripts/fx.lua` and `scripts/skin_assets.lua` are mainly registries or presentation assets.
`scripts/screens/redux/scrapbookdata.lua` is generated presentation data.

## `0x70001211` Authority Boundary

HUDs and widgets can consume input, play sounds, open screens, update local UI state, and request remote crafting.
Remote crafting requests go through a replica or `playercontroller`.
Authoritative world changes usually remain in a server component, a `playercontroller` RPC, or prefab behaviour.

## `0x70002000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/input.lua` | `Input:OnControl` | Gives `TheFrontEnd` the first chance to consume input |
| `scripts/frontend.lua` | `FrontEnd` | Manages `screenstack`, focus, fades, and debug panels |
| `scripts/screens/playerhud.lua` | `PlayerHud` | In-game HUD screen |
| `scripts/widgets/widget.lua` | `Widget` | UI tree, focus, and recursive `OnControl` handling |
| `scripts/widgets/controls.lua` | `Controls` | HUD control collection |
| `scripts/components/playercontroller.lua` | `PlayerController` | Map, placement, and remote crafting |
| `scripts/recipe.lua` | `Recipe2` | Recipe objects and `AllRecipes` |
| `scripts/recipes.lua` | `Recipe2(...)` | Official recipe registration |
| `scripts/tuning.lua` | `TUNING` | Numeric constants and modifiers |
| `scripts/strings.lua` | `STRINGS` | Text table |
| `scripts/translator.lua` | `TranslateStringTable` | Recursive text-table translation |
| `scripts/skin_assets.lua` | `skin_assets` | Skin asset list |
| `scripts/screens/redux/scrapbookdata.lua` | generated table | Scrapbook presentation data |
| `scripts/fx.lua` | `fx` table | Shared FX definitions |
| `scripts/prefabs/fx.lua` | `MakeFx` | Converts FX definitions into prefabs |
| `scripts/util.lua` | `DebugSpawn` | Debug entity spawning |
| `scripts/consolecommands.lua` | `c_` functions | Console command entry points |

### `0x70002111` Runtime Reading Order

Read `Input:OnControl`, `FrontEnd:OnControl`, and `FrontEnd:PushScreen` first.
Then read `Widget:OnControl`, `PlayerHud:OnControl`, and `Controls:ToggleMap`.

### `0x70002211` Data Reading Order

Find each registration point before its consumers.
`Recipe2` writes to `AllRecipes`, and `CraftingMenuHUD:RebuildRecipes` populates `valid_recipes`.
The crafting UI then applies search, filters, and details.

## `0x70003000` Runtime Flow

~~~mermaid
flowchart TD
    A["engine input callback"]
    A --> B["TheInput:OnControl"]
    B --> C["TheFrontEnd:OnControl"]
    C --> D["top screen OnControl"]
    D --> E["Widget focus tree"]
    D --> F["PlayerHud shortcuts"]
    B --> G["Input event handlers when UI did not consume input"]
    F --> H["Controls / crafting menu / map / chat"]
    H --> I["replica builder or playercontroller request"]
    I --> K["server component / RPC / placement"]
    J["TUNING / STRINGS / Recipe2 / fx table"]
    J --> H
~~~

### `0x70003111` Input Precedence

When `TheFrontEnd:OnControl` returns `true`, input does not reach `Input.oncontrol`.
Check this branch first when UI blocks a gameplay action.

### `0x70003211` Data Is Not Behaviour

`TUNING`, `STRINGS`, `skin_assets`, and `scrapbookdata` do not execute gameplay by themselves.
Follow their consumers into a component, prefab, screen, widget, or `playercontroller` request.

## `0x70004111` Pages

- [Frontend UI](0x7100-frontend-ui/README.md)
- [Frontend and Input](0x7100-frontend-ui/0x7101-frontend-input.md)
- [Screens, Widgets, and HUD](0x7100-frontend-ui/0x7102-screens-widgets-hud.md)
- [Crafting UI](0x7100-frontend-ui/0x7103-crafting-ui.md)
- [Data, Media, and Tools](0x7200-data-media-tools/README.md)
- [Tuning and Recipes](0x7200-data-media-tools/0x7201-tuning-recipes.md)
- [Localization, Skins, and Scrapbook](0x7200-data-media-tools/0x7202-localization-skins-scrapbook.md)
- [Media, FX, and Audio](0x7200-data-media-tools/0x7203-media-fx-audio.md)
- [Tools and Debugging](0x7200-data-media-tools/0x7204-tools-debug.md)

## `0x70005100` Verification

~~~bash
rg -n "function Input:OnControl|function FrontEnd:OnControl|function FrontEnd:PushScreen" \
  scripts/input.lua scripts/frontend.lua

rg -n "function PlayerHud:OnControl|function Controls:ToggleMap|IsMapControlsEnabled|function DoRecipeClick" \
  scripts/screens/playerhud.lua scripts/widgets/controls.lua \
  scripts/components/playercontroller.lua scripts/widgets/widgetutil.lua

rg -n "Recipe2\\(|TranslateStringTable|function DebugSpawn|local fx =" \
  scripts/recipes.lua scripts/translator.lua scripts/util.lua scripts/fx.lua
~~~

### `0x70005111` Minimal Traces

Trace one closed-map `CONTROL_MAP` path from `Input:OnControl` through `PlayerHud:OnControl` and `Controls:ToggleMap`.
Continue through `IsMapControlsEnabled` to `TheFrontEnd:PushScreen`.
Trace one non-placer remote crafting button from `CraftingMenuDetails:_MakeBuildButton` through `DoRecipeClick`.
Continue through `replica.builder:MakeRecipeFromMenu` to `playercontroller:RemoteMakeRecipeFromMenu`.
