# `0x61020000` Creatures and Bosses

Creatures and bosses are both entities assembled by prefabs.
Their differences come from component parameters, Brain complexity, StateGraphs, phase events, variants, and loot logic.

## `0x61021000` Purpose

This guide compares `rabbit.lua` with `deerclops.lua` to establish a reusable source-reading pattern.

### `0x61021100` Comparison

Ordinary creatures combine movement, health, loot, and AI.
Bosses extend the same systems with targeting, phases, special attacks, and variants.

#### `0x61021110` Reading Model

Treat the prefab as the assembly layer.
Behaviour decisions belong to the Brain, animation states to the StateGraph, and mutable values to components.

##### `0x61021111` Samples

`rabbit.lua` uses `rabbitbrain` and `SGrabbit`.
`deerclops.lua` uses `deerclopsbrain` and `SGdeerclops`.
Both connect to shared systems through components such as `health`, `combat`, and `lootdropper`.

## `0x61022000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/prefabs/rabbit.lua` | `Prefab("rabbit", fn, ...)` | Ordinary creature example |
| `scripts/prefabs/deerclops.lua` | `Prefab("deerclops", normalfn, ...)` | Boss and variant example |
| `scripts/prefabs/critters.lua` | `MakeCritter` | Pet creature factory |
| `scripts/stategraphs/SGcritter_common.lua` | `SGCritterStates` | Shared pet state factory |
| `scripts/stategraphs/SGcritter_eets.lua` | `StateGraph("SGcritter_eets", ...)` | Eets-specific pet states |
| `scripts/components/combat.lua` | `Combat:SetRetargetFunction` | Targeting and hit handling |
| `scripts/components/health.lua` | `Health:SetMaxHealth` | Maximum health and death events |
| `scripts/brains/deerclopsbrain.lua` | `Brain` | Boss behaviour tree |
| `scripts/standardcomponents.lua` | `MakeCharacterPhysics` | Shared physics, burning, and freezing helpers |

### `0x61022110` `scripts/prefabs/rabbit.lua`

The `rabbit` prefab configures physics, tags, `locomotor`, its StateGraph, and its Brain.
It then adds `eater`, `inventoryitem`, `health`, `lootdropper`, and an optional `combat` component.

#### `0x61022111` Search Terms

Search for `SetStateGraph("SGrabbit")`, `SetBrain(brain)`, and `SetMaxHealth`.
Also search for `LootSetupFunction` and `AddComponent("combat")`.

### `0x61022120` `scripts/prefabs/deerclops.lua`

The `deerclops` prefab adds retargeting, target retention, timers, variants, and phase logic.
Normal and mutated forms use different health, damage, range, and loot settings.

#### `0x61022121` Search Terms

Search for `RetargetFn`, `KeepTargetFn`, `SetStateGraph("SGdeerclops")`, `SetBrain(brain)`, `normalfn`, and `mutatedfn`.

### `0x61022130` `scripts/components/combat.lua`

Use `combat` to verify whether a creature can attack, which targets are valid, and when it drops a target.
Boss prefabs commonly set `SetRetargetFunction`, `SetKeepTargetFunction`, and `SetRange`.

#### `0x61022131` Search Terms

Search for `SetRetargetFunction`, `SetKeepTargetFunction`, `CanTarget`, `SuggestTarget`, and `GetAttacked`.

### `0x61022140` `scripts/standardcomponents.lua`

`standardcomponents.lua` reuses physics, burning, freezing, and perish behaviour through prefab helpers.
Inspect this file when a prefab calls `MakeCharacterPhysics` or a related helper.
Examples include `MakeMediumBurnableCharacter` and `MakeLargeFreezableCharacter`.

#### `0x61022141` Search Terms

Search for `MakeCharacterPhysics` and `MakeGiantCharacterPhysics`.
Also search for `Make.*BurnableCharacter` and `Make.*FreezableCharacter`.

## `0x61023000` Runtime Flow

~~~mermaid
flowchart TD
    A["Creature prefab"]
    A --> B["Base tags and physics"]
    B --> C["locomotor"]
    C --> D["StateGraph"]
    C --> E["Brain"]
    D --> F["Animation events and state tags"]
    E --> G["Targets and behaviour nodes"]
    G --> H["combat"]
    H --> I["health"]
    I --> J["death and lootdropper"]
~~~

### `0x61023110` Ordinary Creature: Rabbit

Rabbit behaviour comes mostly from combining standard components.
The prefab uses `locomotor`, `SGrabbit`, `rabbitbrain`, `eater`, `health`, `lootdropper`, and optional `combat`.

#### `0x61023111` Context Boundary

Do not assume every ordinary creature has `combat`.
`rabbit.lua` adds it outside the Quagmire game mode.

### `0x61023210` Boss: Deerclops

Deerclops defines explicit retarget and keep-target functions.
It connects `health`, `combat`, `lootdropper`, `knownlocations`, and `timer` to its Brain and StateGraph.

#### `0x61023211` Variant Check

`normalfn` uses `TUNING.DEERCLOPS_HEALTH` and `TUNING.DEERCLOPS_DAMAGE`.
The mutated form uses `TUNING.MUTATED_DEERCLOPS_HEALTH` and planar components.

### `0x61023220` Pet: Critter Eets

`MakeCritter` now always loads basic, emote, and trait assets and adds `sleeper` and `crittertraits`.
`critter_eets` uses `skin_only`, `favoritefood`, and `allow_platform_hopping` while following that shared path.
`SGcritter_common.lua` forwards `oneat` event data into the shared `eat` state.
`SGcritter_eets.lua` uses `QueueStateAfterEat` to select pepper, onion, or mushroom emotes from the food prefab.
It composes the remaining trait, combat, hungry, nuzzle, pet, sleep, and wake states from shared helpers.

## `0x61024110` Assembly Responsibilities

The prefab attaches components and sets initial parameters.
The Brain continuously selects behaviour.
The StateGraph owns states, animation windows, and event reactions.
Components own mutable gameplay state.

### `0x61024111` Fields to Check

Check `SetStateGraph`, `SetBrain`, `SetMaxHealth`, and `SetDefaultDamage`.
Then check `SetRange`, `SetAttackPeriod`, and `SetLootSetupFn`.
When the prefab calls a standard helper, inspect its physics, burning, freezing, and perish side effects.

## `0x61024210` Boss Variants

`deerclops.lua` returns both normal and mutated prefabs.
The mutated form changes more than assets: it also changes health, damage, range, loot, and planar components.

### `0x61024211` Variant Boundary

Search every `Prefab(...)` returned from a boss file.
Reading only the normal prefab misses variant tuning.

## `0x61025100` Verification

~~~bash
rg -n "SetStateGraph|SetBrain|SetMaxHealth|lootdropper|AddComponent\\(\"combat\"\\)" \
  scripts/prefabs/rabbit.lua \
  scripts/prefabs/deerclops.lua

rg -n "SetRetargetFunction|SetKeepTargetFunction|GetAttacked|CanTarget|SuggestTarget" \
  scripts/components/combat.lua

rg -n "SetMaxHealth|SetVal|DoDelta|death" \
  scripts/components/health.lua

rg -n "MakeCharacterPhysics|Make.*BurnableCharacter|Make.*FreezableCharacter" \
  scripts/standardcomponents.lua

rg -n "MakeCritter|critter_eets|SGCritterStates\\.AddEat|QueueStateAfterEat|OnTraitChanged|AddSleepExStates" \
  scripts/prefabs/critters.lua \
  scripts/stategraphs/SGcritter_common.lua \
  scripts/stategraphs/SGcritter_eets.lua
~~~

### `0x61025110` Reading Order

Use `rabbit.lua` to learn the ordinary assembly pattern.
Then inspect `deerclops.lua` and mark its targeting functions, phase logic, and variants.

#### `0x61025111` Minimal Trace

Start at `RetargetFn` in `deerclops.lua`.
Follow `combat:SetTarget`, `combat:DoAttack`, the target's `combat:GetAttacked`, `health:DoDelta`, and the loot path.
