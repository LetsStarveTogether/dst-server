# `0x63020000` Ocean and Boating

A boat is a moving platform, a physics entity, and a collection of attachments.
Do not treat it as ordinary ground or limit the trace to the `boat.lua` prefab.

## `0x63021111` Purpose

`boat.lua` assembles `walkableplatform` and `boatphysics`.
`walkableplatform` tracks players and items on the platform.
`boatphysics` combines velocity, steering, mast force, anchors, and other drag sources.
Players detect boarding and departure through `walkableplatformplayer`.

## `0x63021211` World-Simulation Boundary

The `0x50000000` section covers ocean tiles and world generation.
This guide starts with an existing runtime world and follows player, boat, attachment, and crafting interactions.

## `0x63022000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/recipes.lua` | `boat_item` / `anchor_item` | Declares ocean recipes |
| `scripts/prefabs/boat.lua` | `create_common_pre` / `create_master_pst` | Assembles boat bodies |
| `scripts/components/walkableplatform.lua` | `SetEntitiesOnPlatform` | Tracks platform occupants |
| `scripts/components/walkableplatformplayer.lua` | `TestForPlatform` | Detects player boarding and departure |
| `scripts/entityscript.lua` | `GetCurrentPlatform` | Reports an entity's platform |
| `scripts/components/boatphysics.lua` | `BoatPhysics` | Controls speed, steering, and drag |
| `scripts/components/boatcrew.lua` | `RemoveMember` | Cleans up crews and pirate ship data |
| `scripts/components/boatleak.lua` | `SetState` | Manages leak repair states |
| `scripts/components/mast.lua` | `SetBoat` / `CalcSailForce` / `CalcMaxVelocity` | Adds mast force |
| `scripts/components/anchor.lua` | `SetBoat` | Binds an anchor to a boat |
| `scripts/components/boatdrag.lua` | `drag` / `sailforcemodifier` | Supplies drag parameters |
| `scripts/prefabs/anchor.lua` | `SGanchor` | Assembles the anchor and its StateGraph |
| `scripts/prefabs/mast.lua` | `mast` component | Assembles masts and upgrades |

### `0x63022111` Boat Assembly

`create_common_pre` adds networking, physics, `walkableplatform`, and `healthsyncer`.
It also adds `waterphysics`, `reticule`, and `boatringdata`.
On the master simulation, `create_master_pst` adds `hull`, `repairable`, `boatring`, and `hullhealth`.
It also adds `boatphysics`, `boatdrifter`, `health`, and `SGboat`.

### `0x63022211` Platform Tracking

`WalkablePlatform:SetEntitiesOnPlatform` scans the platform radius.
It attaches each match through `EntityScript:AddPlatformFollower` (`AddPlatformFollower`).
On the server, `WalkablePlatformPlayer:TestForPlatform` uses `inst:GetCurrentPlatform()`.
On the client, it uses `TheWorld.Map:GetPlatformAtPoint(...)`.

## `0x63023000` Runtime Flow

~~~mermaid
flowchart TD
    A["recipes.lua: boat_item / anchor_item / mast_item"]
    A --> B["builder.lua: SpawnPrefab(product)"]
    B --> C["boat.lua: create_common_pre"]
    C --> D["walkableplatform"]
    C --> E["boatphysics"]
    D --> F["walkableplatformmanager"]
    D --> G["walkableplatformplayer: TestForPlatform"]
    E --> H["mast.lua: AddMast / CalcMaxVelocity"]
    E --> I["anchor.lua + boatdrag.lua: AddBoatDrag"]
    H --> J["boatphysics: GetMaxVelocity / OnUpdate"]
    I --> J
~~~

### `0x63023111` Crafting to Boat Prefab

`boat_item`, `boat_grass_item`, `anchor_item`, and `mast_item` use the normal crafting path.
After the product enters the world, its deploy kit or prefab creates the final boat, anchor, or mast.
Inspect the recipe, deploy kit, and final prefab together.

### `0x63023211` Platform Coordinates

On the master simulation, `EntityScript:GetCurrentPlatform` reads `self.platform`.
On the client, it reads the engine-level `entity:GetPlatform()`.
`EntityScript:GetSaveRecord` stores platform-relative coordinates and `walkableplatform:GetUID()`.
Saving, movement, and departure for boat occupants cannot use ordinary world positions alone.

### `0x63023311` Boat Motion

`BoatPhysics` stores `velocity_x`, `velocity_z`, `masts`, `magnets`, and `boatdraginstances`.
`ApplyRowForce` applies rowing force.
`GetMaxVelocity` combines mast and magnet sources.
`GetBoatDrag` and `GetTotalAnchorDrag` combine anchors and other drag components.
`walkableplatform` uses `SetHalting` as an emergency brake for invalid positions or obstacles.
It is not a general gameplay API.

### `0x63023321` Crew and Leak Repair

When the last member leaves, `Boatcrew:RemoveMember` removes ship data from `piratespawner`.
It also removes `vanish_on_sleep` and `boatcrew` from the boat.
The `repaired_tape` state in `boatleak.lua` uses the Winona tape repair sound.

## `0x63024111` Masts and Anchors

In `components/mast.lua`, `Mast:SetBoat` calls `RemoveMast` on the old boat and `AddMast` on the new boat.
`Mast:CalcMaxVelocity` and `Mast:CalcSailForce` determine the mast's contribution to `BoatPhysics`.
`components/anchor.lua` finds its boat through `inst:GetCurrentPlatform()`.
`prefabs/anchor.lua` adds `anchor`, `boatdrag`, and `SGanchor`.
`boatdrag` supplies drag, maximum-speed modifiers, and sail-force modifiers.

## `0x63024211` Player Platform State

`scripts/prefabs/player_common.lua` adds `walkableplatformplayer` before pristine setup.
Its lifecycle methods live in `scripts/components/walkableplatformplayer.lua`.
`GetOnPlatform` calls `Transform:SetIsOnPlatform(true)`.
It registers the player through `platform.components.walkableplatform:AddPlayerOnPlatform`.
`GetOffPlatform` unregisters the player, stops boat camera and music checks, and clears `self.platform`.

## `0x63024311` Building Restrictions

`NoBoats_testfn` lives in `scripts/recipes.lua` (`recipes.lua`).
It rejects every point on a platform.
Some scaffolds, Winters Feast tables, and deconstruction recipes use it.
Ocean restrictions therefore also enter through recipe placement tests.

## `0x63025100` Verification

~~~bash
rg -n "boat_item|anchor_item|mast_item|NoBoats_testfn" \
  scripts/recipes.lua

rg -n "create_common_pre|create_master_pst|walkableplatform|boatphysics|SGboat" \
  scripts/prefabs/boat.lua

rg -n "SetEntitiesOnPlatform|AddEntityToPlatform|GetOnPlatform|TestForPlatform|GetCurrentPlatform" \
  scripts/components/walkableplatform.lua \
  scripts/components/walkableplatformplayer.lua \
  scripts/entityscript.lua

rg -n "AddMast|RemoveMast|CalcSailForce|AddBoatDrag|ApplyRowForce|GetMaxVelocity|SetHalting" \
  scripts/components/boatphysics.lua \
  scripts/components/mast.lua \
  scripts/components/anchor.lua \
  scripts/components/boatdrag.lua

rg -n "RemoveMember|RemoveShipData|repaired_tape|ChangeToRepaired" \
  scripts/components/boatcrew.lua \
  scripts/components/boatleak.lua
~~~

### `0x63025111` Reading Order

Start with boat assembly in `scripts/prefabs/boat.lua`.
Read `walkableplatform` and `walkableplatformplayer` to trace player and item attachment.
Finish with `boatphysics`, `mast`, `anchor`, and `boatdrag` to see how attachments change speed and braking.
