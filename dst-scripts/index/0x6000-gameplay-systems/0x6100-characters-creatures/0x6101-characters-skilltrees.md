# `0x61010000` Characters and Skill Trees

A player character combines shared code, character hooks, skill data, and controller components.
Start with `prefabs/player_common.lua`, then inspect the character prefab and its skill tree.

## `0x61011000` Purpose

This guide shows how the player skeleton, character hooks, skill trees, and controls form one runtime entity.
It separates pristine network setup, client prediction, master-simulation components, and skill activation callbacks.

### `0x61011100` Assembly Question

Character files usually specialize `common_postinit` and `master_postinit` instead of rebuilding the player.
Skill data reaches components and events through `skilltreeupdater`.

#### `0x61011110` Reading Path

Start with the prefab factory returned by `MakePlayerCharacter`.
Then confirm that `wilson.lua` supplies character differences.

##### `0x61011111` Authority Facts

`player_common.lua` adds network variables and optimization tags during pristine setup.
The authoritative `combat`, `health`, `hunger`, and `sanity` components exist only when `TheWorld.ismastersim` is true.
The master branch also adds `eater` when the game mode permits eating.
`skilltree_defs.lua` uses `BuildAllData` to load `prefabs/skilltree_*.lua` dynamically.

## `0x61012000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/prefabs/player_common.lua` | `MakePlayerCharacter` | Builds the player prefab factory |
| `scripts/prefabs/wilson.lua` | `MakePlayerCharacter("wilson", ...)` | Shows character-specific hooks |
| `scripts/prefabs/skilltree_defs.lua` | `BuildAllData` | Collects character skill definitions |
| `scripts/skilltreedata.lua` | `SkillTreeData:ActivateSkill` / `ValidateCharacterData` | Stores and validates skill selections |
| `scripts/components/skilltreeupdater.lua` | `SkillTreeUpdater:ActivateSkill` | Applies skills and synchronizes them |
| `scripts/networkclientrpc.lua` | `SetSkillActivatedState` | Resolves skill RPC IDs |

### `0x61012110` `scripts/prefabs/player_common.lua`

This file contains the player entity's shared implementation.
`fn` creates the network entity and returns early on non-master simulations.
The master branch creates `player_classified`, adds authoritative components, and assigns `SGwilson`.

#### `0x61012111` Search Terms

Search for `MakePlayerCharacter`, `entity:SetPristine`, and `TheWorld.ismastersim`.
Also search for `player_classified` and `SetStateGraph("SGwilson")`.

### `0x61012120` `scripts/prefabs/wilson.lua`

This file shows the standard shape of character-specific behaviour.
`common_postinit` adds tags, a `reticule`, and client-visible behaviour.
`master_postinit` adds starting inventory, `beard` behaviour, food affinity, and server events.

#### `0x61012121` Search Terms

Search for `common_postinit`, `master_postinit`, `skilltreeupdater:IsActivated`, and `MakePlayerCharacter("wilson"`.

### `0x61012130` `scripts/prefabs/skilltree_defs.lua`

`BuildAllData` iterates over `SKILLTREE_CHARACTERS`.
For each character, it calls `require("prefabs/skilltree_" .. character)`.
It then fills `SKILLTREE_DEFS`, `SKILLTREE_ORDERS`, and `CUSTOM_FUNCTIONS`.

#### `0x61012131` Search Terms

Search for `SKILLTREE_CHARACTERS`, `BuildAllData`, `CreateSkillTreeFor`, and `DEBUG_REBUILD`.

### `0x61012140` `scripts/components/skilltreeupdater.lua`

`SkillTreeUpdater` delegates selection changes to `SkillTreeData`.
After validation succeeds, `ActivateSkill` selects the client, server, or RPC branch.
`ActivateSkill_Server` then invokes `onactivate`.

#### `0x61012141` Search Terms

Search for `ActivateSkill`, `ValidateCharacterData`, `SetSkillActivatedState`, `OnSave`, and `SendFromSkillTreeBlob`.

## `0x61013000` Runtime Flow

~~~mermaid
flowchart TD
    A["Character prefab"]
    A --> B["require prefabs/player_common"]
    B --> C["MakePlayerCharacter(name, prefabs, assets, hooks)"]
    C --> D["CreateEntity and shared network state"]
    D --> E["common_postinit"]
    E --> F["entity:SetPristine"]
    F --> G{"TheWorld.ismastersim"}
    G -->|false| H["Client entity; local owner later adds playercontroller"]
    G -->|true| I["player_classified and authoritative components"]
    I --> J["master_postinit"]
    J --> K["skilltreeupdater applies skills"]
    K --> L["SGwilson and player components consume results"]
~~~

### `0x61013110` Shared Assembly

Before pristine setup, the factory configures networking, base tags, net variables, and client-visible functions.
Only the post-pristine master branch adds components that can change world state.

#### `0x61013111` Authority Boundary

Do not treat predicted client components as authoritative state.
`playercontroller` exchanges data through `player_classified`, but server components own health, hunger, and combat.

### `0x61013210` Character Hooks

Use `common_postinit` for tags, reticules, and network-visible action hints.
Use `master_postinit` for inventory, beards, listeners, food affinity, and component parameters.

#### `0x61013211` Check

In `wilson.lua`, the reticule is configured in the common hook.
The beard and `foodaffinity` are configured in the master hook.

### `0x61013310` Skill Application

`skilltree_defs.lua` collects skill definitions.
`skilltreedata.lua` validates and stores selections.
`skilltreeupdater.lua` runs callbacks, RPC synchronization, and save restoration.

#### `0x61013311` Consumption Boundary

Do not stop at `skilltree_wilson.lua`.
Find calls to `skilltreeupdater:IsActivated` to see whether a component, action, or prefab hook consumes the skill.

## `0x61014110` Authoritative Player Components

The master branch adds `locomotor`, `combat`, `inventory`, `health`, `hunger`, `sanity`, `builder`, and `eater`.
Together they provide the player's basic world capabilities.

### `0x61014111` Fields to Check

`locomotor` must exist before `SetStateGraph("SGwilson")`.
`combat` uses `TUNING.UNARMED_DAMAGE`, `TUNING.WILSON_ATTACK_PERIOD`, and the default attack range.
`hunger` sets its maximum, depletion rate, and starvation damage rate.

## `0x61014210` Skill Synchronization

`ActivateSkill` first calls `SkillTreeData:ActivateSkill`, which rejects invalid selections.
For an accepted selection, the server invokes `onactivate` and synchronizes the client.
The local player may also run client activation logic to keep frontend state current.

### `0x61014211` Check

Determine whether `onactivate` changes a component.
Otherwise, look for later behaviour behind an `IsActivated` branch.

## `0x61015100` Verification

~~~bash
rg -n "MakePlayerCharacter|entity:SetPristine|TheWorld\\.ismastersim|player_classified|SetStateGraph" \
  scripts/prefabs/player_common.lua

rg -n "common_postinit|master_postinit|skilltreeupdater:IsActivated|MakePlayerCharacter" \
  scripts/prefabs/wilson.lua

rg -n "BuildAllData|SKILLTREE_CHARACTERS|CreateSkillTreeFor|ActivateSkill|ValidateCharacterData" \
  scripts/prefabs/skilltree_defs.lua \
  scripts/skilltreedata.lua \
  scripts/components/skilltreeupdater.lua

rg -n "SetSkillActivatedState|SendFromSkillTreeBlob" \
  scripts/components/skilltreeupdater.lua \
  scripts/networkclientrpc.lua
~~~

### `0x61015110` Reading Order

Read the prefab factory in `player_common.lua`.
Then inspect one character, such as `wilson.lua`.
Finally trace a skill through `skilltree_defs.lua`, its character skill file, `skilltreedata.lua`, and `skilltreeupdater.lua`.

#### `0x61015111` Minimal Trace

Use `wilson_torch_7` as the sample.
In `wilson.lua`, `skilltreeupdater:IsActivated` makes it affect a special right-click action.
This trace covers the character hook, skill data, and player action system.
