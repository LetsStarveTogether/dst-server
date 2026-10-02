# `0x72040000` Tools and Debugging

Console commands, debug helpers, keys, rendering, and maintenance scripts verify source-level conclusions.
They are diagnostic entry points, not the main gameplay path.

## `0x72041111` Purpose

`DebugSpawn` is defined in `scripts/util.lua`.
`debughelpers.lua` provides `DumpEntity`, `DumpComponent`, and `DumpUpvalues` for inspection.

## `0x72042000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/util.lua` | `DebugSpawn` | Spawns a prefab at the cursor |
| `scripts/consolecommands.lua` | `c_` | Console commands |
| `scripts/consolecommands.lua` | `c_spawn` / `c_give` | Builds reproductions around `DebugSpawn` |
| `scripts/debugcommands.lua` | `d_` | Scenario-oriented debug and export commands |
| `scripts/debughelpers.lua` | `DumpEntity` | Entity and component inspection |
| `scripts/debugtools.lua` | `DebugArcAttackHitBox` | Debug-render helpers |
| `scripts/debugkeys.lua` | `AddGameDebugKey` | Shortcuts enabled by `CHEATS_ENABLED` |
| `scripts/tools/getmissingstrings.lua` | `GetMissingPrefabStrings` | Text coverage tool |
| `scripts/tools/generate_worldgenoverride.lua` | `worldgen` | World-settings export tool |
| `scripts/feedback.lua` | `StartFeedback` | Opens feedback after screenshot capture |
| `scripts/poc_feedback_stack.lua` | `POC` | Manual feedback-call diagnostic module |

### `0x72042111` Spawn Anchor

Find `function DebugSpawn` in `scripts/util.lua`.
Then inspect `c_spawn` and `c_give` in `consolecommands.lua` to see how they call it.

### `0x72042211` Inspection Anchor

`debughelpers.lua` is a small module centered on `DumpComponent`, `DumpEntity`, and `DumpUpvalues`.
`main.lua` requires it during startup for entity inspection, not entity spawning.

### `0x72042311` Maintenance Scripts

`scripts/tools/getmissingstrings.lua` adjusts `package.path`, loads scripts, and checks for missing text.
`scripts/tools/generate_worldgenoverride.lua` runs inside the game console.
Load it with `require 'tools/generate_worldgenoverride'`.
Both are maintenance or export tools rather than persistent runtime systems.

## `0x72043000` Debug Flow

~~~mermaid
flowchart TD
    A["main.lua requires consolecommands"]
    A --> B["CHEATS_ENABLED requires debugcommands / debugkeys"]
    C["console c_spawn / c_give"]
    C --> D["util.lua DebugSpawn"]
    D --> E["SpawnPrefab at ConsoleWorldPosition"]
    F["debughelpers DumpEntity"]
    F --> G["inspect entity / components / upvalues"]
    H["debugtools DebugRender"]
    H --> I["visualize hitbox / path / state"]
    J["tools/*.lua"]
    J --> K["offline or console maintenance output"]
~~~

### `0x72043111` Console Commands

`c_spawn(prefab)` lowercases the prefab name and calls `DebugSpawn(prefab)`.
It selects the new entity with `SetDebugEntity(inst)`.
`c_give(prefab)` also spawns an entity, then gives it to the player's inventory.

### `0x72043211` Debug Commands

`debugcommands.lua` defines many `d_` functions.
`d_createscrapbookdata()`, `d_scanlayout()`, and `d_testsound()` process runtime observations.
After `d_scanlayout()` selects an area, `d_scanlayout_export(filename)` writes a TMX file.
The file contains tiles and scannable entities.
`_scanlayout_is_scannable` requires `ent.prefab` and excludes `ThePlayer`.
It also rejects entities tagged `locomotor`, `NOCLICK`, `FX`, `DECOR`, or `placer`.
Finite-use calculations in `d_createscrapbookdata()` ignore two actions.
They are `ACTIONS.REMOVELUNARBUILDUP` and `ACTIONS.TERRAFORM_REMOVE`.
This keeps special lunar-buildup and golf-terrain removals out of ordinary Scrapbook tool durability.
The generator exempts `worm_boss_shadow` from the usual unlockability filter.
Its floating-animation override excludes delayed projectiles such as `king_cane`.
`d_spawnitemswithskins(releasegroup)` groups skins through `GetPrefabSkinsForReleaseGroup`.
It defaults to `require("versioning_skins").VERSION` and requires a local player's user ID.
It spawns base items beside their skins, skips boats and characters, and removes samples without `AnimState`.

### `0x72043311` Debug Keys

`main.lua` requires `debugcommands.lua` and `debugkeys.lua` only when `CHEATS_ENABLED` is true.
Their shortcuts and some `d_` commands are therefore absent from normal runtime paths.

### `0x72043411` Tool Outputs

`generate_worldgenoverride.lua` writes `worldgenoverride.lua`.
`getmissingstrings.lua` checks string coverage.
Audit each script's input tables, output files, and dependency on the in-game environment.

### `0x72043511` Feedback Diagnostics

`feedback.StartFeedback` waits for a screenshot before opening `screens/redux/feedbackscreen.lua`.
It returns early when a screenshot is pending or the frontend already has an error widget.
`FeedbackScreen:SendFeedback` calls the engine's `TheSim:CreateFeedback` method.
`poc_feedback_stack.lua` is a separate manual diagnostic with no callers elsewhere in the tracked Lua source.
Its exported functions call the same engine method, so running them can submit feedback.
The module comments describe native call-stack checks and cooldowns whose implementation is outside this Lua snapshot.
Read those statements as unverified native claims; the source queries below only inspect the Lua paths.

## `0x72044111` Tool Boundaries

`c_spawn`, `c_give`, `c_select`, `c_find`, and `c_sounddebug` reproduce or select state.
Use `debughelpers.lua`, `debugtools.lua`, and `debugkeys.lua` for inspection, and `scripts/tools` for maintenance output.

## `0x72045100` Verification

~~~bash
rg -n "function DebugSpawn|ConsoleWorldPosition|SpawnPrefab" \
  scripts/util.lua

rg -n "function c_spawn|function c_give|function c_select|function c_sounddebug|DebugSpawn" \
  scripts/consolecommands.lua

rg -n \
  -e "function d_createscrapbookdata|function d_scanlayout" \
  -e "function d_scanlayout_export|function d_testsound|function d_require" \
  scripts/debugcommands.lua

rg -n "REMOVELUNARBUILDUP|TERRAFORM_REMOVE|finiteuses|scrapbook_finiteuses_useamount_modifiers" \
  scripts/debugcommands.lua

rg -n "d_spawnitemswithskins|GetPrefabSkinsForReleaseGroup|versioning_skins|worm_boss_shadow|projectiledelay" \
  scripts/debugcommands.lua scripts/skinsutils.lua

rg -n "DumpEntity|DumpComponent|DumpUpvalues|DebugArcAttackHitBox|AddGameDebugKey" \
  scripts/debughelpers.lua \
  scripts/debugtools.lua \
  scripts/debugkeys.lua

rg -n "package.path|generate_worldgenoverride|GetWorldSettingsOptions|GetMissing" \
  scripts/tools

rg -n "StartFeedback|SendFeedback|CreateFeedback|function POC" \
  scripts/feedback.lua scripts/screens/redux/feedbackscreen.lua scripts/poc_feedback_stack.lua
~~~

### `0x72045111` Minimal Trace

Use `c_spawn` to trace `consolecommands.lua -> DebugSpawn -> ConsoleWorldPosition -> SpawnPrefab`.
Use `DumpEntity` to inspect the result without confusing the inspection helper with the spawn path.
