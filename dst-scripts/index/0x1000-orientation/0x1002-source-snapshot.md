# `0x10020000` Source Snapshot

The tracked `scripts` tree contains 4,045 files, including 4,030 Lua files.

The `scripts` submodule is pinned to `6ea1ee2` (build `747465`, authored 2026-08-13).

Compared with `c2d52ec` (build `740477`), this snapshot modifies 19 existing files without changing the source tree shape.

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
- Compared with `c2d52ec` (build `740477`), `6ea1ee2` modifies 19 files and adds, removes, or renames none.
- The delta contains 979 insertions and 75 deletions across 16 Lua files and three language catalogs.
- Most of the volume is generated account-item data and localization catalogs.
- Directory totals guide reading effort but do not replace the reference coverage inventory.

## `0x10024111` Directory Breakdown

| Scope | Lua files | Reading focus |
| --- | ---: | --- |
| `scripts/` root | 218 | Startup, global services, and data entry points |
| `scripts/prefabs/` | 1,594 | The largest entity-assembly area |
| `scripts/components/` | 821 | Primary server-side behaviour state |
| `scripts/stategraphs/` | 261 | Action presentation and animation state machines |
| `scripts/brains/` | 191 | AI decision entry points |
| `scripts/behaviours/` | 29 | Behaviour-tree nodes |
| `scripts/map/` | 444 | World generation, layouts, and world definitions |
| `scripts/widgets/` | 272 | HUD and UI components |
| `scripts/screens/` | 135 | Front-end screens |
| `scripts/scenarios/` | 50 | Scenario scripts |
| `scripts/util/` | 8 | Small utility modules |
| `scripts/languages/` | 2 | Language-loading utilities |
| `scripts/nis/` | 2 | Cinematic scripts |
| `scripts/tools/` | 2 | Maintenance and export tools |
| `scripts/cameras/` | 1 | Camera Lua implementation |

## `0x10024211` Focus Areas

Build `747465` changes these clusters:

- Release group 184 adds 16 account items, including nine `SEASIDE` outfits and a beach mystery box.
- `waterballoon_insect` gains the complete base-prefab, skin initializer, held-symbol, and equip-event path.
- `body_wathgrithr_ancient` overrides its character-specific upper-arm symbol instead of hiding it.
- New strings cover store login, online/offline world conversion, and premium-online membership.
- They have no direct references elsewhere in tracked Lua.
- `worldroutefollower` clears its current teleport task handle when the callback starts.
- It can then schedule the next virtual route hop.
- Cannonball launches deactivate inventory mines and custom traps before marking them airborne.
- Repaired fumarole shovels use `TUNING.SHOVEL_DAMAGE` rather than axe damage.
- The AoE spider-healing scan excludes `creaturecorpse` targets that have no health component.
- The HUD clock scales its higher-resolution face, rim, and hand textures to `0.5` without changing clock state.

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
git -C scripts diff --shortstat c2d52ec..6ea1ee2
git -C scripts diff --name-status c2d52ec..6ea1ee2
~~~

### `0x10025111` Next Step

Recount the total and the two largest directories first.

Then decide whether a topic should explain a workflow or list reference coverage.

If the `git ls-files` results change, update the complete inventory in `0x8000-reference`.
