# `0x72010000` Tuning and Recipes

`TUNING`, `Recipe2`, and `AllRecipes` are data entry points rather than behaviour entry points.
Trace their consumers in the UI, replica, RPC handler, and server component after finding how each table is built.

## `0x72011111` Data Roles

`scripts/tuning.lua` maintains `TUNING`, `TUNING_MODIFIERS`, and `ORIGINAL_TUNING`.
`scripts/recipe.lua` defines `Recipe`, `Recipe2`, `Ingredient`, and `AllRecipes`.
`scripts/recipes.lua` registers official recipes with `Recipe2(...)`.
`scripts/components/builder.lua` and `scripts/components/builder_replica.lua` are the main behavioral consumers.

## `0x72011211` Official and Mod Recipes

Official recipes call `Recipe2(...)` directly and initially receive incrementing `rpc_id` values.
`scripts/modutil.lua` exposes `AddRecipe2` to mod environments and calls `SetModRPCID()`.

## `0x72012000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/tuning.lua` | `TUNING` | Global numeric constants |
| `scripts/tuning.lua` | `AddTuningModifier` | Deferred modifier mechanism |
| `scripts/worldsettings_overrides.lua` | `OverrideTuningVariables` | Records and applies world-setting overrides |
| `scripts/recipe.lua` | `Ingredient` | Ingredient data |
| `scripts/recipe.lua` | `Recipe` | Recipe base class |
| `scripts/recipe.lua` | `Recipe2` | Recipe construction entry |
| `scripts/recipe.lua` | `AllRecipes` | Runtime recipe table |
| `scripts/recipes.lua` | `Recipe2(...)` | Official recipe registrations |
| `scripts/recipes_filter.lua` | `CRAFTING_FILTER_DEFS` | Crafting-menu filter data |
| `scripts/modutil.lua` | `env.AddRecipe2` | Mod recipe registration wrapper |
| `scripts/components/builder.lua` | `Builder:MakeRecipe` | Server-side entity creation |
| `scripts/components/builder_replica.lua` | `Builder:MakeRecipeFromMenu` | Selects local or remote crafting |
| `scripts/components/playercontroller.lua` | `PlayerController:RemoteMakeRecipeFromMenu` | Sends the crafting RPC |
| `scripts/networkclientrpc.lua` | `MakeRecipeFromMenu` | Restores a recipe from `rpc_id` |
| `scripts/widgets/redux/craftingmenu_widget.lua` | `AllRecipes` | Frontend recipe list |

### `0x72012111` Recipe Anchor

Find `AllRecipes = {}`, `Recipe = Class`, and `Recipe2 = Class`.
Verify storage in `AllRecipes[name]` and the incrementing `rpc_id` assigned to official recipes.

### `0x72012211` Tuning Anchor

`TUNING`, `TUNING_MODIFIERS`, and `ORIGINAL_TUNING` are initialized near the top of `scripts/tuning.lua`.
`Tune()` rebuilds `TUNING`, and the file installs a metatable at the end.
`AddTuningModifier` saves the base value and sets the key to nil so reads go through `__index`.

## `0x72013000` Data Flow

~~~mermaid
flowchart TD
    A["tuning.lua: Tune() builds TUNING"]
    A --> W["worldsettings_overrides changes selected keys"]
    W --> B["components / prefabs read TUNING keys"]
    A --> C["AddTuningModifier stores modifier"]
    C --> D["TUNING __index calculates nil keys"]
    E["recipe.lua defines Ingredient / Recipe / Recipe2"]
    F["recipes.lua calls Recipe2"]
    E --> F
    F --> G["AllRecipes[name]"]
    G --> H["crafting UI and filters"]
    G --> I["builder_replica: Builder:MakeRecipeFromMenu"]
    I --> P["playercontroller: RemoteMakeRecipeFromMenu"]
    P --> J["networkclientrpc restores recipe by rpc_id"]
    J --> K["Builder:MakeRecipe / DoBuild"]
~~~

### `0x72013111` Tuning Consumers

Do not stop at a `TUNING` definition.
For combat, hunger, or boat values, find every consumer of the specific key.

### `0x72013211` Recipe Fields

`Recipe` separates ordinary, character, and technology ingredients with `IsCharacterIngredient` and `IsTechIngredient`.
`product`, `placer`, `testfn`, and `canbuild` affect later crafting behaviour.
So do `builder_tag`, `builder_skill`, and `sg_state`.
`limitedamount` and `getlimitedrecipecount` also enter the network-synchronized crafting-station limit path.

### `0x72013311` UI and Builder Consumers

The crafting UI reads `AllRecipes` for display, search, pins, and filters.
The client starts crafting requests through `builder_replica`.
Its remote branch calls `playercontroller:RemoteMakeRecipeFromMenu`, which sends the RPC.
`networkclientrpc.lua` restores the recipe from `rpc_id`.
`components/builder.lua` then removes ingredients and creates the entity on the server.

## `0x72014111` Modifier State

`TUNING_MODIFIERS` stores a modifier function and its current base value for each modified key.
`ORIGINAL_TUNING` is separate: `OverrideTuningVariables` records values there before applying world-setting overrides.
`Tune()` resets both tables.
When a value is unexpected, inspect the literal, an active modifier, and world-setting overrides.

## `0x72014211` Recipe IDs

The `Recipe` constructor sets `rpc_id`.
Official recipes use incrementing IDs, while mod recipes call `SetModRPCID()` through `env.AddRecipe2()`.
`MakeRecipeFromMenu`, `MakeRecipeAtPoint`, and `BufferBuild` in `networkclientrpc.lua` scan `AllRecipes` by `rpc_id`.

## `0x72014311` Official Recipe Data

`recipes.lua` begins with `require("recipe")` and defines `PROTOTYPER_DEFS` plus placer test functions.
It also defines character restrictions and event recipes before registering them with `Recipe2`.
Use `recipes_filter.lua` and crafting UI consumers to verify display categories.
Carnival golf cutout items share `CUTOUT_SMART_RADIUS` instead of separate radius tables.
`witchbroom` and `pumpkinhat` use `TECH.HALLOWED_NIGHTS` with the `NEEDSHALLOWED_NIGHTS` hint.
Both appear under `SPECIAL_EVENT`; the broom also appears under `TOOLS`.
Five ritual-boss chesspiece builders use `TECH.LOST`, sculpting ingredients, and the `CRAFTING_STATION` filter.

## `0x72014411` Desiccant and Fumarole Tuning

`tuning.lua` defines `DESICCANT_HELD_TEMPERATURE`, `DESSICANT_MIN_TEMPERATURE`, and `DESSICANT_THRESHOLD_TEMPERATURE`.
`prefabs/desiccant.lua` reads these keys to decide whether the desiccant continues controlling external moisture.
`TRAP_FUMAROLE_TEMP_MODIFIER` is `-20`.
`InventoryItemTemperature:GetTargetDeltaTemperature` derives `externalheaterpower` from nearby exothermic heaters.
`trap_fumarole.lua` uses that value to scale `TRAP_FUMAROLE_TEMP_MODIFIER` linearly.
`standardcomponents.lua` applies the same mechanism separately to `FUMAROLETOOL_TEMP_MODIFIER`.

## `0x72014511` Arena and Reward Tuning

`SHROUDEN_ATTACK_PERIOD` and `SHROUDEN_PORTAL_CD` contain one value per threat level.
`prefabs/shrouden.lua` selects the attack period, while `stategraphs/SGshrouden.lua` selects the portal cooldown.
`KING_CANE_CORRUPTION` maps gem types to corruption increments consumed by `prefabs/king_cane.lua`.
The cane also reads corruption thresholds to select its effects, shadow-item tag, and sanity drain.
`QUEEN_TORCH_RADIUS_SQ` bounds `FindClosestQueenTorchAtXZ` when it searches registered torch lights.
`WORM_BOSS_RITUAL_WAVE_TIME_MULT` reaches the hound scheduler through `atriumritualorgantracker`.
`prefabs/atrium_gate.lua:GetRitualTime` selects `SHROUDEN_RITUAL_TIME` (14 seconds) before Charlie's defeat.
After `charlie_tracker:IsCharlieDefeated()` becomes true, it selects `SHROUDEN_RITUAL_NOCHARLIE_TIME` (6 seconds).
Both the target update and summon task read this selection.
Trace these tables to their consumers before treating a numeric change as an isolated balance change.

## `0x72014611` Hallowed Nights Tuning

`LEAFPILEMANAGER_*` controls placement density and timing; `LEAFPILE_*` controls growth and sweep outcomes.
`WITCHBROOM_USES` and its wear keys feed the broom's `finiteuses`, including sweep, attack, and enchanted movement costs.
`PUMPKIN_BOMB_*` supplies explosion range, damage, drops, and king-trade probabilities.
`HN_MONSTER_*` is shared by the Frankenswine, Mermummy, and Vamphare implementations in their base creature Prefabs.
Follow these consumers through [Items and Crafting](../../0x6000-gameplay-systems/0x6300-items-crafting-events/0x6301-items-crafting.md)
and [Creatures and Bosses](../../0x6000-gameplay-systems/0x6100-characters-creatures/0x6102-creatures-bosses.md).

## `0x72015100` Verification

~~~bash
rg -n "TUNING = \\{|TUNING_MODIFIERS|AddTuningModifier|setmetatable\\(TUNING" \
  scripts/tuning.lua

rg -n "OverrideTuningVariables|ORIGINAL_TUNING" \
  scripts/worldsettings_overrides.lua

rg -n "Ingredient = Class|Recipe = Class|Recipe2 = Class|AllRecipes\\[name\\]" \
  scripts/recipe.lua

rg -n "Recipe2\\(|PROTOTYPER_DEFS|env.AddRecipe2" \
  scripts/recipes.lua scripts/modutil.lua

rg -n \
  -e "CUTOUT_SMART_RADIUS" \
  -e "DESICCANT_HELD_TEMPERATURE|DESSICANT_MIN_TEMPERATURE|DESSICANT_THRESHOLD_TEMPERATURE" \
  -e "TRAP_FUMAROLE_TEMP_MODIFIER|FUMAROLETOOL_TEMP_MODIFIER|externalheaterpower" \
  scripts/recipes.lua \
  scripts/tuning.lua \
  scripts/components/inventoryitemtemperature.lua \
  scripts/prefabs/desiccant.lua \
  scripts/prefabs/trap_fumarole.lua \
  scripts/standardcomponents.lua

rg -n "MakeRecipeFromMenu|MakeRecipeAtPoint|BufferBuild|rpc_id" \
  scripts/components/builder.lua \
  scripts/components/builder_replica.lua \
  scripts/components/playercontroller.lua \
  scripts/networkclientrpc.lua

rg -n "SHROUDEN_ATTACK_PERIOD|SHROUDEN_PORTAL_CD|KING_CANE_CORRUPTION|QUEEN_TORCH_RADIUS_SQ" \
  scripts/tuning.lua scripts/prefabs/shrouden.lua scripts/stategraphs/SGshrouden.lua \
  scripts/prefabs/king_cane.lua scripts/prefabs/queen_torch.lua

rg -n "WORM_BOSS_RITUAL_WAVE_TIME_MULT|hounded_forceplannewattack|multtimetoattack" \
  scripts/tuning.lua scripts/components/atriumritualorgantracker.lua scripts/components/hounded.lua

rg -n "GetRitualTime|SHROUDEN_RITUAL_(NOCHARLIE_)?TIME|IsCharlieDefeated" \
  scripts/tuning.lua scripts/prefabs/atrium_gate.lua

rg -n -e "witchbroom|pumpkinhat" \
  -e "chesspiece_(batboss_shadow|rockyboss_shadow|wormboss_shadow|charlie_boss|shrouden)_builder" \
  scripts/recipes.lua scripts/recipes_filter.lua

rg -n "LEAFPILE|WITCHBROOM|PUMPKIN_BOMB|HN_MONSTER" \
  scripts/tuning.lua scripts/components/leafpilemanager.lua scripts/prefabs/leafpile.lua \
  scripts/prefabs/witchbroom.lua scripts/prefabs/pumpkin_bomb.lua \
  scripts/prefabs/pigking.lua scripts/prefabs/mermking.lua \
  scripts/prefabs/pigman.lua scripts/prefabs/merm.lua scripts/prefabs/bunnyman.lua
~~~

### `0x72015111` Minimal Trace

Sample `Recipe2("lighter", ...)` in `recipes.lua`, then inspect its fields and `rpc_id` in `recipe.lua`.
Follow its display through `craftingmenu_widget.lua`.
Then trace server behaviour through `builder_replica.lua -> playercontroller.lua -> networkclientrpc.lua -> components/builder.lua`.
