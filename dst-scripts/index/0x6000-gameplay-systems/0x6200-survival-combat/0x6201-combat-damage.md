# `0x62010000` Combat and Damage

Read actions, movement, StateGraphs, weapons, `combat`, and `health` as separate stages.
Normal combat damage reaches health through `Health:DoDelta`.
Earlier action and range checks decide whether an attack lands.

## `0x62011000` Purpose

This guide traces one attack from input to health loss.
It separates `ACTIONS.ATTACK`, attack animation states, and authoritative damage resolution.

### `0x62011100` Attack Boundaries

The attack action, movement, animation, `combat`, and `health` each own a separate stage.

#### `0x62011110` Reading Path

Confirm that `ACTIONS.ATTACK.fn` can call `Combat:DoAttack` directly.
Treat the player StateGraph as a common animation path, not the only damage entry point.

##### `0x62011111` Damage Facts

`Combat:DoAttack` resolves the weapon, projectile, AOE, reflected damage, and hit.
The target's `Combat:GetAttacked` handles armor, dodge, damage types, and `Health:DoDelta`.
When health reaches zero, `Health:SetVal` pushes `entity_death` and `death`.

## `0x62012000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/actions.lua` | `ACTIONS.ATTACK.fn` | Executes the attack action |
| `scripts/components/locomotor.lua` | `LocoMotor:PushAction` | Moves into range and resumes the action |
| `scripts/stategraphs/SGwilson.lua` | `ActionHandler(ACTIONS.ATTACK, ...)` | Selects player attack states |
| `scripts/entityscript.lua` | `EntityScript:PerformBufferedAction` | Executes an action at StateGraph timing |
| `scripts/bufferedaction.lua` | `BufferedAction:Do` | Calls the selected action function |
| `scripts/components/combat.lua` | `Combat:DoAttack` | Executes an attack and calculates damage |
| `scripts/components/combat.lua` | `Combat:GetAttacked` | Applies target defenses and damage |
| `scripts/components/health.lua` | `Health:DoDelta` / `Health:DoFireDamage` | Changes health and emits death events |
| `scripts/components/weapon.lua` | `Weapon:GetDamage` | Supplies weapon and special damage |
| `scripts/components/propagator.lua` | `Health:DoFireDamage` call | Applies fire-spread damage |
| `scripts/stategraphs/SGknight.lua` | `DoJoustAoe` | Tracks joust AOE targets and mounts |
| `scripts/prefabs/ruinsnightmare_horn_attack.lua` | `OnUpdate` | Tracks horn attack AOE targets |

### `0x62012110` `scripts/actions.lua`

`ACTIONS.ATTACK.fn` first checks relevant StateGraph tags.
The normal path calls `act.doer.components.combat:DoAttack(act.target)` and reports success.

#### `0x62012111` Search Terms

Search for `ACTIONS.ATTACK.fn`, `propattack`, `thrusting`, `helmsplitting`, and `DoAttack`.

### `0x62012120` `scripts/components/locomotor.lua`

An attack may first enter `LocoMotor:PushAction`.
When the target is too far away, the locomotor uses `GoToEntity` and pushes the buffered action after arrival.

#### `0x62012121` Search Terms

Search for `PushAction`, `GoToEntity`, `PushBufferedAction`, `PreviewBufferedAction`, and `in_cooldown`.

### `0x62012130` `scripts/stategraphs/SGwilson.lua`

The player's attack handler selects a state from the weapon and actor state.
Options include `attack`, `slingshot_shoot`, `blowdart`, and `throw`.
These states control animation timing and local combo behaviour.

#### `0x62012131` Search Terms

Search for `ActionHandler(ACTIONS.ATTACK`, `abouttoattack`, `slingshot_shoot`, `blowdart`, and `throw`.

### `0x62012140` `scripts/components/combat.lua`

`combat.lua` uses `DoAttack` to resolve weapons, projectiles, stimuli, AOE, reflected damage, and the hit.
The target's `GetAttacked` then resolves defense, armor, dodge, planar damage, and special damage.

#### `0x62012141` Search Terms

Search for `DoAttack`, `CalcDamage`, `CalcReflectedDamage`, `GetAttacked`, `onattackother`, and `onhitother`.

### `0x62012150` `scripts/components/health.lua`

`Health:DoDelta` is the normal combat-delta entry point, not the only health writer.
`Health:SetVal` assigns the value and pushes the world-level `entity_death` and entity-level `death` events.
`Health:SetCurrentHealth` and `Health:SetMaxHealth` also write current health directly.

#### `0x62012151` Search Terms

Search for `DoDelta`, `SetVal`, `entity_death`, `death`, `healthdelta`, and `CanFadeOut`.

### `0x62012160` Direct Fire Damage

`Health:DoFireDamage` is a direct non-weapon health entry point.
Callers such as `components/burnable.lua` and `components/propagator.lua` bypass `ACTIONS.ATTACK` and `Combat:DoAttack`.

#### `0x62012161` Search Terms

Search for `DoFireDamage`, `SMOTHER_DAMAGE`, and `heatoutput`.

## `0x62013000` Runtime Flow

~~~mermaid
flowchart TD
    A["ACTIONS.ATTACK"]
    A --> B{"In range?"}
    B -->|no| C["LocoMotor:GoToEntity"]
    C --> D["PushBufferedAction"]
    B -->|yes| D
    D --> E["SGwilson attack state"]
    E --> M["EntityScript:PerformBufferedAction"]
    M --> N["BufferedAction:Do"]
    N --> F["ACTIONS.ATTACK.fn"]
    F --> G["Combat:DoAttack"]
    G --> H["Weapon:GetDamage / projectile / AOE"]
    H --> I["Target Combat:GetAttacked"]
    I --> J["Armor / dodge / damage type / planar"]
    J --> K["Health:DoDelta"]
    K --> L["healthdelta / death / entity_death"]
~~~

### `0x62013110` Action and Movement

Attacks do not always execute immediately.
When proximity is required, `locomotor` stores the buffered action and moves within execution range.

#### `0x62013111` Failure Boundary

Check range, target validity, and cooldown before investigating `Health:DoDelta`.

### `0x62013210` StateGraph and Combat

Player attacks commonly enter an animation state through the StateGraph.
State tags affect prediction, combos, ranged weapons, and special attack branches.

#### `0x62013211` Entry-Point Boundary

Non-player entities and projectiles can call `Combat:DoAttack` or `Combat:GetAttacked` directly.
The StateGraph is therefore not the only path into damage resolution.

### `0x62013310` Attacker and Target Responsibilities

The attacker's `DoAttack` calculates outgoing damage and calls the target's `combat:GetAttacked`.
`GetAttacked` resolves defense, armor, dodge, and special damage.
`health:DoDelta` applies the final health change.

#### `0x62013311` Checks

`GetAttacked` calls `health:DoDelta(-damage, ...)`.
`DoDelta` pushes `healthdelta`.
`SetVal` pushes `entity_death` and `death` when the target dies.

### `0x62013410` AOE Target Tracking

Some AOE attacks store hit entities in a `targets` table to prevent duplicate hits during one scan.
The joust AOE in `SGknight.lua` and `ruinsnightmare_horn_attack.lua` also track mounted targets.
Check both rider and ordinary target branches to confirm whether the table stores the hit entity or its mount.

## `0x62014110` Weapon Damage

`weapon.lua` uses `Weapon:SetDamage` to store base damage.
`Weapon:GetDamage` supports function-based damage, `damagetypebonus`, and special damage tables.
`Weapon:OnAttack` runs the weapon's post-hit callback.

### `0x62014111` Samples

Use `spear.lua` for fixed damage.
Use `hambat.lua` for `SetOnAttack(UpdateDamage)`.
Use `slingshotammo.lua` for projectile and special-ammunition branches.

## `0x62014210` Death and Loot Boundary

`health.lua` does not decide all loot.
Prefabs or `lootdropper` usually react to death state or events.
`LootDropper:ClearChanceLoot()` clears chance loot.
`houndbone.lua` stores its appearance in `bonetype` and rebuilds chance loot when that type changes.

### `0x62014211` Check

To explain a drop, inspect the prefab's `lootdropper` setup.
`Health:SetVal` emits death state and events; it is not the loot execution point.

## `0x62015100` Verification

~~~bash
rg -n "ACTIONS\\.ATTACK\\.fn|DoAttack|propattack|thrusting|helmsplitting" \
  scripts/actions.lua

rg -n "PushAction|GoToEntity|PushBufferedAction|PreviewBufferedAction|in_cooldown" \
  scripts/components/locomotor.lua

rg -n "ActionHandler\\(ACTIONS\\.ATTACK|slingshot_shoot|blowdart|throw|abouttoattack" \
  scripts/stategraphs/SGwilson.lua

rg -n "PerformBufferedAction|BufferedAction:Do|self\\.action\\.fn" \
  scripts/entityscript.lua \
  scripts/bufferedaction.lua

rg -n "DoAttack|CalcDamage|GetAttacked|CalcReflectedDamage|onhitother|onattackother" \
  scripts/components/combat.lua

rg -n "SetDamage|GetDamage|OnAttack|LaunchProjectile" \
  scripts/components/weapon.lua \
  scripts/prefabs/spear.lua \
  scripts/prefabs/hambat.lua \
  scripts/prefabs/slingshotammo.lua

rg -n "DoDelta|SetVal|entity_death|healthdelta|death" \
  scripts/components/health.lua

rg -n "DoFireDamage|SMOTHER_DAMAGE|heatoutput" \
  scripts/components/health.lua \
  scripts/components/burnable.lua \
  scripts/components/propagator.lua

rg -n "ClearChanceLoot|bonetype" \
  scripts/components/lootdropper.lua \
  scripts/prefabs/houndbone.lua

rg -n "DoJoustAoe|ruinsnightmare_horn_attack|components\\.rider|targets\\[" \
  scripts/stategraphs/SGknight.lua \
  scripts/prefabs/ruinsnightmare_horn_attack.lua
~~~

### `0x62015110` Reading Order

Read the `SGwilson.lua` action handler and follow its state to `EntityScript:PerformBufferedAction` and `BufferedAction:Do`.
Continue through `ACTIONS.ATTACK.fn`, `Combat:DoAttack`, and `Combat:GetAttacked`.
Finish in `health.lua` to verify the health change and death events.

#### `0x62015111` Minimal Trace

Use a spear attack.
Follow `SetDamage` in `spear.lua` through `Combat:CalcDamage`, the target's `Combat:GetAttacked`, and `Health:DoDelta`.
