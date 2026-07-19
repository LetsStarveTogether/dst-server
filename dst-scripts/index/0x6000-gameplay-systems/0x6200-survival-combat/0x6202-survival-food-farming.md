# `0x62020000` Survival, Food, and Farming

Treat eating, hunger depletion, spoilage modifiers, item temperature, and farming as separate runtime paths.
Food does not flow into the farming manager, and farming is not a continuation of the eat action.

## `0x62021000` Purpose

This guide traces how eating changes `health`, `hunger`, and `sanity`.
It also traces seeds through soil, plant stress, tending, and world-level nutrient cycles.

### `0x62021100` Separate Paths

Food values, spoilage, starvation, and farming enter through separate components.

#### `0x62021110` Reading Paths

Connect `ACTIONS.EAT`, `Eater:Eat`, `Edible:Get*`, `Hunger:DoDelta`, and `Health:DoDelta`.
Separately connect `PLANTSOIL`, `farmplantable`, `farm_plants`, and `farming_manager`.

##### `0x62021111` Key Facts

`ACTIONS.EAT.fn` calls `eater:Eat`.
`Eater:Eat` calculates health, hunger, and sanity deltas.
`Edible:Get*` reads `Perishable:IsStale` and `Perishable:IsSpoiled`.
`FarmingManager:CycleNutrientsAtPoint` updates farm nutrients.

## `0x62022000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/actions.lua` | `ACTIONS.EAT.fn` | Starts eating |
| `scripts/components/eater.lua` | `Eater:Eat` | Validates food and distributes deltas |
| `scripts/components/edible.lua` | `Edible:GetHunger` | Calculates food values and spoilage effects |
| `scripts/components/perishable.lua` | `Perishable:IsSpoiled` | Reports spoilage stages |
| `scripts/components/inventoryitemtemperature.lua` | `UpdateTemperature` | Updates item temperature |
| `scripts/components/inventoryitem.lua` | `EnableTemperature` | Enables the temperature component |
| `scripts/standardcomponents.lua` | `MakeFumaroleTool` | Configures insulated fumarole tools |
| `scripts/prefabs/trap_fumarole.lua` | `OnTemperatureDelta` | Recomputes the trap's heat modifier |
| `scripts/components/hunger.lua` | `Hunger:DoDec` | Depletes hunger and applies starvation damage |
| `scripts/components/sanity.lua` | `Sanity:DoDelta` | Applies food and continuous sanity changes |
| `scripts/prefabs/desiccant.lua` | `OnUpdateExternallyControlled` | Connects temperature to absorbed moisture |
| `scripts/components/farmplantable.lua` | `FarmPlantable:Plant` | Plants a seed in soil |
| `scripts/components/farmplanttendable.lua` | `FarmPlantTendable:TendTo` | Updates tending state |
| `scripts/prefabs/farm_plants.lua` | `MakePlant` | Builds crop prefabs and stress systems |
| `scripts/components/farming_manager.lua` | `CycleNutrientsAtPoint` | Cycles world-level soil nutrients |
| `scripts/standardcomponents.lua` | `MakeDeployableFertilizer` | Connects fertilizer deployment to soil nutrients |

### `0x62022110` `scripts/actions.lua`

`ACTIONS.EAT.fn` selects `act.target` or `act.invobject`.
When the object has `edible` and the actor has `eater`, it calls `act.doer.components.eater:Eat(obj, act.doer)`.

#### `0x62022111` Search Terms

Search for `ACTIONS.EAT.fn`, `components.edible`, `components.eater`, and `souleater`.

### `0x62022120` `scripts/components/eater.lua`

`Eater:Eat` validates diet rules with `PrefersToEat`.
It reads `Edible:GetHealth`, `Edible:GetHunger`, and `Edible:GetSanity`.
Each value is applied through the matching component.

#### `0x62022121` Search Terms

Search for `Eater:Eat`, `PrefersToEat`, `foodmemory`, `custom_stats_mod_fn`, `oneat`, and `OnEaten`.

### `0x62022130` `scripts/components/edible.lua`

`Edible:GetSanity`, `Edible:GetHunger`, and `Edible:GetHealth` convert base food values into actual effects.
They account for spoilage, spices, `foodaffinity`, and custom getters.

#### `0x62022131` Search Terms

Search for `GetSanity`, `GetHunger`, `GetHealth`, `IsStale`, `IsSpoiled`, and `foodaffinity`.

### `0x62022140` `scripts/components/hunger.lua`

`Hunger:DoDelta` changes only the current hunger value.
`Hunger:DoDec` performs timed depletion and calls `health:DoDelta(..., "hunger")` when hunger is zero.

#### `0x62022141` Search Terms

Search for `DoDelta`, `DoDec`, `hungerrate`, `hurtrate`, and `overridestarvefn`.

### `0x62022150` `scripts/components/sanity.lua`

Eating can call `sanity:DoDelta` through `Eater:Eat`.
`Sanity:Recalc` also combines equipment, wetness, light, auras, ghosts, and external modifiers.

#### `0x62022151` Search Terms

Search for `Sanity:DoDelta`, `Sanity:Recalc`, `dapperness`, `sanityaura`, and `externalmodifiers`.

### `0x62022160` `scripts/components/farmplantable.lua`

`FarmPlantable:Plant` requires a target with the `soil` tag.
It spawns the crop prefab, positions it, pushes `on_planted`, and removes the seed.

#### `0x62022161` Search Terms

Search for `FarmPlantable:Plant`, `HasTag("soil")`, `SpawnPrefab`, and `on_planted`.

### `0x62022170` `scripts/components/farmplanttendable.lua`

`FarmPlantTendable:TendTo` clears its tendable state only when `tendable` is true and `ontendtofn` succeeds.
`farm_plants.lua` calls `SetTendable(stage_data.tendable)` when growth stages change.

#### `0x62022171` Search Terms

Search for `FarmPlantTendable:TendTo`, `SetTendable`, `ontendtofn`, and `tendable_farmplant`.

## `0x62023000` Runtime Flows

~~~mermaid
flowchart TD
    A["ACTIONS.EAT"]
    A --> B["Eater:Eat"]
    B --> C["Edible:GetHealth / GetHunger / GetSanity"]
    C --> D["Perishable stale / spoiled modifiers"]
    C --> E["foodaffinity / foodmemory / spice"]
    E --> F["Health:DoDelta"]
    E --> G["Hunger:DoDelta"]
    E --> H["Sanity:DoDelta"]
    I["PLANTSOIL / DEPLOY"]
    I --> J["FarmPlantable:Plant"]
    J --> K["farm_plants MakePlant"]
    K --> L["farmplantstress / growable / farmsoildrinker"]
    L --> M["FarmingManager:CycleNutrientsAtPoint"]
~~~

### `0x62023110` Eating

`Eater:Eat` first computes three deltas.
`custom_stats_mod_fn` may adjust them together before `health`, `hunger`, and `sanity` receive their `DoDelta` calls.

#### `0x62023111` Ownership Boundary

`edible` does not directly change actor state.
It supplies values and an `OnEaten` callback; `eater` writes through the actor's components.

### `0x62023210` Starvation Damage

`Hunger:DoDec` reduces positive hunger.
When hunger is zero and `ignore_damage` is false, it calls `health:DoDelta(-hurtrate * dt, true, "hunger")`.

#### `0x62023211` Check

Starvation health loss uses the cause `"hunger"`.
This path does not pass through `Eater:Eat`.

### `0x62023310` Seed to Crop

`ACTIONS.PLANTSOIL.fn` calls `seed.components.farmplantable:Plant(act.target, act.doer)`.
`FarmPlantable:Plant` creates the crop and passes the seed prefab to its `on_planted` event.

#### `0x62023311` Stress Boundary

Crop stress is not one field.
`farm_plants.lua` attaches `farmplantstress`, `farmsoildrinker`, and `farmplanttendable`.

### `0x62023410` Held-Item Temperature and Desiccant

`inventoryitemtemperature.lua` separates `GetTargetDeltaTemperature()` from `GetTargetTemperature()`.
`DoDelta()` and `UpdateTemperature()` scale heating and cooling with inherent winter and summer insulation.
Nearby exothermic heaters accumulate `externalheaterpower`.
Fumarole tools and traps use it to adjust the `fumaroletool_mod` temperature modifier.
`desiccant.lua` uses `DESSICANT_MIN_TEMPERATURE` and `DESSICANT_THRESHOLD_TEMPERATURE`.
Together with owner wetness, they determine whether the system controls moisture.
When the owner is not dry, an overheated desiccant is clamped to `DESICCANT_HELD_TEMPERATURE`.

## `0x62024110` Food Component Responsibilities

`edible` stores food types and base values.
`perishable` stores freshness, spoilage stages, and spoilage events.
`eater` stores diet rules and absorption multipliers.
`sanity` receives one-time food deltas and calculates continuous equipment, light, and aura effects.
`gelblob_storage.lua` remembers whether an item was perishing before storage and resumes only those items when removed.

### `0x62024111` Fields to Check

Check `healthabsorption`, `hungerabsorption`, and `sanityabsorption`.
Check `degrades_with_spoilage`, `stale_hunger`, and `spoiled_hunger`.
Check whether `foodmemory` changes the multiplier for repeated foods.

## `0x62024210` Farm Definitions and World State

`farm_plant_defs.lua` defines crops.
`farm_plants.lua` creates each crop prefab from its definition.
`farming_manager.lua` stores tile nutrients and moisture.
`farmplanttendable` stores whether a crop can be tended.
`MakeDeployableFertilizer` wires `fertilizer_ondeploy` to `farming_manager:AddTileNutrients`.

### `0x62024211` Nutrient Check

`CycleNutrientsAtPoint` converts world coordinates to tile coordinates.
Without a farming overlay, it reports depleted soil.
With `test_only`, it reports depletion without changing nutrients.

## `0x62025100` Verification

~~~bash
rg -n "ACTIONS\\.EAT\\.fn|ACTIONS\\.PLANTSOIL\\.fn|farmplantable:Plant" \
  scripts/actions.lua

rg -n "Eater:Eat|PrefersToEat|custom_stats_mod_fn|foodmemory|OnEaten" \
  scripts/components/eater.lua

rg -n "GetHealth|GetHunger|GetSanity|IsStale|IsSpoiled|foodaffinity" \
  scripts/components/edible.lua \
  scripts/components/perishable.lua \
  scripts/prefabs/gelblob_storage.lua

rg -n "DoDelta|DoDec|overridestarvefn|hurtrate" \
  scripts/components/hunger.lua

rg -n "Sanity:DoDelta|Sanity:Recalc|dapperness|sanityaura|externalmodifiers" \
  scripts/components/sanity.lua

rg -n "FarmPlantable:Plant|FarmPlantTendable:TendTo|SetTendable|MakePlant" \
  scripts/components/farmplantable.lua \
  scripts/components/farmplanttendable.lua \
  scripts/prefabs/farm_plants.lua

rg -n "farmplantstress|CycleNutrientsAtPoint|AddSoilMoistureAtPoint|AddTileNutrients" \
  scripts/prefabs/farm_plants.lua \
  scripts/components/farming_manager.lua \
  scripts/standardcomponents.lua

rg -n "GetTargetDeltaTemperature|GetInsulation|externalheaterpower|SetNoWetTemperaturePenalty|DESSICANT|DESICCANT" \
  scripts/components/inventoryitemtemperature.lua \
  scripts/components/inventoryitem.lua \
  scripts/prefabs/desiccant.lua \
  scripts/prefabs/trap_fumarole.lua \
  scripts/standardcomponents.lua \
  scripts/tuning.lua
~~~

### `0x62025110` Reading Order

Trace one meal from `ACTIONS.EAT.fn`.
Trace starvation from `Hunger:DoDec`.
Then trace a seed from `ACTIONS.PLANTSOIL.fn` through soil, the crop, and the world manager.

#### `0x62025111` Minimal Traces

Use any prefab with `edible` for the food trace.
Use a seed with `farmplantable` and `farm_soil` for the farm trace.
Each trace should end at a component state change or emitted event.
