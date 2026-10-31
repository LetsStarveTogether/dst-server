# `0x10020000` Source Snapshot

The tracked `scripts` tree contains 4,110 files, including 4,095 Lua files.

The `scripts` submodule is at commit `fc1c3e866aebabfe7b528dac68d279368410d34c`.

Its commit records build `761239`, authored 2026-10-30.

## `0x10021111` Purpose

Use the repository's scale to choose between a runtime reading path and a reference inventory.

All counts on this page use `git ls-files --recurse-submodules scripts`.

## `0x10022000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `LoadScript` / `RunScript` | Caches and executes runtime scripts |
| `scripts/worldgen_main.lua` | `LoadScript` / `RunScript` / `GenerateNew` | Caches world-generation scripts and starts generation |
| `scripts/prefabs.lua` | `Prefab = Class` | Defines Prefab objects |
| `scripts/entityscript.lua` | `AddComponent` | Attaches components |

### `0x10022111` Primary Inspection

Search `scripts/mainfunctions.lua` for `LoadScript` and `RunScript`.

Search for the same names in `scripts/worldgen_main.lua` to confirm that world generation has its own loading context.

## `0x10023000` Coverage Workflow

Count tracked files first, use directory totals to choose a runtime topic, and leave exhaustive lists to the reference section.

### `0x10023111` Counting Scope

- The tracked total includes 15 non-Lua files.
- They are `scripts/.github/workflows/update.yml`, `scripts/controller.vdf`, and 13 files under `scripts/languages/`.
- Directory totals guide reading effort but do not replace the reference coverage inventory.

## `0x10024111` Directory Breakdown

| Scope | Lua files | Reading focus |
| --- | ---: | --- |
| `scripts/` root | 224 | Startup, global services, and data entry points |
| `scripts/prefabs/` | 1,621 | The largest entity-assembly area |
| `scripts/components/` | 835 | Entity behaviour and authoritative state |
| `scripts/stategraphs/` | 264 | Action presentation and animation state machines |
| `scripts/brains/` | 199 | AI decision entry points |
| `scripts/behaviours/` | 29 | Behaviour-tree nodes |
| `scripts/map/` | 447 | World generation, layouts, and world definitions |
| `scripts/widgets/` | 274 | HUD and UI components |
| `scripts/screens/` | 136 | Front-end screens |
| `scripts/scenarios/` | 50 | Scenario scripts |
| `scripts/util/` | 9 | Small utility modules |
| `scripts/languages/` | 2 | Language-loading utilities |
| `scripts/nis/` | 2 | Cinematic scripts |
| `scripts/tools/` | 2 | Maintenance and export tools |
| `scripts/cameras/` | 1 | Camera Lua implementation |

## `0x10024211` Focus Areas

- `leafpilemanager`, `leafpile`, `sweepable`, and `sweeper` connect Hallowed Nights spawning to the `SWEEP` action.
- `witchbroom` and `pumpkin_bomb` add item paths through action handling, durability, and explosive knockback.
- Frankenswine, Mermummy, and Vamphare have new Brains and reuse the pig, merm, and bunnyman StateGraphs.
- `commonstates.lua` centralizes creature knockback; `SGwilson` adds sweeping and enchanted broom movement states.
- Charlie's death sequence branches on prior loot state and transfers arena attack filters to Shrouden.
- Shrouden persists arena transitions and controls standby, combat, and low-health music separately.
- Virtual-room placement checks collision bounds and provides recovery points for caught items or players.
- `itemmimicconfig` lets an item force a mimic reveal for selected actions.
- Runtime world settings use `rare` / `often` for population overrides and add four resource handlers.
- `versioning_prefabs.lua` adds the Prefab subversion to the game-and-skin display version.
- Skin groups 186 and 187 add chess trails and ritual-reward skins; Scrapbook exports need comparison with current Prefabs.

## `0x10025100` Verification

Run these commands from `dst-scripts`.

~~~bash
git ls-files --recurse-submodules scripts | wc -l
git ls-files --recurse-submodules scripts | rg "\.lua$" | wc -l
git ls-files --recurse-submodules scripts | rg -v "\.lua$"
git ls-files --recurse-submodules scripts/prefabs | rg "\.lua$" | wc -l
git ls-files --recurse-submodules scripts/components | rg "\.lua$" | wc -l
git -C scripts rev-parse --short=7 HEAD
git -C scripts log -1 --format='%as %s'
~~~

### `0x10025111` Next Step

Recount the total and the two largest directories first.

Then decide whether a topic should explain a workflow or list reference coverage.

If the `git ls-files` results change, update the complete inventory in `0x8000-reference`.
