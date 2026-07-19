# `0x71030000` Crafting UI

The crafting path starts at recipe registration and visibility metadata.
It continues through display, click handling, replica requests, and authoritative builder execution.
Keep direct crafting, automatic sub-ingredient crafting, and placer-based crafting separate while tracing it.

## `0x71031111` Build Button

`CraftingMenuDetails:_MakeBuildButton` calls `DoRecipeClick`.
`DoRecipeClick` uses `owner.replica.builder` to evaluate recipe knowledge, ingredients, and buffered builds.
It also evaluates placer behaviour.

## `0x71031211` Authority Boundary

`Builder:MakeRecipeFromMenu` in `builder_replica.lua` calls a local `components.builder` when available.
Without a local server builder, it calls `components.playercontroller:RemoteMakeRecipeFromMenu`.
A recipe with `recipe.placer` enters `StartBuildPlacementMode` instead of creating its final prefab on the menu click.

## `0x71032000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/recipe.lua` | `Recipe2` | Builds recipes and writes `AllRecipes` |
| `scripts/recipes.lua` | `Recipe2(...)` | Registers official recipes |
| `scripts/widgets/redux/craftingmenu_hud.lua` | `CraftingMenuHUD` | Opens and closes the HUD crafting menu |
| `scripts/widgets/redux/craftingmenu_widget.lua` | `CraftingMenuWidget` | Search, filters, and detail panels |
| `scripts/widgets/redux/craftingmenu_details.lua` | `_MakeBuildButton` | Build-button click entry |
| `scripts/widgets/widgetutil.lua` | `DoRecipeClick` | Shared crafting-click decision function |
| `scripts/components/builder_replica.lua` | `Builder:MakeRecipeFromMenu` | Client request path |
| `scripts/components/builder.lua` | `Builder:MakeRecipeFromMenu` | Server execution entry |
| `scripts/components/playercontroller.lua` | `StartBuildPlacementMode` | Placer preview and placement |
| `scripts/components/playercontroller.lua` | `RemoteMakeRecipeAtPoint` | Remote request for placed crafting |
| `scripts/networkclientrpc.lua` | `MakeRecipeFromMenu` / `MakeRecipeAtPoint` | Validates RPC input and restores recipes from `rpc_id` |

### `0x71032111` Decision Anchor

Find `function DoRecipeClick` and inspect `busy`, `buffered`, `knows`, and `has_ingredients`.
Then inspect `recipe.placer`, `CanCraftIngredient`, and `SetCraftingAutopaused`.

### `0x71032211` UI Anchor

The build button's `SetOnClick` reads the skin spinner and records whether the recipe is already buffered.
The non-hold path calls `DoRecipeClick(self.owner, self.data.recipe, skin)`.
If `DoRecipeClick` allows closure and buffered-build settings agree, it calls `owner.HUD:CloseCrafting()`.

## `0x71033000` Runtime Flow

~~~mermaid
flowchart TD
    A["Recipe2 in recipes.lua"]
    A --> B["AllRecipes in recipe.lua"]
    B --> C["CraftingMenuHUD:RebuildRecipes"]
    C --> D["valid_recipes + meta.build_state"]
    D --> E["CraftingMenuWidget filter / search / details"]
    E --> F["CraftingMenuDetails build button"]
    F --> G["DoRecipeClick"]
    G --> H{"recipe.placer?"}
    H -->|no| I["replica.builder:MakeRecipeFromMenu"]
    H -->|yes| J["BufferBuild + StartBuildPlacementMode"]
    I --> K{"local server builder?"}
    K -->|yes| L["components.builder:MakeRecipeFromMenu"]
    K -->|no| M["playercontroller:RemoteMakeRecipeFromMenu"]
    J --> N["MakeRecipeAtPoint / RemoteMakeRecipeAtPoint"]
~~~

### `0x71033111` Recipe Registration

`Recipe2` inherits from `Recipe`.
The `Recipe` constructor separates ordinary, character, and technology ingredients.
It then stores the object in `AllRecipes[name]`.

### `0x71033211` Recipe Presentation

`CraftingMenuHUD:RebuildRecipes` walks `AllRecipes` and writes metadata for visible recipes.
The destination is `valid_recipes[recipe.name].meta`.
`meta.build_state` includes `buffered`, `freecrafting`, `has_ingredients`, and `no_ingredients`.
It also includes `prototype`, `hint`, and `hide`.
`CraftingMenuWidget:OnUpdate` validates at most 30 uncached recipes per search update.
This avoids scanning the full table at once.

### `0x71033311` Crafting Execution

Ordinary items may call `MakeRecipeFromMenu` directly.
Recipes with `recipe.placer` usually call `BufferBuild` before `playercontroller` enters placement mode.
When ingredients are missing, `DoRecipeClick` may call `CanCraftIngredient`.
The server builder can then craft a required sub-ingredient first.
When current technology can prototype an unknown recipe, a successful build triggers prototyper and unlock behaviour.

## `0x71034111` `CraftingMenuHUD` Lifecycle

`CraftingMenuHUD:Open` sets `TheFrontEnd.crafting_navigation_mode = true` and enables the UI root.
It refreshes help text, starts the opening animation, and calls `SetCraftingAutopaused(true)`.
Search mode focuses `craftingmenu.search_box`.
`CraftingMenuHUD:Close` saves `TheCraftingMenuProfile` and disables crafting autopause.
It disables `ui_root`, `craftingmenu`, and `pinbar` during the closing animation.
The animation callback re-enables `ui_root` and `pinbar`; `craftingmenu` remains disabled until the next `Open`.

## `0x71034211` Builder Replica Branch

The replica calls a local `components.builder` when present.
Otherwise it sends a remote request through `components.playercontroller`.
The same replica implements `MakeRecipeAtPoint` for placer recipes after coordinates are chosen.

## `0x71034311` Server Builder Checks

The server builder requires the player's inventory to be open before either build path.
Non-placer menu builds first require ingredients.
`Builder:MakeRecipeFromMenu` accepts `KnowsRecipe(recipe)`.
It also accepts `CanLearn(recipe.name)` together with `CanPrototypeRecipe(...)`.
Either branch then calls `Builder:MakeRecipe`.
For placed builds, the RPC handler validates field types, platform-relative coordinates, range, and rotation.
It resolves the recipe from `rpc_id` only after those checks pass.
`Builder:MakeRecipeAtPoint` requires a placer recipe, a buffered build, and `CanDeployRecipeAtPoint`.
It also requires `KnowsRecipe(recipe)`, unless `recipe.always_allow_buffered_placer` is true.
It creates the placed result only after all checks pass.
A failed branch-specific check stops the request.

## `0x71035100` Verification

~~~bash
rg -n "Recipe2 = Class|AllRecipes\\[name\\]|function Recipe" \
  scripts/recipe.lua

rg -n "function DoRecipeClick|MakeRecipeFromMenu|BufferBuild|StartBuildPlacementMode|RemoteMakeRecipeAtPoint" \
  scripts/widgets/widgetutil.lua scripts/components/builder_replica.lua \
  scripts/components/playercontroller.lua

rg -n "MakeRecipeFromMenu =|MakeRecipeAtPoint =|IsPointInRange|IsRotationValid|rpc_id" \
  scripts/networkclientrpc.lua

rg -n "_MakeBuildButton|function CraftingMenuHUD:Open|function CraftingMenuHUD:RebuildRecipes" \
  scripts/widgets/redux/craftingmenu_details.lua \
  scripts/widgets/redux/craftingmenu_hud.lua

rg -n "function CraftingMenuWidget:OnUpdate" \
  scripts/widgets/redux/craftingmenu_widget.lua
~~~

### `0x71035111` Minimal Traces

Trace a non-placer recipe such as `lighter` and confirm that its click reaches `MakeRecipeFromMenu`.
Then trace a building recipe with a `placer` through `BufferBuild` and `StartBuildPlacementMode`.
Continue through `MakeRecipeAtPoint` or `RemoteMakeRecipeAtPoint` to the server builder's `MakeRecipeAtPoint`.
