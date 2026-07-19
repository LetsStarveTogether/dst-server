# `0x72030000` Media, FX, and Audio

Animations, effects, and audio originate in prefabs, StateGraphs, or debug commands.
The main FX path is `fx.lua -> prefabs/fx.lua -> SpawnPrefab(...) / SoundEmitter:PlaySound(...)`.

## `0x72031111` Purpose

Trace a visible or audible result back to `AnimState`, `SoundEmitter`, an FX prefab, or its `FrameEvent`.
`scripts/fx.lua` usually defines FX data, while `scripts/prefabs/fx.lua` turns that data into spawnable prefabs.

## `0x72032000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/fx.lua` | `local fx` / `return fx` | FX definitions |
| `scripts/prefabs/fx.lua` | `MakeFx` | Converts definitions into prefabs |
| `scripts/prefabs/fx.lua` | `startfx` | Creates the visible client entity |
| `scripts/debugsounds.lua` | `SoundEmitter.SoundDebug` | Wraps audio calls for debugging |
| `scripts/consolecommands.lua` | `c_sounddebug` / `c_sounddebugui` | Loads sound debugging |
| `scripts/mainfunctions.lua` | `require("debugsounds")` | Debug audio startup path |
| `scripts/stategraphs/SGwilson.lua` | `SoundEmitter:PlaySound` | Plays sounds on StateGraph frames |
| `scripts/stategraphs/SGwilson.lua` | `SpawnPrefab(...)` | Spawns FX on StateGraph frames |

### `0x72032111` FX Definitions

Search `scripts/fx.lua` for `local fx =`, `name =`, `bank =`, `build =`, `anim =`, and `return fx`.
These fields describe an effect but do not trigger it.

### `0x72032211` FX Prefab Factory

`MakeFx(t)` creates `Prefab(t.name, fn, assets)` for each definition in `fx.lua`.
`fn()` first creates a networked proxy entity.
On non-dedicated clients, the proxy defers `startfx()` by one frame to create a local entity with `AnimState`.

### `0x72032311` Sound Debugging

`debugsounds.lua` wraps `SoundEmitter.PlaySound`, `KillSound`, `KillAllSounds`, `SetParameter`, and `SetVolume`.
`c_sounddebug()` and `c_sounddebugui()` load it on demand with `require "debugsounds"`.

## `0x72033000` Runtime Flow

~~~mermaid
flowchart TD
    A["fx.lua defines table entries"]
    A --> B["prefabs/fx.lua MakeFx"]
    B --> C["network proxy prefab"]
    C --> D["client startfx local entity"]
    D --> E["AnimState bank / build / anim"]
    D --> F["optional SoundEmitter"]
    E --> G["animover / animqueueover removes entity"]
    H["StateGraph FrameEvent"]
    H --> I["SpawnPrefab(fx name)"]
    H --> J["SoundEmitter:PlaySound"]
    I --> C
    K["c_sounddebug / mainfunctions"]
    K --> L["debugsounds wraps SoundEmitter"]
~~~

### `0x72033111` FX Fields

`name` is the prefab name accepted by `SpawnPrefab`.
`bank`, `build`, and `anim` select animation resources.
`sound`, `sound2`, `sounddelay`, `fn`, and `fntime` add post-spawn behaviour.
`fx.lua` defines `ghostlyelixir_revive_fx` and `ghostlyelixir_revive_dripfx`.
It also defines `ghostlyelixir_player_revive_fx` and `ghostlyelixir_player_revive_dripfx`.
`ghostlyelixir_shadow_fx` and `ghostlyelixir_lunar_fx` use their respective meta5 sound events.
`carnival_unwrap_fx_s2` and `carnival_unwrap_fx_s3` serve second- and third-season figures.

### `0x72033211` Server and Client Roles

`fn()` creates a proxy with `Network` and the `FX` tag.
A dedicated server does not create the local display entity.
The master simulation removes the proxy after one second.

### `0x72033311` StateGraph Triggers

Search `SGwilson.lua` for `FrameEvent`, `SpawnPrefab`, and `SoundEmitter:PlaySound`.
The surrounding code identifies the animation frame that triggers the effect.

### `0x72033411` Debug Boundary

Sound debugging records call stacks, positions, looping sounds, and UI sounds.
It observes behaviour without changing gameplay authority.

## `0x72034111` Trace Boundary

Trace a `FrameEvent` or prefab call through its `fx.lua` definition, `MakeFx`, and the client-local `startfx` entity.

## `0x72035100` Verification

~~~bash
rg -n "local fx =|return fx|name =|bank =|build =|anim =|sound =|fn =" \
  scripts/fx.lua

rg -n \
  -e "ghostlyelixir_revive|ghostlyelixir_shadow_fx|ghostlyelixir_lunar_fx" \
  -e "carnival_unwrap_fx_s2|carnival_unwrap_fx_s3" \
  scripts/fx.lua \
  scripts/prefabs/ghostly_elixirs.lua \
  scripts/prefabs/carnivaldecor_figure.lua

rg -n "MakeFx|startfx|AddNetwork|AddAnimState|AddSoundEmitter|animover|animqueueover" \
  scripts/prefabs/fx.lua

rg -n "FrameEvent|SpawnPrefab|SoundEmitter:PlaySound" \
  scripts/stategraphs/SGwilson.lua

rg -n "c_sounddebug|c_sounddebugui|SoundEmitter.SoundDebug|require\\(\"debugsounds\"\\)" \
  scripts/consolecommands.lua \
  scripts/debugsounds.lua \
  scripts/mainfunctions.lua
~~~

### `0x72035111` Minimal Trace

Sample `splash` or `die_fx` in `fx.lua`, then inspect how `MakeFx` in `prefabs/fx.lua` builds the prefab.
Find its trigger with `rg "SpawnPrefab\\(\"splash"` or `rg "die_fx"`.
For audio, trace `c_sounddebugui()` to the StateGraph or prefab that calls `PlaySound`.
