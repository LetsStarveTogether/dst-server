# `0x22040000` Runtime Foundations

`class.lua`, `json.lua`, and `constants.lua` establish shared Lua contracts early in startup.

## `0x22041111` Purpose

`main.lua` loads `json`, `constants`, and `class` before most gameplay, UI, prefab, and world-generation modules.

They define the common language layer and enums rather than private helpers for one system.

## `0x22042000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/main.lua` | `require("json")` | Load JSON encoding and decoding early |
| `scripts/main.lua` | `require("constants")` | Load shared enums and numeric contracts early |
| `scripts/main.lua` | `require("class")` | Load the `Class` constructor early |
| `scripts/class.lua` | `Class` | Build classes, inheritance, properties, and instance metatables |
| `scripts/class.lua` | `ClassRegistry` | Retain inherited members for hot-reload cleanup |
| `scripts/json.lua` | `json.encode` / `json.decode` | Encode and decode the runtime's non-standard JSON form |
| `scripts/json.lua` | `json.encode_compliant` | Encode standards-compliant JSON |
| `scripts/constants.lua` | `RESET_ACTION` | Select reset paths |
| `scripts/constants.lua` | `SAVELOAD` | Represent save and load states |
| `scripts/constants.lua` | `REMOTESHARDSTATE` / `SHARDID` | Represent shard state and identity |
| `scripts/constants.lua` | `SHARDTRANSACTIONTYPES` | Identify cross-shard transaction types |

## `0x22043000` Startup Role

~~~mermaid
flowchart TD
    A["main.lua startup"]
    A --> B["require json"]
    A --> C["require constants"]
    A --> D["require class"]
    B --> E["data encode/decode helpers"]
    C --> F["shared runtime enums"]
    D --> G["Class(base, ctor, props)"]
    G --> H["components / widgets / managers"]
    F --> I["gamelogic / networking / shard paths"]
~~~

### `0x22043111` `Class`

`Class(base, _ctor, props)` creates a callable class table and uses it as each instance's metatable.

With `props`, it installs property-aware `__index` and `__newindex` handlers.

`makereadonly`, `addsetter`, and `removesetter` manage properties.

`ClassRegistry` retains inherited members for hot reload.

A component, widget, or manager created with `Class(...)` is therefore more than a table factory.

### `0x22043211` JSON

`json.encode` is the common runtime encoder, while `json.encode_compliant` explicitly produces standard JSON.

`json.decode` and `json.null` handle decoding and null values.

Confirm the chosen encoding path when reading save, configuration, server-data, or external-text tools.

### `0x22043311` Constants

`RESET_ACTION` controls reset routing in `gamelogic.lua`, and `SAVELOAD` represents persistence states.

`REMOTESHARDSTATE`, `SHARDID`, and `SHARDTRANSACTIONTYPES` shape shard-network behaviour.

Changes to these enums can affect runtime, networking, and world-state code together.

## `0x22044100` Verification

~~~bash
rg -n "require\\(\"json\"\\)|require\\(\"constants\"\\)|require\\(\"class\"\\)" \
  scripts/main.lua

rg -n "function Class|ClassRegistry|makereadonly|addsetter|removesetter" \
  scripts/class.lua

rg -n "json\\.encode|json\\.decode|encode_compliant|null" \
  scripts/json.lua

rg -n "RESET_ACTION|SAVELOAD|REMOTESHARDSTATE|SHARDID|SHARDTRANSACTIONTYPES" \
  scripts/constants.lua
~~~

### `0x22044111` Next Read

Confirm the load order in `main.lua`, then inspect object construction in `class.lua`.

Open `json.lua` or `constants.lua` for the active problem domain.
