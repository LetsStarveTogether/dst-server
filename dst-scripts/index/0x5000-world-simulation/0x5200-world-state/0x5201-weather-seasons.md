# `0x52010000` Weather and Seasons

This page traces shared time, seasons, temperature, surface weather, projected world state, and entity reactions.

`world_network` owns shared timing, while `worldstate.data` exposes the resulting state.

## `0x52011111` State Projection

`scripts/components/worldstate.lua` does not calculate seasons, precipitation, or temperature.

It listens for normalized world events such as `clocktick`, `seasontick`, `temperaturetick`, and `weathertick`.

It then updates the `self.data` table exposed as `TheWorld.state`.

## `0x52011121` Component Ownership

`scripts/prefabs/world_network.lua` adds `clock`, `worldtemperature`, and `seasons` to `TheWorld.net`.

`scripts/prefabs/forest_network.lua` adds surface `weather` through `custom_postinit`.

## `0x52012000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/prefabs/world.lua` | `MakeWorld` | Creates `TheWorld`, adds `worldstate`, and exposes `inst.state` |
| `scripts/prefabs/world_network.lua` | `MakeWorldNetwork` | Creates `TheWorld.net` with `clock`, `worldtemperature`, and `seasons` |
| `scripts/prefabs/forest_network.lua` | `custom_postinit` | Adds surface `weather` to the network world |
| `scripts/components/clock.lua` | `OnUpdate` | Advances day phases, moon phases, and cycle events |
| `scripts/components/seasons.lua` | `OnSeasonDirty` | Emits `seasontick` from replicated season values |
| `scripts/components/worldtemperature.lua` | `OnUpdate` / `PushTemperature` | Computes and emits `temperaturetick` |
| `scripts/components/weather.lua` | `OnUpdate` | Computes surface moisture, precipitation, snow, lightning, and lunar hail |
| `scripts/components/worldstate.lua` | `SetVariable` | Projects event data into `TheWorld.state` |
| `scripts/components/temperature.lua` | `OnUpdate` | Combines environment, equipment, heat sources, and moisture penalties for an entity |
| `scripts/components/temperatureoverrider.lua` | `GetTemperatureAtXZ` / `GetLocalTemperature` | Resolves local overrides before falling back to world temperature |
| `scripts/components/moisture.lua` | `OnUpdate` | Combines precipitation, waterproofing, drying, and water state for an entity |

### `0x52012111` Shared Timing

Search `scripts/prefabs/world_network.lua` for `AddComponent("clock")`, `AddComponent("worldtemperature")`, and `AddComponent("seasons")`.

These components belong to `TheWorld.net`, not `TheWorld.components`.

### `0x52012121` Surface Weather

Search `scripts/prefabs/forest_network.lua` for `AddComponent("weather")`.

Surface precipitation is owned by `forest_network`; cave precipitation must not be inferred from `weather.lua`.

### `0x52012131` World State

Search `scripts/components/worldstate.lua` for `inst:ListenForEvent("weathertick"`.

`SetVariable("precipitation"` confirms its state-projection and watcher-notification role.

## `0x52013000` Flow

~~~mermaid
flowchart TD
    A["prefabs/world.lua\nAddComponent(worldstate)"]
    B["prefabs/world_network.lua\nclock + seasons + worldtemperature"]
    C["prefabs/forest_network.lua\nweather"]
    D["components/clock.lua\nclocktick / phasechanged"]
    E["components/seasons.lua\nseasontick"]
    F["components/worldtemperature.lua\nOnUpdate -> temperaturetick"]
    G["components/weather.lua\nweathertick / precipitationchanged"]
    H["components/worldstate.lua\nSetVariable"]
    I["EntityScript:WatchWorldState\nentity and component reactions"]

    A --> H
    B --> D
    B --> E
    B --> F
    C --> G
    D --> H
    E --> F
    E --> G
    E --> H
    F --> G
    F --> H
    G --> H
    H --> I
~~~

### `0x52013111` World Assembly

In `scripts/prefabs/world.lua`, `inst:AddComponent("worldstate")` precedes the `inst.state` assignment.

`inst.state = inst.components.worldstate.data` makes most callers read the same table.

### `0x52013121` Network Assembly

Find `inst:AddComponent("seasons")` in `scripts/prefabs/world_network.lua`.

Confirm that `seasons`, `clock`, and `worldtemperature` share the same network prefab.

### `0x52013211` Clock Events

Search `clock.lua` for `_world:PushEvent("clocktick"`, `_world:PushEvent("phasechanged"`, and `ms_cyclecomplete`.

`seasons.lua` advances the season in response to `ms_cyclecomplete`.

### `0x52013221` Season Events

Search `seasons.lua` for `_world:PushEvent("seasontick"`.

The same component listens for `ms_setseason`, `ms_advanceseason`, and `ms_setseasonlength`.

### `0x52013231` World Temperature

`OnSeasonTick` only caches the season name and seasonal temperature term; it does not call `PushTemperature`.

`OnClockTick` stores the phase contribution.

`OnUpdate` advances noise and summer bloom, then `PushTemperature` calculates and emits `temperaturetick`.

### `0x52013311` Surface Weather Inputs

`weather.lua` calls `inst:ListenForEvent("seasontick"` and `inst:ListenForEvent("temperaturetick"`.

It also calls `inst:ListenForEvent("phasechanged"`.

These inputs affect precipitation chance and type, snow cover, and lightning timing.

### `0x52013321` Surface Weather Outputs

Search `weather.lua` for `_world:PushEvent("weathertick"`, `preciptypedirty`, `snowcovereddirty`, and `wetdirty`.

`weathertick` directly carries continuous values, including `lunarhailrate`, into `worldstate.lua`'s `OnWeatherTick`.

Dirty handlers emit `moistureceilchanged`, `precipitationchanged`, `snowcoveredchanged`, and `wetchanged`.

`worldstate.lua` listens directly for `temperaturetick` and for both weather event forms.

### `0x52013411` Entity Reactions

`EntityScript:WatchWorldState` uses `TheWorld.components.worldstate:AddWatcher` to subscribe entities and components.

Common values include `israining`, `issnowing`, `iswet`, and `temperature`.

### `0x52013421` Entity Temperature and Moisture

Search `temperature.lua` for `GetTemperatureAtXZ` and `GetMoisturePenalty`.

`temperatureoverrider.lua` defines `GetTemperatureAtXZ` and `GetLocalTemperature`.

It checks active overrides, cave fumarole temperature, then `TheWorld.state.temperature`.

Then search `moisture.lua` for `GetMoistureRate`, `GetWaterproofness`, and `OnUpdate`.

## `0x52014111` Clock Fields

Events from `clock.lua` drive `time`, `timeinphase`, `cycles`, `phase`, `isday`, `isdusk`, and `isnight`.

They also drive `moonphase` and the moon-phase booleans.

## `0x52014121` Season Fields

Events from `seasons.lua` drive `season`, `isautumn`, `iswinter`, `isspring`, and `issummer`.

They also drive `seasonprogress` and remaining-day fields.

## `0x52014131` Weather Fields

`temperaturetick` directly sets `temperature` from `worldtemperature.lua`.

`weathertick` directly sets `moisture`, `pop`, `precipitationrate`, `snowlevel`, and `lunarhaillevel`.

It also directly sets `lunarhailrate` and `wetness`.

The discrete dirty-handler events update `moistureceil`, precipitation fields, `issnowcovered`, and `iswet`.

## `0x52014211` Server and Client Boundary

`clock.lua`, `seasons.lua`, `worldtemperature.lua`, and `weather.lua` contain server-side event handling.

They also emit netvar dirty notifications.

Trace mutations under `TheWorld.ismastersim` before following dirty events to client presentation.

## `0x52014221` Cave Boundary

Cave network worlds use `scripts/components/caveweather.lua`.

Surface snow, lunar hail, and lightning rules from `weather.lua` therefore do not apply directly.

## `0x52015100` Verification

~~~bash
rg -n \
  -e "AddComponent\\(\"(worldstate|clock|worldtemperature|seasons|weather)\"\\)" \
  -e "PushEvent\\(\"(clocktick|seasontick|temperaturetick|weathertick)" \
  -e "PushEvent\\(\"(moistureceilchanged|precipitationchanged|snowcoveredchanged|wetchanged)\"" \
  -e "ListenForEvent\\(\"(clocktick|seasontick|temperaturetick|weathertick)" \
  -e "ListenForEvent\\(\"(moistureceilchanged|precipitationchanged|snowcoveredchanged|wetchanged)" \
  -e "OnTemperatureTick|OnWeatherTick|lunarhailrate|PushTemperature" \
  scripts/prefabs/world.lua \
  scripts/prefabs/world_network.lua \
  scripts/prefabs/forest_network.lua \
  scripts/components/clock.lua \
  scripts/components/seasons.lua \
  scripts/components/worldtemperature.lua \
  scripts/components/weather.lua \
  scripts/components/worldstate.lua

rg -n "GetTemperatureAtXZ|GetLocalTemperature|fumarolelocaltemperature" \
  scripts/components/temperature.lua \
  scripts/components/temperatureoverrider.lua \
  scripts/prefabs/cave_network.lua
~~~

### `0x52015111` Minimum Trace

Follow component assembly in `world_network.lua`.

Then trace `clocktick`, `seasontick`, `temperaturetick`, and `weathertick`.

Finally, verify their fields in `worldstate.lua`.

### `0x52015121` Consumer Sample

~~~bash
rg -n "WatchWorldState\\(\"(israining|issnowing|iswet|temperature|season|iswinter|isspring)" \
  scripts/prefabs \
  scripts/components
~~~

This query samples how entities, components, and UI consume weather and season state.
