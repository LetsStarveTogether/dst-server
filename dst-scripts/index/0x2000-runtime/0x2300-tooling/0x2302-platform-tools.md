# `0x23020000` Platform Tools

This page covers the scripts under `tools/`, user commands, metrics, platform patches, and update-loop utility entries.

`update.lua` is the frame-update loop, not a game updater.

## `0x23021111` Tool Scripts

`scripts/tools/` contains only `generate_worldgenoverride.lua` and `getmissingstrings.lua`.

The first runs through `require 'tools/generate_worldgenoverride'` and writes `worldgenoverride.lua`.

The second calls `TestStrings()` to produce a missing-string report.

## `0x23021211` Runtime Entries

`usercommands.lua` and `builtinusercommands.lua` implement player and administrator commands.

`update.lua` drives component, StateGraph, Brain, frontend, and related per-frame work.

## `0x23022000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/tools/generate_worldgenoverride.lua` | top-level script | Generate `worldgenoverride.lua` |
| `scripts/tools/getmissingstrings.lua` | `TestStrings` | Write `MISSINGSTRINGS.lua` |
| `scripts/usercommands.lua` | `AddUserCommand` | Register a user command |
| `scripts/usercommands.lua` | `RunUserCommand` | Execute a named command with prepared parameters |
| `scripts/builtinusercommands.lua` | `AddUserCommand` | Register `help`, `kick`, and `rollback` |
| `scripts/components/worldvoter.lua` | `OnStartVote` | Start a vote, check permissions, and log failures |
| `scripts/update.lua` | `WallUpdate` | Update wall-clock systems, input, and the frontend |
| `scripts/update.lua` | `Update` | Update simulation while the server is unpaused |
| `scripts/update.lua` | `PostUpdate` | Update emitters and post-update loopers |
| `scripts/stats.lua` | `GetClientMetricsData` | Supply client metrics to C++ and Lua callers |
| `scripts/stats.lua` | `BuildContextTable` | Build the tracking context |
| `scripts/platformpostload.lua` | top-level script | Apply platform-specific post-load changes |

### `0x23022111` Generated Override Anchor

`generate_worldgenoverride.lua` has no `generate` function.

Its top-level code reads `map/customize` and `map/levels`, builds text, and writes the result with `io.open`.

### `0x23022211` User Command Anchor

Find `AddUserCommand`, `RunUserCommand`, and `RunTextUserCommand`.

Trace permission, vote, confirmation, and local-versus-server checks from those entries.

### `0x23022311` Update Anchor

Find `WallUpdate`, `StaticUpdate`, `Update`, `LongUpdate`, and `PostUpdate`.

These are runtime-loop functions, not download or patch operations.

## `0x23023000` Tool and Command Flow

~~~mermaid
flowchart TD
    A["tool script required from console"]
    A --> B["top-level Lua code runs"]
    B --> C["write worldgenoverride.lua or MISSINGSTRINGS.lua"]
    D["user text command"]
    D --> E["RunTextUserCommand"]
    E --> F["parseinput"]
    F --> G["runcommand"]
    H["named command and params"] --> I["RunUserCommand"]
    I --> G
    G --> J["localfn / serverfn / vote"]
~~~

### `0x23023111` Tool Execution

`generate_worldgenoverride.lua` reads map-configuration tables from the active Lua environment.

It writes `worldgenoverride.lua` to the current working directory.

`getmissingstrings.lua` calls `TestStrings()` at top level.

That function loads prefab and character speech data and writes `MISSINGSTRINGS.lua`.

### `0x23023211` User Command Execution

Built-in commands register through `AddUserCommand`.

A command definition can include `localfn`, `serverfn`, permission checks, vote settings, and argument definitions.

`RunTextUserCommand` parses text.

`RunUserCommand` accepts a command name and parameter table.

Both call the local `runcommand` dispatcher.

Commands with `COMMAND_RESULT.ALLOW` queue their permitted functions in `cmdqueue`.

`WallUpdate` drains it through `HandleUserCmdQueue`, while vote commands call `TheNet:StartVote`.

### `0x23023311` Frame Updates

`WallUpdate` continues some UI, audio, and input work even while the server is paused.

`Update` asserts that the server is not paused.

It updates `SGManager` and `BrainManager` once for every unseen simulation tick.

## `0x23024111` `tools/` Inventory

`scripts/tools/` contains two tracked Lua files, both implemented as top-level scripts.

Update this page and the reference inventory if that directory changes.

## `0x23024211` User Command Table

`AddUserCommand(name, data)` registers a command.

`AddModUserCommand(mod, name, data)` namespaces a mod command, and `RemoveUserCommand(name)` removes one.

## `0x23024311` Update Registries

`RegisterStaticComponentUpdate` and `RegisterStaticComponentLongUpdate` register class-level functions.

`Update` and `LongUpdate` traverse those registries.

## `0x23024411` Metrics

`GetClientMetricsData` is a global function available to C++.

`BuildContextTable` reads `TheNet` and `TheWorld`.

It obtains local profile identifiers through `GetClientMetricsData`.

`BuildStartupContextTable` adds platform and branch data and checks `KnownModIndex` for enabled mods.

`SendTrackingStats` is local to `stats.lua` and is not an external call anchor.

## `0x23024511` Platform Post-Load

After loading built-in user commands, `gamelogic.lua` requires `platformpostload`.

On `WIN32_RAIL`, top-level `platformpostload.lua` localizes existing command aliases.

It also removes the `bug` command and replaces the `kick` vote rule.

## `0x23024611` World Votes

A user command can enter a vote instead of immediately running `serverfn`.

`worldvoter.lua` calls `UserCommands.CanUserStartVote` from `OnStartVote`.

Its failure log formats `starteruserid` with `tostring`, so a nil or non-string value cannot break the logging path.

## `0x23025100` Verification

~~~bash
rg -n \
  -e "AddUserCommand" \
  -e "RunUserCommand" \
  -e "RunTextUserCommand" \
  -e "parseinput" \
  -e "runcommand" \
  -e "HandleUserCmdQueue" \
  -e "StartVote" \
  -e "TestStrings" \
  -e "MISSINGSTRINGS" \
  -e "io.open" \
  -e "platformpostload" \
  -e "RailUserCommand" \
  scripts/tools \
  scripts/usercommands.lua \
  scripts/builtinusercommands.lua \
  scripts/components/worldvoter.lua \
  scripts/update.lua \
  scripts/stats.lua \
  scripts/gamelogic.lua \
  scripts/platformpostload.lua
rg -n \
  -e "WallUpdate" \
  -e "StaticUpdate" \
  -e "Update" \
  -e "LongUpdate" \
  -e "PostUpdate" \
  -e "GetClientMetricsData" \
  -e "BuildContextTable" \
  -e "BuildStartupContextTable" \
  scripts/tools \
  scripts/usercommands.lua \
  scripts/builtinusercommands.lua \
  scripts/update.lua \
  scripts/stats.lua
~~~

### `0x23025111` Next Read

Confirm that both files under `tools/` are top-level scripts.

Inspect one command in `builtinusercommands.lua`, then read `update.lua` as a frame loop.
