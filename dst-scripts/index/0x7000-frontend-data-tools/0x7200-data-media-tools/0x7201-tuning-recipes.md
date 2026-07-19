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
A modifier participates through `__index` only after its key is set to nil.

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

## `0x72014411` Desiccant and Fumarole Tuning

`tuning.lua` defines `DESICCANT_HELD_TEMPERATURE`, `DESSICANT_MIN_TEMPERATURE`, and `DESSICANT_THRESHOLD_TEMPERATURE`.
`prefabs/desiccant.lua` reads these keys to decide whether the desiccant continues controlling external moisture.
`TRAP_FUMAROLE_TEMP_MODIFIER` is `-20`.
`InventoryItemTemperature:GetTargetDeltaTemperature` derives `externalheaterpower` from nearby exothermic heaters.
`trap_fumarole.lua` uses that value to scale `TRAP_FUMAROLE_TEMP_MODIFIER` linearly.
`standardcomponents.lua` applies the same mechanism separately to `FUMAROLETOOL_TEMP_MODIFIER`.

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
~~~

### `0x72015111` Minimal Trace

Sample `Recipe2("lighter", ...)` in `recipes.lua`, then inspect its fields and `rpc_id` in `recipe.lua`.
Follow its display through `craftingmenu_widget.lua`.
Then trace server behaviour through `builder_replica.lua -> playercontroller.lua -> networkclientrpc.lua -> components/builder.lua`.
