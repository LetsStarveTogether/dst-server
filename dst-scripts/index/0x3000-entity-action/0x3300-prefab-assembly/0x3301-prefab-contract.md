# `0x33010000` Prefab Assembly Contract

This page explains how `Prefab(name, fn, assets, deps)` becomes a spawnable entity.
It covers the runtime contract, representative examples, and verification paths.
The complete prefab file list remains in `0x8000-reference/0x8200-runtime-catalogs/0x8201-prefab-catalog.md`.

## `0x33011111` Prefab Objects

A `Prefab` object is an assembly description registered with the simulation, not an entity.
It stores a name, entity constructor, asset list, spawn dependencies, and asset resolution policy.
The entity exists only after `SpawnPrefabFromSim` calls `prefab.fn(TheSim)`.

## `0x33011121` Client and Server Boundary

Most network prefab functions first create the entity, network, animation, tags, and client-readable netvars.
They then call `inst.entity:SetPristine()`.
Only the master simulation continues past `if not TheWorld.ismastersim then return inst`.
It adds authoritative components, SG, Brain, persistence, and loot.
This boundary determines what clients can display directly and what must arrive through a replica or classified.

## `0x33011131` Helper Files

`scripts/prefabutil.lua` can return prefab factories.
`scripts/standardcomponents.lua` provides helpers for physics, burning, freezing, haunting, and similar features.
Prefab files call these helpers, but neither file is the `Prefabs` registry.

## `0x33012000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/prefabs.lua` | `Prefab` | Normalize and store the prefab declaration. |
| `scripts/mainfunctions.lua` | `LoadPrefabFile` | Execute a prefab file chunk and collect returned `Prefab` objects. |
| `scripts/mainfunctions.lua` | `RegisterPrefabsImpl` | Resolve, store, and register prefabs. |
| `scripts/mainfunctions.lua` | `SpawnPrefabFromSim` | Call `prefab.fn` from the simulation and emit `entity_spawned`. |
| `scripts/prefabutil.lua` | `MakePlacer`, `MakeDeployableKitItem` | Generate common prefabs through helpers. |
| `scripts/standardcomponents.lua` | `MakeInventoryPhysics`, `MakeSnowCovered` | Apply standard assembly helpers. |
| `scripts/prefabs/rabbit.lua` | `fn` | Show the pristine, client return, and server component pattern for a creature. |
| `scripts/prefabs/player_common.lua` | `MakePlayerCharacter` | Assemble player network state. |

### `0x33012111` `Prefab` Constructor

The constructor strips path prefixes and keeps the final segment as `self.name`.
It stores `fn`, `assets`, `deps`, and `force_path_search`.
When `PREFAB_SKINS[self.name]` exists, it appends skin prefabs to `deps`.
`deps` is therefore not always identical to the caller-provided list.

### `0x33012121` Registration

`LoadPrefabFile` gets a chunk with `loadfile(filename)` and captures every return value through `{fn()}`.
Only values where `type(val) == "table"` and `val:is_a(Prefab)` enter registration.
`RegisterPrefabsImpl` resolves `asset.file`, records `PrefabPostInit`, and writes `Prefabs[prefab.name]`.
It then calls `TheSim:RegisterPrefab(prefab.name, prefab.assets, prefab.deps)`.

### `0x33012211` Spawning

Lua `SpawnPrefab(name, skin, skin_id, creator, skin_custom)` calls `TheSim:SpawnPrefab`.
The simulation returns to Lua through `SpawnPrefabFromSim(name)`.
`SpawnPrefabFromSim` looks up `Prefabs[name]`, calls `prefab.fn(TheSim)`, and then calls `inst:SetPrefabName`.
`PrefabPostInit` and `PrefabPostInitAny` run after the constructor returns.
Finally, `TheWorld:PushEvent("entity_spawned", inst)` notifies the world.
Mod post-inits are outside the original prefab body but can change the final entity.

## `0x33013000` Assembly Flow

~~~mermaid
flowchart TD
    A["Prefab file returns Prefab(...)"]
    A --> B["LoadPrefabFile executes chunk"]
    B --> C["RegisterPrefabsImpl"]
    C --> D["Prefabs[name] + TheSim:RegisterPrefab"]
    D --> E["SpawnPrefab -> TheSim:SpawnPrefab"]
    E --> F["SpawnPrefabFromSim -> prefab.fn(TheSim)"]
    F --> G["CreateEntity + AddNetwork + tags + netvars"]
    G --> H["entity:SetPristine"]
    H --> I{"TheWorld.ismastersim"}
    I -->|false| J["client returns pristine entity"]
    I -->|true| K["server AddComponent / SetStateGraph / SetBrain"]
    K --> L["PrefabPostInit + entity_spawned"]
~~~

### `0x33013111` Declaration Fields

`assets` declares resources that the simulation resolves during registration.
`deps` names prefabs that must be known before spawning.
`fn` is the entity assembly function.
`force_path_search` and `search_asset_first_path` affect asset resolution, not entity lifecycle.

### `0x33013121` File Return Values

One prefab file can return multiple values.
Only actual `Prefab` objects enter registration.
Helper tables, local functions, and ordinary configuration tables do not register automatically.

### `0x33013211` Before `SetPristine`

Before `SetPristine`, add the network, base tags, animation bank and build, and inventory image overrides.
Also add other client-readable state.
State that must reach remote clients initially needs a tag, netvar, or network component before this boundary.

### `0x33013221` After `SetPristine`

Place server components, SG, Brain, and persistence in the master simulation branch.
Authoritative event listeners belong there too.
The client path cannot depend on server-only components after returning.
Documentation that treats a server component as directly client-readable crosses this boundary incorrectly.

## `0x33014111` Creature Example: `scripts/prefabs/rabbit.lua`

The `rabbit` constructor first adds `Transform`, `AnimState`, `SoundEmitter`, `DynamicShadow`, and `Network`.
Before pristine, it adds animal and cookable tags plus client build and inventory image overrides.
It also calls `MakeFeedableSmallLivestockPristine(inst)`.
Remote clients return after `SetPristine`.
Only the master simulation adds `locomotor`, `drownable`, `eater`, `inventoryitem`, `health`, and `combat`.
A source comment requires `locomotor` to exist before `SetStateGraph("SGrabbit")`.

## `0x33014211` Player Example: `scripts/prefabs/player_common.lua`

`MakePlayerCharacter` merges base player assets and dependencies with `customassets` and `customprefabs`.
Base dependencies include `player_classified`, `inventory_classified`, `wonkey`, and `spellbookcooldown`.
`SetInstanceFunctions` installs `AttachClassified`, `DetachClassified`, and `OnRemoveEntity` on the instance.
Clients need these functions because classified binding also occurs client-side.
Before pristine, the player manually adds prereplica tags.
They include `_health`, `_hunger`, `_sanity`, `_builder`, and `_combat`.
The master simulation later removes those tags and reintroduces them through real component replication.
It then spawns `player_classified` and makes it a child of the player entity.

## `0x33014311` Factories in `scripts/prefabutil.lua`

`MakePlacer` returns `Prefab(name, fn)` for a non-network placement helper.
`MakeDeployableKitItem` returns an item factory in the form `Prefab(name, function() ... end, assets)`.
That factory still follows `AddNetwork`, `SetPristine`, client return, and master simulation component boundaries.
`deployable_data.ondeploy` can replace its default structure-spawning deployment handler.

## `0x33014321` Helpers in `scripts/standardcomponents.lua`

`MakeInventoryPhysics`, `MakeCharacterPhysics`, and similar helpers modify the supplied entity directly.
`MakeSnowCoveredPristine` sets client-visible animation symbols and tags.
On a master simulation entity with `Network`, `MakeSnowCovered` can connect `MakeLunarHailBuildup`.
Visibility depends on where a helper is called, not only on which file defines it.
`MakeGolfObstaclePhysics(inst, rad, golfradoverride)` accepts a golf collision radius override.
`GolfObstacle_OnEntityWake` uses that override to create boat-limit collision helpers.
`MakeFumaroleTool` sets `inventoryitemtemperature.inherentinsulation`.
It also listens for `temperaturedelta` and adjusts a temperature modifier from external heat intensity.

## `0x33015111` Catalog Scope

`0x8201-prefab-catalog.md` verifies complete Lua file coverage under `scripts/prefabs/`.
This page does not duplicate that list because two copies would drift from `git ls-files`.

## `0x33015121` Gameplay Scope

Character, boss, item, and FX internals belong in their own topics.
This page only explains how they become prefab entities.

## `0x33016100` Verification

~~~bash
rg -n "Prefab = Class|LoadPrefabFile|RegisterPrefabsImpl|SpawnPrefabFromSim|function SpawnPrefab\\b" \
  scripts/prefabs.lua \
  scripts/mainfunctions.lua
rg -n "SetPristine|TheWorld\\.ismastersim|SetStateGraph|SetBrain|return Prefab" \
  scripts/prefabs/rabbit.lua \
  scripts/prefabs/player_common.lua \
  scripts/prefabutil.lua
rg -n "MakeDeployableKitItem|deployable_data\\.ondeploy" scripts/prefabutil.lua
rg -n "MakeGolfObstaclePhysics|GolfObstacle_OnEntityWake|MakeFumaroleTool|fumarole_UpdateTemperatureModifier" \
  scripts/standardcomponents.lua
~~~

### `0x33016111` Minimal Trace

Read the `Prefab` constructor in `scripts/prefabs.lua`.
Trace `LoadPrefabFile` into `RegisterPrefabsImpl`.
Use `rabbit` to verify the three-stage ordinary prefab pattern.
Finish with `MakePlayerCharacter` to verify prereplica tags and classified dependencies.

### `0x33016121` Common Misreadings

- `assets` is not an entity field.
- `deps` is not a component list.
- `PrefabPostInit` is not part of the original prefab file body.
- Not every prefab has an SG or Brain.
- `standardcomponents.lua` is not a registry.
