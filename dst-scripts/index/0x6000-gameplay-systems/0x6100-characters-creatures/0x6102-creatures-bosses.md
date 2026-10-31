# `0x61020000` Creatures and Bosses

Creatures and bosses are both entities assembled by prefabs.
Their differences come from component parameters, Brain complexity, StateGraphs, phase events, variants, and loot logic.

## `0x61021000` Purpose

This guide compares `rabbit.lua` with `deerclops.lua` to establish a reusable source-reading pattern.
The Charlie and Shrouden encounter then shows how a boss transition crosses prefabs, StateGraphs, and an arena owner.

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
| `scripts/prefabs/charlie_boss_trial.lua` | `TrackCharlieBoss` / `SpawnExitCrack` | Owns the encounter, hazards, boss replacement, and victory effects |
| `scripts/stategraphs/SGcharlie_boss.lua` | `death_spawn_shrouden` / `track_charlie_boss` | Spawns Shrouden and transfers arena tracking |
| `scripts/prefabs/shrouden.lua` | `TryReset` / `OnSave` | Assembles the boss and restores its encounter state |
| `scripts/brains/shroudenbrain.lua` | `ShroudenBrain:OnStart` | Chooses chase, distant attacks, and devoured-target behavior |
| `scripts/stategraphs/SGshrouden.lua` | `death_final` / `ms_charlie_boss_defeated` | Completes the encounter after the final death sequence |
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

### `0x61023230` Arena Encounter: Charlie and Shrouden

`charlie_boss_trial.lua` creates `charlie_boss` and tracks it through `TrackCharlieBoss`.
In `SGcharlie_boss`, the death and transformation states lead to `death_spawn_shrouden`.
That state's timeout spawns `shrouden`, transfers the AOE tag set, and pushes `track_charlie_boss` inside the arena.
The saved `looted` flag lets a restored Charlie boss skip directly to that spawn state.
The arena then follows the new boss through the same tracking callbacks.
`shrouden.lua` installs `shroudenbrain`, `SGshrouden`, retargeting, and phase-dependent attacks.
Its Brain handles chasing, `dofarattack`, and facing devoured players; its StateGraph performs the attacks.

`SGshrouden` emits `ms_charlie_boss_defeated` when `death_final` times out inside the arena.
The arena callback pushes `resetvault` and `ms_defeated_shrouden`, synchronizes Charlie defeat, and opens `charliearena_teleporter`.
The arena therefore completes after the final StateGraph sequence rather than at the initial health-zero event.
The gate creates `king_cane` and `queen_torch` during ritual arena entry, before boss defeat.
It also creates `mask_charlie_bosshat` while Charlie has not yet been defeated.
See [Items and Crafting](../0x6300-items-crafting-events/0x6301-items-crafting.md) for those items.

Both boss prefabs use `TryReset` after losing targets.
The ordinary reset waits until the Atrium room set has no players, then restores health and the first phase.
Both bosses return to `spawn_loop`.
While a disengage reset is pending, their retarget functions can consider players across the arena.
Shrouden saves `looted` to distinguish its two death-loot stages and `changearena` to restore an arena transition.
The latter flag lives in `sg.mem`, and `OnLoadPostPass` schedules the arena change after load.
Room and Vault reset rules are covered in [Caves, Ocean, and Ruins](../../0x5000-world-simulation/0x5200-world-state/0x5202-caves-ocean-ruins.md).

### `0x61023240` Hallowed Nights Creature Forms

The event transforms existing creature prefabs and swaps their Brains, targeting, health, movement, and loot settings.
Their StateGraphs remain the shared pig, merm, and bunnyman graphs, which branch on `franken`, `mummy`, or `vampire`.

| Prefab source | Transformation | Brain source | StateGraph source |
| --- | --- | --- | --- |
| `scripts/prefabs/pigman.lua` | `MakeFrankenPig` | `scripts/brains/frankenpigbrain.lua` | `scripts/stategraphs/SGpig.lua` |
| `scripts/prefabs/merm.lua` | `MakeMummyMerm` | `scripts/brains/mummymermbrain.lua` | `scripts/stategraphs/SGmerm.lua` |
| `scripts/prefabs/bunnyman.lua` | `MakeVampireBunny` | `scripts/brains/vampirebunnybrain.lua` | `scripts/stategraphs/SGbunnyman.lua` |

Each Brain pauses its decision subtree while the SG has `jumping`.
It then chooses walking or running `ChaseAndAttack` branches before wandering.
The transformations disable trading and clear the follower's leader, changing the usual creature alliance rules.
Follow the corresponding house prefabs for spawn and home behavior.
`OnPreLoad` restores saved forms according to the event switch.
`NAUGHTY_VALUE` in `constants.lua` uses functions for pigmen and bunnymen.
Frankenpigs, werepigs, and vampire bunnies contribute zero through those entries.
`components/kramped.lua` calls `OnNaughtyAction` only for a positive result.

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

rg -n -e "TrackCharlieBoss|death_spawn_shrouden|track_charlie_boss|ms_charlie_boss_defeated" \
  -e "TryReset|looted|changearena|spawn_loop" \
  scripts/prefabs/charlie_boss_trial.lua \
  scripts/prefabs/charlie_boss.lua \
  scripts/prefabs/shrouden.lua \
  scripts/stategraphs/SGcharlie_boss.lua \
  scripts/stategraphs/SGshrouden.lua

rg -n "ShroudenBrain:OnStart|dofarattack|targetdevoured|SpawnShroudenLootSetPiece|ms_defeated_shrouden|SpawnExitCrack" \
  scripts/brains/shroudenbrain.lua \
  scripts/prefabs/atrium_gate.lua \
  scripts/prefabs/charlie_boss_trial.lua

rg -n "MakeCritter|critter_eets|SGCritterStates\\.AddEat|QueueStateAfterEat|OnTraitChanged|AddSleepExStates" \
  scripts/prefabs/critters.lua \
  scripts/stategraphs/SGcritter_common.lua \
  scripts/stategraphs/SGcritter_eets.lua

rg -n "MakeFrankenPig|MakeMummyMerm|MakeVampireBunny|HN_MONSTER|OnPreLoad|SetBrain" \
  scripts/prefabs/pigman.lua scripts/prefabs/merm.lua scripts/prefabs/bunnyman.lua \
  scripts/prefabs/pighouse.lua scripts/prefabs/mermhouse.lua scripts/prefabs/rabbithouse.lua

rg -n "OnStart|jumping|hn_walk_t0|ChaseAndAttack|GoHomeAction" \
  scripts/brains/frankenpigbrain.lua scripts/brains/mummymermbrain.lua scripts/brains/vampirebunnybrain.lua

rg -n "NAUGHTY_VALUE|naughty_val|franken|vampire" scripts/constants.lua scripts/components/kramped.lua
~~~

### `0x61025110` Reading Order

Use `rabbit.lua` to learn the ordinary assembly pattern.
Then inspect `deerclops.lua` and mark its targeting functions, phase logic, and variants.

#### `0x61025111` Minimal Trace

Start at `RetargetFn` in `deerclops.lua`.
Follow `combat:SetTarget` and `combat:DoAttack` to the target's `combat:GetAttacked_Internal`.
Continue through `health:DoDelta` and the loot path.
