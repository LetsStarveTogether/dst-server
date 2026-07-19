# `0x21020000` Main Loop

`scripts/update.lua` defines the Lua-visible update loop.

The scheduler, components, StateGraphs, and Brains are top-level phases within `Update(dt)`.

`Update(dt)` invokes them in that order.

## `0x21021111` Purpose

This page records the update order for normal, static, paused, and post-update ticks.

A paused call to `Update(dt)` is rejected by an assertion.

`StaticUpdate(dt)` continues the static scheduler and paused static components.

## `0x21022000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/update.lua` | `Update` | Main Lua update for an unpaused game tick |
| `scripts/update.lua` | `StaticUpdate` | Static ticks, paused static components, and static StateGraph events |
| `scripts/update.lua` | `WallUpdate` / `PostPhysicsWallUpdate` | Wall-time systems and post-physics platform work |
| `scripts/update.lua` | `PostUpdate` | Emitter and `UpdateLooper_PostUpdate` phase |
| `scripts/scheduler.lua` | `RunScheduler` | Normal scheduler `OnTick` and `Run` |
| `scripts/scheduler.lua` | `RunStaticScheduler` | Static scheduler `OnTick` and `Run` |
| `scripts/stategraph.lua` | `StateGraphWrangler:Update` | StateGraph tick updates and event processing |
| `scripts/brain.lua` | `BrainWrangler:Update` | Brain tick updates and sleep-tick management |

### `0x21022111` Primary Search

Find `function Update`.

In the same file, inspect `RunScheduler`, component `OnUpdate`, `SGManager:Update`, and `BrainManager:Update` order.

## `0x21023000` Runtime Flow

~~~mermaid
flowchart TD
    Q["engine wall tick"] --> R["WallUpdate(dt)"]
    R --> S["queues, wall components, audio, camera, input, frontend"]
    A["engine tick"] --> B["update.lua: Update(dt)"]
    B --> C["RunScheduler for each unseen tick"]
    C --> D["StaticComponentUpdates"]
    D --> E["component OnUpdate(dt)"]
    E --> F["SGManager:Update(i)"]
    F --> G["BrainManager:Update(i)"]
    H["engine static tick"] --> I["StaticUpdate(dt)"]
    I --> J["RunStaticScheduler for each unseen static tick"]
    J --> K["TickRPCQueue"]
    K --> L["paused static component OnStaticUpdate"]
    L --> M["SGManager:UpdateEvents when paused"]
    N["engine post update"] --> O["PostUpdate(dt)"]
    O --> P["EmitterManager and UpdateLooper_PostUpdate"]
    T["post-physics wall tick"] --> U["PostPhysicsWallUpdate(dt)"]
    U --> V["walkableplatformmanager:PostUpdate(dt)"]
~~~

`WallUpdate(dt)` runs on wall time and drains RPC and user-command queues.

It then updates wall components, audio, the camera, input, and the frontend.

`PostPhysicsWallUpdate(dt)` forwards to the world's walkable-platform manager when it exists.

### `0x21023111` Normal Tick

`Update(dt)` starts with class-tracking and demo-timeout checks.

It then asserts that the server is not paused before reading the simulation tick.

It uses `TheSim:GetTick()` and `last_tick_seen` to run every unprocessed tick through `RunScheduler(i)`.

Component `OnUpdate(dt)` runs after the scheduler and before StateGraphs and Brains.

### `0x21023121` StateGraphs and Brains

`SGManager:Update(i)` and `BrainManager:Update(i)` run at the end of the tick loop in `update.lua`.

`RunScheduler()` does not call either manager.

### `0x21023131` Static Tick

`StaticUpdate(dt)` finds missed static ticks with `TheSim:GetStaticTick()` and `last_static_tick_seen`.

It runs each one through `RunStaticScheduler(i)`.

It calls static component `OnStaticUpdate(0)` only while `TheNet:IsServerPaused()` is true.

The same condition enables `SGManager:UpdateEvents()`.

It does not call `SGManager:Update()` while paused.

## `0x21024111` Tick Tracking

`last_tick_seen` and `last_static_tick_seen` prevent duplicate work.

If the engine advances several ticks at once, scheduler, StateGraph, and Brain work catches up per unseen tick.

`StaticComponentUpdates` callbacks and entity component `OnUpdate(dt)` still run once per `Update(dt)` call.

## `0x21024121` Update Sets

Components use `UpdatingEnts`, `NewUpdatingEnts`, and `StopUpdatingComponents`.

StateGraphs use `SGManager` collections such as `updaters`, `tickwaiters`, and `haveEvents`.

Brains use `BrainManager` collections such as `updaters`, `tickwaiters`, and `_safe_updaters`.

## `0x21025100` Verification

~~~bash
rg -n \
  -e "function Update" \
  -e "function WallUpdate" \
  -e "function StaticUpdate" \
  -e "function PostPhysicsWallUpdate" \
  -e "RunScheduler" \
  -e "RunStaticScheduler" \
  -e "OnUpdate\\(dt\\)" \
  -e "SGManager:Update" \
  -e "BrainManager:Update" \
  -e "GetTickTime" \
  scripts/update.lua \
  scripts/scheduler.lua \
  scripts/stategraph.lua \
  scripts/brain.lua \
  scripts/mainfunctions.lua
~~~

### `0x21025111` Next Read

Start with `WallUpdate(dt)`, then read `Update(dt)` and `StaticUpdate(dt)`.

Then inspect sleep, wake, and updater collections in `scheduler.lua`, `stategraph.lua`, and `brain.lua`.
