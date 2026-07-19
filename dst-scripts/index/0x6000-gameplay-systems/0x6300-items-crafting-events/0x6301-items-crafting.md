# `0x63010000` Items and Crafting

A recipe moves from client UI state to authoritative `ACTIONS.BUILD` execution.
It then produces an inventory item or world entity.
This guide follows that runtime path rather than listing every recipe.
Use `0x8000-reference` for exhaustive indexes.

## `0x63011111` Purpose

The key question is when `Recipe2`, `AllRecipes`, `Builder`, `Inventory`, and `SpawnPrefab` participate.
`scripts/recipe.lua` creates recipe objects.
`scripts/recipes.lua` declares the built-in recipes.
`scripts/widgets/redux/craftingmenu_hud.lua` calculates visible client state and handles clicks.
`scripts/components/builder.lua` owns the authoritative result.

## `0x63011211` UI Boundary

The `0x70000000` section covers crafting panels, filters, and presentation.
This guide treats the UI as an input.
It follows `builder_replica`, RPCs, `Builder:MakeRecipe*`, `Builder:DoBuild`, and `Inventory:GiveItem`.

## `0x63012000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/recipe.lua` | `Recipe2` / `GetValidRecipe` | Creates recipes and filters event recipes |
| `scripts/recipes.lua` | `Recipe2(...)` | Declares built-in recipes |
| `scripts/crafting_sorting.lua` | `CraftableSort` | Groups menu entries by existing build state |
| `scripts/widgets/redux/craftingmenu_hud.lua` | `RebuildRecipes` | Computes client craftability |
| `scripts/components/builder_replica.lua` | `Builder:MakeRecipeFromMenu` | Routes client crafting requests |
| `scripts/components/playercontroller.lua` | `PlayerController:RemoteMakeRecipe*` | Sends recipe `rpc_id` values |
| `scripts/networkclientrpc.lua` | `MakeRecipeFromMenu` | Resolves recipes on the server |
| `scripts/components/builder.lua` | `MakeRecipe` / `DoBuild` | Executes authoritative crafting |
| `scripts/actions.lua` | `ACTIONS.BUILD.fn` | Connects a `BufferedAction` to `DoBuild` |
| `scripts/components/inventory.lua` | `GiveItem` | Stores or routes crafted items |

### `0x63012111` Recipe Data

`Recipe2` extends `Recipe` and writes the object to `AllRecipes[name]`.
`GetValidRecipe` rejects deconstruction recipes and checks `require_special_event` with `IsSpecialEventActive`.
A recipe declaration alone does not make an event recipe available.

### `0x63012211` Authoritative Execution

`Builder:MakeRecipe` does not spawn the product directly.
It creates `BufferedAction(self.inst, nil, ACTIONS.BUILD, ...)` and sends it through `locomotor`.
For non-manufactured recipes, `Builder:DoBuild` consumes ingredients and calls `SpawnPrefab(recipe.product)`.
It then pushes `builditem` or `buildstructure` and invokes `prod:OnBuilt`.

## `0x63013000` Runtime Flow

~~~mermaid
flowchart TD
    A["recipes.lua: Recipe2(...)"]
    A --> B["recipe.lua: AllRecipes[name]"]
    B --> C["craftingmenu_hud.lua: RebuildRecipes"]
    C --> D["builder_replica.lua: MakeRecipeFromMenu / MakeRecipeAtPoint"]
    D --> E["playercontroller.lua: RemoteMakeRecipe*"]
    E --> F["networkclientrpc.lua: lookup recipe.rpc_id"]
    F --> G["builder.lua: MakeRecipe* / BufferBuild"]
    G --> H["BufferedAction(ACTIONS.BUILD)"]
    H --> I["actions.lua: ACTIONS.BUILD.fn"]
    I --> J["builder.lua: DoBuild"]
    J --> K["RemoveIngredients + SpawnPrefab"]
    K --> L["inventory.lua: GiveItem or world placement"]
~~~

### `0x63013111` Menu Crafting

`Builder:MakeRecipeFromMenu` handles ordinary products without a placer.
It requires gameplay-enabled inventory UI.
It calls `HasIngredients` and `KnowsRecipe` before checking temporary technology, tags, skills, and learnability.
When ingredients are missing, `_TryMakeIngredientRecipe` may craft a missing subrecipe first.

### `0x63013211` Placed Crafting

Buildings and deployables with a placer commonly use `BufferBuild`.
`BufferBuild` pre-consumes ingredients and sets `buffered_builds[recname]` to true.
`MakeRecipeAtPoint` then checks `TheWorld.Map:CanDeployRecipeAtPoint(pt, recipe, rot)`.
This check is where boat restrictions, terrain rules, and rotation validity enter the crafting path.

### `0x63013311` Build Action

`ACTIONS.BUILD.fn` delegates to `act.doer.components.builder:DoBuild(...)`.
The StateGraph handles animation and prediction boundaries.
`Builder:DoBuild` owns spawning, ingredient consumption, and events.

## `0x63014111` Runtime Recipe Fields

- `product` names the prefab passed to `SpawnPrefab`.
- `placer` selects the placement path.
- `builder_tag` and `builder_skill` restrict characters or skills.
- `require_special_event` is checked by `GetValidRecipe`.
- `manufactured` delegates product creation to a crafting station.
- `dropitem` places the product in the world instead of calling `Inventory:GiveItem`.
- `numtogive` and `override_numtogive_fn` change the product count.

## `0x63014211` Inventory and World Results

`Builder:DoBuild` branches on `prod.components.inventoryitem`.
Products with `inventoryitem` follow the inventory path.
Inventory products may be equipped, stacked, or passed to `GiveOrDropItem`.
Non-inventory products receive a world position and rotation and emit `buildstructure` and `onbuilt`.
Crafting success therefore does not always mean the product enters the inventory.

## `0x63014311` Ocean Recipes

Ocean products remain ordinary `Recipe2` declarations in `recipes.lua`.
Examples include `boat_item`, `anchor_item`, `mast_item`, `boat_rotator_kit`, and `boat_magnet_kit`.
Boat products continue into `scripts/prefabs/boat.lua`.
Anchors and masts continue into `scripts/prefabs/anchor.lua` and `scripts/prefabs/mast.lua`.

## `0x63014411` Carnival Golf Recipes

The `carnivalgame_golfgame_kit_*` recipes create courses.
Examples include `carnivalgame_golfgame_kit_easy` and `carnivalgame_golfgame_kit_medium`.
So do `carnivalgame_golfgame_kit_hard` and `carnivalgame_golfgame_kit_diy`.
Course props use `TECH.CARNIVAL_GOLFPROPS_ONE` and the matching prototyper to require a `carnivalgame_golfgame` area.
Golf prop recipes use empty ingredient lists.
Their main constraints are `testfn` and `overridecandeployrecipeatpointfn`.
Check `IsGolfPropWithinGolfArea` in `recipes.lua` before the area bounds in `carnivalgame_golfgame.lua`.
Cutout props share `CUTOUT_SMART_RADIUS`.
Premade course kits carry `_available_courses`, and redeployment removes the last `_coursecode_index` before choosing again.
Custom tee crafting uses `OPEN_CRAFTING` and `prototyper.redirect_to_prototyper`.
The tee removes its own `prototyper` tag so it does not appear as a standard research station.

## `0x63015100` Verification

~~~bash
rg -n "Recipe2|AllRecipes|GetValidRecipe|require_special_event" \
  scripts/recipe.lua \
  scripts/recipes.lua

rg -n "carnivalgame_golfgame_kit|CARNIVAL_GOLFPROPS_ONE|IsGolfPropWithinGolfArea" \
  scripts/recipes.lua

rg -n "CUTOUT_SMART_RADIUS|ResetAvailableCourses|OPEN_CRAFTING|redirect_to_prototyper|hideprototyperaction" \
  scripts/recipes.lua \
  scripts/actions.lua \
  scripts/prefabs/carnivalgame_golfgame.lua \
  scripts/prefabs/carnivalgame_golf_tee.lua

rg -n "MakeRecipeFromMenu|MakeRecipeAtPoint|BufferBuild|RemoteMakeRecipe" \
  scripts/components/builder_replica.lua \
  scripts/components/playercontroller.lua \
  scripts/networkclientrpc.lua

rg -n "MakeRecipe|DoBuild|ACTIONS.BUILD|GiveItem|SpawnPrefab" \
  scripts/components/builder.lua \
  scripts/actions.lua \
  scripts/components/inventory.lua
~~~

### `0x63015111` Reading Order

Read `Recipe2` and `GetValidRecipe` in `scripts/recipe.lua`.
Inspect one simple `Recipe2` entry in `scripts/recipes.lua`.
For an event sample, inspect `carnivalgame_golfgame_kit_easy` and the props under `TECH.CARNIVAL_GOLFPROPS_ONE`.
Follow the recipe's `rpc_id` from `builder_replica` through `networkclientrpc`.
Finish in `Builder:DoBuild`.
Verify whether the product reaches inventory, equipment, world space, or a crafting station.
