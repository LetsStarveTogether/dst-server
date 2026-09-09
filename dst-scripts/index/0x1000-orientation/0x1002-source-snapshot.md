# `0x10020000` Source Snapshot

The tracked `scripts` tree contains 4,087 files, including 4,072 Lua files.

The `scripts` submodule is pinned to `64f28a7` (build `752118`, authored 2026-09-10).

Compared with `6ea1ee2` (build `747465`), this snapshot adds 46 files, removes four, and modifies 248.

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
- Compared with `6ea1ee2` (build `747465`), the delta contains 30,643 insertions and 5,781 deletions across 298 files.
- The changed files comprise 296 Lua files, `languages/strings.pot`, and `.github/workflows/update.yml`.
- Directory totals guide reading effort but do not replace the reference coverage inventory.

## `0x10024111` Directory Breakdown

| Scope | Lua files | Reading focus |
| --- | ---: | --- |
| `scripts/` root | 222 | Startup, global services, and data entry points |
| `scripts/prefabs/` | 1,610 | The largest entity-assembly area |
| `scripts/components/` | 829 | Primary server-side behaviour state |
| `scripts/stategraphs/` | 264 | Action presentation and animation state machines |
| `scripts/brains/` | 195 | AI decision entry points |
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

Builds `751350` through `752118` change these clusters:

- Virtual rooms move from the removed Vault-specific components into `virtualroommanager`, `virtualroomset`, and `virtualroomteleporter`.
- `world.lua` installs the shared virtual-room manager and `worldstaticlayouts`; `cave.lua` registers the Vault layout.
- Prefab, Brain, and StateGraph updates cover Charlie, bat, and rocky bosses and the Atrium ritual.
- Three new bat-boss cave layouts extend world-generation data.
- `aoeutil.lua` provides shared area-attack and work helpers.
- `feedback.lua`, the feedback screen, and screenshot utilities add a feedback path.
- Entity, combat, customization, and world-settings changes are relevant to native Lua contract checks.

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
git -C scripts diff --shortstat 6ea1ee2..64f28a7
git -C scripts diff --name-status 6ea1ee2..64f28a7
~~~

### `0x10025111` Next Step

Recount the total and the two largest directories first.

Then decide whether a topic should explain a workflow or list reference coverage.

If the `git ls-files` results change, update the complete inventory in `0x8000-reference`.
