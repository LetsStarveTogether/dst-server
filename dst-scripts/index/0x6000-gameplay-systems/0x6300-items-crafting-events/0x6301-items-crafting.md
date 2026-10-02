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
| `scripts/prefabs/atrium_gate.lua` | `SpawnShroudenLootSetPiece` | Creates the cane and torch during the ritual |
| `scripts/prefabs/king_cane.lua` | `OnGemGiven` / `SetGem` | Selects shared staff behaviour through gem trading |
| `scripts/prefabs/staff_common.lua` | `fns` / `cleargemstaff` | Shares spell behaviour between staffs and the cane |
| `scripts/components/corruption.lua` | `DoDelta` / `MakeCorrupted` | Tracks the cane's corruption state |
| `scripts/prefabs/moonbase.lua` | `OnStaffGiven` / `OnTimerDone` | Holds and purifies a corrupted cane |
| `scripts/prefabs/queen_torch.lua` | `FindClosestQueenTorchAtXZ` / `IgniteTossed` | Tracks torch lights and equips or throws the torch |

### `0x63012111` Recipe Data

`Recipe2` extends `Recipe` and writes the object to `AllRecipes[name]`.
`GetValidRecipe` first checks that networking exists and the recipe is valid in the current game mode.
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

## `0x63014511` King Cane and Shared Staff Spells

`SpawnShroudenLootSetPiece` in `atrium_gate.lua` creates `king_cane` and `queen_torch` before the boss is defeated.
It runs in the arena-morph callback during `ritualstate_OnSummoned`.
These items enter the world through the ritual's `SpawnPrefab` calls.
`staff.lua` and `king_cane.lua` both load gem behaviours and dependencies from `prefabs/staff_common.lua`.
The cane's `ItemTradeTest` accepts supported gems and rejects precious gems.
`OnGemGiven` clears the previous spell components, returns the previous gem, and calls `SetGem` to install the new behaviour.
`SetGem` publishes the gem through `_gem`; client `OnGemDirty` refreshes the reticule for the new spell.
Spell, blink, and projectile launch events call `DoCorruption` with an amount determined by the gem.
At maximum corruption, `OnCorruptedFn` disables gem trading and adds `shadowdominance`.
It also enables cooldowns for the shared purple, orange, green, and yellow spell paths.
`Corruption:MakeCorrupted` stops natural decay, so reaching maximum is a distinct state from accumulating partial corruption.
`moonbase.lua` keeps the cane instance when it is inserted and calls `MakeUncorrupted` when the `mooncharge` timer completes.

## `0x63014611` Queen Torch Light and Throwing

`queen_torch.lua` creates a networked `queen_torchlight` while equipped or burning on the ground.
Equipping parents the light to the holder; dropping or throwing parents it to the torch.
`OnThrown` disables pickup until `OnHit`, and `OnPutInInventory` removes the light and extinguishes the torch.
Wilson's torch skills adjust the light radius through `RefreshAttunedSkills`.
Throwing preserves the thrower's brightness modifier.
`FindClosestQueenTorchAtXZ` searches the registered light entities using `TUNING.QUEEN_TORCH_RADIUS_SQ`.
That gameplay distance is separate from the visual light radius changed by skills.
`BrainCommon.RunAwayFromQueenTorch` uses the shared lookup to make creatures flee.
Checks in specific shadow prefabs also use it to change targeting behaviour.
The prefab's save callbacks restore a lit ground torch and its saved thrower brightness data.

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

rg -n -e "SpawnShroudenLootSetPiece|ItemTradeTest|OnGemGiven|SetGem|OnGemDirty" \
  -e "DoCorruption|OnCorruptedFn|OnStaffGiven|OnTimerDone|MakeUncorrupted" \
  scripts/prefabs/atrium_gate.lua \
  scripts/prefabs/king_cane.lua \
  scripts/components/corruption.lua \
  scripts/prefabs/moonbase.lua

rg -n "staff_common|cleargemstaff|dodischarge|KING_CANE_.*COOLDOWN" \
  scripts/prefabs/staff.lua \
  scripts/prefabs/staff_common.lua \
  scripts/prefabs/king_cane.lua

rg -n -e "FindClosestQueenTorch|QUEEN_TORCH_RADIUS_SQ|RefreshAttunedSkills|IgniteTossed" \
  -e "OnThrown|OnPutInInventory|OnSave|OnLoad" \
  scripts/prefabs/queen_torch.lua \
  scripts/brains/braincommon.lua
~~~

### `0x63015111` Reading Order

Read `Recipe2` and `GetValidRecipe` in `scripts/recipe.lua`.
Inspect one simple `Recipe2` entry in `scripts/recipes.lua`.
For an event sample, inspect `carnivalgame_golfgame_kit_easy` and the props under `TECH.CARNIVAL_GOLFPROPS_ONE`.
Follow the recipe's `rpc_id` from `builder_replica` through `networkclientrpc`.
Finish in `Builder:DoBuild`.
Verify whether the product reaches inventory, equipment, world space, or a crafting station.
For the ritual items, follow `SpawnShroudenLootSetPiece` into `king_cane.lua` and `queen_torch.lua`.
Then inspect shared spells and the light lookup's callers.
