# `0x10020000` Source Snapshot

The tracked `scripts` tree contains 4,045 files, including 4,030 Lua files.

The `scripts` submodule is pinned to `c2d52ec` (build `740477`, authored 2026-07-06).

The latest delta contains no gameplay Lua or path changes; the focus areas below come from the preceding `740256` update.

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
- Compared with parent `3b390612` (build `740256`), `c2d52ec` modifies five files and adds or removes none.
- Those files are the update workflow, three language catalogs, and `scripts/skin_strings.lua`.
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

The preceding `740256` update changed these gameplay clusters:

- Carnival golf spans `scripts/recipes.lua`, `scripts/standardcomponents.lua`, and `scripts/widgets/controls.lua`.
- Its Prefabs use the `carnivalgame_golf` prefix under `scripts/prefabs/`.
- Item temperature enters through `scripts/components/inventoryitem.lua`.
- Its state lives in `scripts/components/inventoryitemtemperature.lua`.
- `scripts/prefabs/desiccant.lua` and `scripts/prefabs/trap_fumarole.lua` consume that temperature state.
- Eets starts in `scripts/prefabs/critters.lua` and `scripts/components/crittertraits.lua`.
- Its states live in `scripts/stategraphs/SGcritter_common.lua` and `scripts/stategraphs/SGcritter_eets.lua`.
- Vault changes touch `scripts/components/vaultroom.lua` and `scripts/prefabs/vault_key_activator.lua`.
- Scrapbook support touches `scripts/screens/redux/scrapbookdata.lua` and `scripts/debugcommands.lua`.
- `scripts/prefabskins.lua` and `scripts/skin_strings.lua` add three `walrushat_minigolf_*` skins.
- `scripts/speech_wx78.lua` and `scripts/languages/strings.pot` correct WX-78's green and blue spoon-lure descriptions.

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
git -C scripts diff --name-status 3b390612..c2d52ec
~~~

### `0x10025111` Next Step

Recount the total and the two largest directories first.

Then decide whether a topic should explain a workflow or list reference coverage.

If the `git ls-files` results change, update the complete inventory in `0x8000-reference`.
