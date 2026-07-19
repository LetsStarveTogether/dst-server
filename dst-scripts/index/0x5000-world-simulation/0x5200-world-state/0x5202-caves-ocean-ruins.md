# `0x52020000` Caves, Ocean, and Ruins

This page separates cave environment rules, surface ocean systems, and ruins generation from runtime reset behaviour.

## `0x52021111` World Prefabs

`scripts/prefabs/forest.lua` returns `MakeWorld("forest", ...)`.

`scripts/prefabs/cave.lua` returns `MakeWorld("cave", ...)` with the `cave` tag.

## `0x52021121` Cave Environment

`scripts/prefabs/cave_network.lua` adds five cave-specific components.

They are `caveweather`, `quaker`, `nightmareclock`, `vault_floor_helper`, and `fumarolelocaltemperature`, but not `weather`.

## `0x52021131` Ocean Ownership

`world.lua` provides shared `waterphysics` and `walkableplatformmanager` components plus the master `dockmanager`.

Surface `forest.lua` adds `WaveComponent`, `wavemanager`, and `oceanicemanager`.

## `0x52022000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/prefabs/world.lua` | `MakeWorld` | Adds shared water, platform, dock, and ocean-colour services |
| `scripts/prefabs/forest.lua` | `common_postinit` | Initializes waves and surface ocean presentation |
| `scripts/prefabs/forest.lua` | `master_postinit` | Adds `worldwind`, `oceanicemanager`, and surface ecology managers |
| `scripts/prefabs/cave.lua` | `master_postinit` | Adds cave spawners, `caveins`, `riftspawner`, and ruins managers |
| `scripts/prefabs/cave_network.lua` | `custom_postinit` | Adds cave weather, quake, nightmare, Vault-floor, and local-temperature components |
| `scripts/components/caveweather.lua` | `OnUpdate` | Broadcasts cave precipitation, acid rain, and moisture |
| `scripts/components/fumarolelocaltemperature.lua` | `GetTemperatureAtXZ` | Blends cave fumarole-area and shared world temperatures |
| `scripts/components/quaker.lua` | `StartQuake` | Handles quake warnings, debris, sound, and forced quakes |
| `scripts/components/nightmareclock.lua` | `OnUpdate` | Advances nightmare phases and broadcasts events |
| `scripts/components/wavemanager.lua` | `OnUpdate` | Spawns client wave prefabs |
| `scripts/components/oceancolor.lua` | `OnPhaseChanged` | Blends client ocean color and textures |
| `scripts/components/oceanicemanager.lua` | `CreateIceAtPoint` | Creates and breaks ocean ice and fixes displaced objects |
| `scripts/components/dockmanager.lua` | `DestroyDockAtPoint` | Fixes objects displaced by dock removal |
| `scripts/components/walkableplatform.lua` | `DestroyObjectsOnPlatform` | Cleans up objects when a platform is destroyed |
| `scripts/components/vaultroom.lua` | `_getunloadaction` | Selects entity persistence behaviour when a Vault room unloads |
| `scripts/map/tasks/ruins.lua` | `AddTask` | Defines ruins task structure |
| `scripts/map/rooms/cave/ruins.lua` | `AddRoom` | Defines ruins rooms, distributions, and static layouts |
| `scripts/prefabs/atrium_gate.lua` | `OnDestabilizeExplode` | Pushes `resetruins` after gate destabilization |
| `scripts/prefabs/ruinsrespawner.lua` | `MakeFn` | Resets ruins objects on `resetruins` |
| `scripts/prefabs/cave_hole.lua` | `ListenForEvent("resetruins")` | Restarts cave-hole object spawning |
| `scripts/prefabs/treasurechest.lua` | `ListenForEvent("resetruins")` | Resets ruins chest variants |
| `scripts/prefabs/chest_mimic.lua` | `ListenForEvent("resetruins")` | Resets mimic forms and trackers |

### `0x52022111` Cave Network Search

Search `cave_network.lua` for its five `AddComponent` calls.

`caveweather`, `quaker`, and `nightmareclock` are the main cave environment signal sources.

`fumarolelocaltemperature` supplies a local temperature branch.

### `0x52022121` Surface Ocean Search

Search `forest.lua` for `AddWaveComponent`, `AddComponent("wavemanager")`, and `AddComponent("oceanicemanager")`.

These calls separate wave rendering, wave spawning, and ocean-ice management.

### `0x52022131` Ruins Search

Search `scripts/map/rooms/cave/ruins.lua` for `AddRoom`, `ruins_statue`, and `cave_hole`.

Worldgen places ruins room content, while prefabs and respawners own its runtime behaviour.

## `0x52023000` Flow

~~~mermaid
flowchart TD
    A["prefabs/world.lua\ncommon world services"]
    B["prefabs/cave.lua\ncave tag + cave master components"]
    C["prefabs/cave_network.lua\ncave weather + quake + nightmare + local temperature"]
    D["components/caveweather.lua\nweathertick / precipitationchanged"]
    E["components/quaker.lua\nwarnquake / startquake / endquake"]
    F["components/nightmareclock.lua\nnightmarephasechanged"]
    G["components/worldstate.lua\ncave and nightmare fields"]
    H["map/tasks/ruins.lua\nruins task graph"]
    I["map/rooms/cave/ruins.lua\nroom contents"]
    J["prefabs/ruinsrespawner.lua\nresetruins"]
    N["prefabs/atrium_gate.lua\nPushEvent(resetruins)"]
    O["cave_hole + treasurechest + chest_mimic\ndirect resetruins listeners"]
    K["prefabs/forest.lua\nwaves + ocean ice"]
    L["components/wavemanager.lua\nocean visuals"]
    M["components/oceanicemanager.lua\nocean ice runtime"]

    A --> B
    B --> C
    C --> D
    C --> E
    C --> F
    D --> G
    F --> G
    H --> I
    I --> J
    I --> O
    N --> J
    N --> O
    A --> K
    K --> L
    K --> M
~~~

### `0x52023111` Cave World Assembly

In `cave.lua`, `master_postinit` adds `caveins`, `hounded`, `retrofitcavemap_anr`, `riftspawner`, and `miasmamanager`.

It also adds `ruinsshadelingspawner` and `vaultroommanager` on the Master Simulation.

### `0x52023121` Cave Network Assembly

Search `cave_network.lua` for `SetTemperatureMod`.

The cave network adds environment components and applies a cave modifier to `worldtemperature`.

It also adds `fumarolelocaltemperature`, which `GetTemperatureAtXZ` consults before falling back to shared world temperature.

### `0x52023211` Cave Weather Inputs

`caveweather.lua` calls `inst:ListenForEvent("seasontick"` and `inst:ListenForEvent("temperaturetick"`.

It also calls `inst:ListenForEvent("phasechanged"`.

Cave precipitation therefore uses shared timing but its own calculation logic.

### `0x52023221` Cave Weather Outputs

Search `caveweather.lua` for `_world:PushEvent("weathertick"`, `preciptypedirty`, `wetdirty`, and `moistureceildirty`.

Dirty handlers convert netvar updates into world events that `worldstate.lua` consumes.

The cave `acidrain` precipitation type becomes `TheWorld.state.isacidraining`.

### `0x52023311` Quake Lifecycle

Search `quaker.lua` for `WarnQuake`, `StartQuake`, `EndQuake`, `ms_miniquake`, and `ms_forcequake`.

The component emits `warnquake`, `startquake`, and `endquake`, while the same file handles falling debris.

### `0x52023321` Nightmare Phases

Search `nightmareclock.lua` for `nightmarephasechanged` and `nightmareclocktick`.

`worldstate.lua` projects these events into `nightmarephase`, `isnightmarecalm`, and `isnightmarewarn`.

It also updates `isnightmarewild` and `isnightmaredawn`.

### `0x52023411` Shared Water Systems

Search `world.lua` for `walkableplatformmanager`, `waterphysics`, and `dockmanager` assembly.

These are common world services rather than surface-only systems.

### `0x52023421` Surface Ocean Presentation

Search `forest.lua` for `AddWaveComponent`, `Map:DoOceanRender(true)`, and `AddComponent("wavemanager")`.

`wavemanager` is omitted on dedicated servers because it primarily supports client presentation.

### `0x52023431` Ocean Ice

Search `oceanicemanager.lua` for `worldmapsetsize`, `CreateIceAtPoint`, `DestroyIceAtPoint`, and `icefloebreak`.

Ocean-ice logic adjusts floaters, fishable objects, and objects on platforms, so it cannot be reduced to tile state.

### `0x52023511` Ruins Generation

`AddTask` in `scripts/map/tasks/ruins.lua` defines task structure.

`AddRoom` in `scripts/map/rooms/cave/ruins.lua` defines prefabs, distributions, and static layouts.

### `0x52023521` Ruins Reset

Search `ruinsrespawner.lua` for `resetruins`, `objectspawner`, and `onprefabswaped`.

`OnDestabilizeExplode` in `atrium_gate.lua` pushes `resetruins`.

The respawner recreates its owned ruins objects at runtime.

`cave_hole.lua`, `treasurechest.lua`, and `chest_mimic.lua` also listen directly on `TheWorld` for `resetruins`.

Each runs its own reset path without rerunning cave worldgen.

## `0x52024111` Cave State Fields

Cave branch events drive `cavephase`, `iscaveday`, `iscavedusk`, and `iscavenight`.

They also drive `cavemoonphase`, `iscavefullmoon`, and `iscavenewmoon` in `worldstate.lua`.

## `0x52024121` Nightmare State Fields

`nightmareclock.lua` drives `nightmarephase`, `nightmaretime`, `nightmaretimeinphase`, and the four `isnightmare*` booleans.

## `0x52024211` Common Ocean Layer

`world.lua` adds `waterphysics`, `walkableplatformmanager`, master `dockmanager`, and non-dedicated `oceancolor`.

These components affect water and platform rules across worlds.

## `0x52024221` Surface Ocean Layer

`forest.lua` adds `WaveComponent`, `wavemanager`, and `Map:DoOceanRender(true)` as surface presentation entry points.

## `0x52024231` Surface Ice Layer

`forest.lua` adds `oceanicemanager` in `master_postinit`; the component listens for map size and manages the ocean-ice lifecycle.

## `0x52024311` Ruins Worldgen Layer

`scripts/map/tasks/ruins.lua` and `scripts/map/rooms/cave/ruins.lua` define ruins placement and contents.

## `0x52024321` Ruins Runtime Layer

`ruinsrespawner.lua` and the direct prefab listeners define reset behaviour without regenerating the cave map.

## `0x52024411` Cave, Ocean, and Vault Edge Cases

The cave gel spawn in `prefabs/cave.lua` excludes topology ids containing `Vault`.

When a virtual room unloads, `vaultroom.lua` preserves an owner with a migration pet.

`minotaur.lua` disables minimap revealability after `ATRIUM_KEY_FOUND`.

`trap_fumarole.lua` disables its minimap icon while held, dropped, or beginning to float.

Unlike mushroom spores, `scripts/prefabs/moonstorm_spark.lua` has no density-crowding task.

`perishable` calls `depleted` to end its lifetime; `sparktask` only schedules electrical effects.

Five displacement and destruction paths across four files include parented `childdeployblocker` entities.

`componentutil.lua` provides the ocean and void tile-change paths.

The other three are `DestroyDockAtPoint`, `DestroyIceAtPoint`, and `DestroyObjectsOnPlatform`.

## `0x52025100` Verification

~~~bash
rg -n \
  -e "AddComponent\\(\"(caveweather|quaker|nightmareclock|vault_floor_helper|fumarolelocaltemperature|caveins)\"\\)" \
  -e "AddComponent\\(\"(wavemanager|oceanicemanager|waterphysics|walkableplatformmanager|dockmanager)\"\\)" \
  -e "PushEvent\\(\"(weathertick|nightmarephasechanged|nightmareclocktick)" \
  -e "PushEvent\\(\"(warnquake|startquake|endquake|icefloebreak)\"" \
  -e "AddWaveComponent|AddTask|AddRoom|resetruins" \
  scripts/prefabs/world.lua \
  scripts/prefabs/forest.lua \
  scripts/prefabs/cave.lua \
  scripts/prefabs/cave_network.lua \
  scripts/components/caveweather.lua \
  scripts/components/fumarolelocaltemperature.lua \
  scripts/components/quaker.lua \
  scripts/components/nightmareclock.lua \
  scripts/components/wavemanager.lua \
  scripts/components/oceanicemanager.lua \
  scripts/map/tasks/ruins.lua \
  scripts/map/rooms/cave/ruins.lua \
  scripts/prefabs/atrium_gate.lua \
  scripts/prefabs/ruinsrespawner.lua \
  scripts/prefabs/cave_hole.lua \
  scripts/prefabs/treasurechest.lua \
  scripts/prefabs/chest_mimic.lua

rg -n \
  -e "Vault|migrationpetowner|ATRIUM_KEY_FOUND|childdeployblocker" \
  -e "SetEnabled\\(false\\)|MUSHSPORE|crowding" \
  -e "sparktask|perishable|depleted" \
  scripts/prefabs/cave.lua \
  scripts/components/vaultroom.lua \
  scripts/prefabs/minotaur.lua \
  scripts/prefabs/trap_fumarole.lua \
  scripts/prefabs/moonstorm_spark.lua \
  scripts/prefabs/mushtree_spores.lua \
  scripts/componentutil.lua \
  scripts/components/dockmanager.lua \
  scripts/components/oceanicemanager.lua \
  scripts/components/walkableplatform.lua
~~~

### `0x52025111` Minimum Trace

Follow the three environment components in `cave_network.lua`.

Inspect their events in `caveweather.lua`, `quaker.lua`, and `nightmareclock.lua`.

Then verify cave and nightmare fields in `worldstate.lua`.

Finish with the ocean branch in `scripts/prefabs/forest.lua`.

Then inspect the ruins branch in `scripts/map/rooms/cave/ruins.lua`.

### `0x52025121` Ruins Runtime Sample

~~~bash
rg -n "resetruins|ruinsrespawner|ruins_statue|ruins_shadeling|nightmarephase" \
  scripts/prefabs \
  scripts/map/rooms/cave/ruins.lua
~~~

This query distinguishes ruins generation data, ruins prefabs, and consumers of nightmare state.
