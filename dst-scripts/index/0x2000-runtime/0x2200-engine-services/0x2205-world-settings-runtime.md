# `0x22050000` World Settings Runtime

World-generation overrides enter through `worldsettings_overrides.lua` and `worldsettingsutil.lua`.

The `worldsettings` and `worldsettingstimer` components hold runtime state.

## `0x22051111` Purpose

`gamelogic.lua` loads `worldsettings_overrides`.

It applies `WorldSettings_Overrides.Pre` before spawning the world prefab.

It restores the world and network entities, then applies `WorldSettings_Overrides.Post`.

The remaining saved entities load afterward.

Some Post overrides push `ms_setworldsetting`.

`components/worldsettings.lua` records the event through `WorldSettings:SetSetting`.

Shard updates use `Shard_SyncWorldSettings` and the `SyncWorldSettings` RPC handler.

## `0x22052000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/gamelogic.lua` | `require("worldsettings_overrides")` | Load runtime world-setting overrides |
| `scripts/gamelogic.lua` | `WorldSettings_Overrides.Pre` | Apply overrides before the world prefab spawns |
| `scripts/gamelogic.lua` | `WorldSettings_Overrides.Post` | Apply overrides before saved entities load |
| `scripts/worldsettings_overrides.lua` | `Pre` / `Post` / `Sync` | Export load and shard-sync override tables |
| `scripts/worldsettings_overrides.lua` | `ms_setworldsetting` | Synchronize a setting to the world component |
| `scripts/shardnetworking.lua` | `Shard_SyncWorldSettings` | Send master overrides through a shard RPC |
| `scripts/networkclientrpc.lua` | `SyncWorldSettings` | Apply a Sync override or fall back to Pre and Post |
| `scripts/components/worldsettings.lua` | `WorldSettings:SetSetting` | Store the current value of a world setting |
| `scripts/worldsettingsutil.lua` | `WorldSettings_ChildSpawner_PreLoad` | Migrate child-spawner timing |
| `scripts/worldsettingsutil.lua` | `WorldSettings_Spawner_SpawnDelay` | Drive a spawner through `worldsettingstimer` |
| `scripts/worldsettingsutil.lua` | `WorldSettings_Pickable_RegenTime` | Drive pickable regeneration |
| `scripts/components/worldsettingstimer.lua` | `WorldSettingsTimer` | Persist setting-driven timers |
| `scripts/components/worldsettingstimer.lua` | `LongUpdate` | Advance timers across offline time or long frames |

## `0x22053000` Runtime Flow

~~~mermaid
flowchart TD
    A["gamelogic loads savedata"]
    A --> B["WorldSettings_Overrides.Pre"]
    B --> C["spawn world prefab"]
    C --> D["restore world and network persistence"]
    D --> E["WorldSettings_Overrides.Post"]
    E --> F["some Post overrides push ms_setworldsetting"]
    F --> G["WorldSettings:SetSetting"]
    E --> H["restore saved entities"]
    H --> I["prefab OnPreLoad helpers"]
    I --> J["worldsettingstimer migration"]
    K["Shard_SyncWorldSettings"] --> L["SHARD_RPC SyncWorldSettings"]
    L --> M["networkclientrpc handler"]
    M --> N{"Sync override exists?"}
    N -->|yes| O["WorldSettings_Overrides.Sync"]
    N -->|no| P["matching Pre and Post overrides"]
~~~

### `0x22053111` Pre Phase

The Pre phase runs after world generation but before the world prefab spawns.

It can change configuration needed during world initialization.

Start at the loop over `WorldSettings_Overrides.Pre` in `gamelogic.lua`.

### `0x22053211` Post Phase

The Post phase follows the world prefab and its world and shard-network persistence.

It runs before the saved entity loop.

It can use the world entity, its components, and world managers.

Some Post overrides push `TheWorld:PushEvent("ms_setworldsetting", ...)`.

`WorldSettings:SetSetting(setting, value)` records the value, while gameplay effects remain in override helpers.

Prefab `OnPreLoad` functions call the timer-migration helpers later, while saved entities are restored.

### `0x22053311` Shard RPC Sync

`Shard_SyncWorldSettings` sends selected master overrides through `SHARD_RPC.SyncWorldSettings`.

The `networkclientrpc.lua` handler calls `WorldSettings_Overrides.Sync[option]` when present.

Otherwise, it applies matching Pre and Post overrides.

The current Sync table is empty.

## `0x22054111` `worldsettingstimer`

`WorldSettingsTimer:AddTimer` stores a name, maximum duration, enabled state, callback, and long-update handler.

`StartTimer`, `PauseTimer`, `ResumeTimer`, and `StopTimer` control execution, while `OnSave` and `OnLoad` persist it.

`LongUpdate(dt)` advances timers across offline time or a long frame.

## `0x22054211` Timer Migration

`WorldSettings_ChildSpawner_PreLoad` moves `childspawner` timing data into `worldsettingstimer`.

`WorldSettings_Timer_PreLoad` moves `timer` data.

`WorldSettings_Spawner_PreLoad` and `WorldSettings_Pickable_PreLoad` move `spawner` and `pickable` timing data.

The world-settings runtime therefore normalizes saved timer data as well as applying live settings.

## `0x22055100` Verification

~~~bash
rg -n "worldsettings_overrides|WorldSettings_Overrides\\.(Pre|Post)" \
  scripts/gamelogic.lua

rg -n "Shard_SyncWorldSettings|SyncWorldSettings|WorldSettings_Overrides\\.Sync" \
  scripts/shardnetworking.lua \
  scripts/networkclientrpc.lua

rg -n "ms_setworldsetting|SetSetting|applyoverrides_(pre|post|sync)" \
  scripts/worldsettings_overrides.lua \
  scripts/components/worldsettings.lua

rg -n "WorldSettings_.*PreLoad|WorldSettings_.*Spawn|WorldSettings_.*Regen|worldsettingstimer" \
  scripts/worldsettingsutil.lua \
  scripts/components/worldsettingstimer.lua \
  scripts/prefabs
~~~

### `0x22055111` Next Read

Read the Pre and Post call sites in `gamelogic.lua`.

Then inspect the override table in `worldsettings_overrides.lua`.

Trace shard updates through `shardnetworking.lua` and `networkclientrpc.lua`.

Finally, inspect timer migration and persistence in `worldsettingsutil.lua` and `worldsettingstimer.lua`.
