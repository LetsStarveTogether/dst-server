# `0x31020000` Component Lifecycle

Component lifecycle is not one linear sequence.
Attachment, updates, persistence, component removal, and entity removal remain separate.
`EntityScript` joins these paths.

## `0x31021111` Purpose

This page follows component loading, updates, persistence, and listener cleanup.
It also distinguishes `RemoveComponent` from entity `Remove`.

## `0x31022000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/entityscript.lua` | `AddComponent` | Attach a component. |
| `scripts/entityscript.lua` | `StartUpdatingComponent` | Register normal updates and optional paused static updates. |
| `scripts/entityscript.lua` | `StartWallUpdatingComponent` | Register wall-clock updates. |
| `scripts/entityscript.lua` | `StopUpdatingComponent_Deferred` | Safely remove a component from update tables. |
| `scripts/entityscript.lua` | `LongUpdate` | Apply a large simulation time step. |
| `scripts/update.lua` | `Update` / `StaticUpdate` / `WallUpdate` / `LongUpdate` | Drive all four update channels. |
| `scripts/entityscript.lua` | `RemoveComponent` | Remove one component. |
| `scripts/entityscript.lua` | `Remove` | Destroy the whole entity. |
| `scripts/components/health.lua` | `Health` | Typical state component. |
| `scripts/components/inventory.lua` | `Inventory` | Container component with saved child references. |
| `scripts/standardcomponents.lua` | `MakeSmallBurnable` | Common prefab assembly pattern. |

### `0x31022111` Primary Anchors

Start at `AddComponent` in `entityscript.lua`.
Use `health.lua` for an updating state component and `inventory.lua` for a container that persists child references.
Use `Make*` functions in `standardcomponents.lua` to see how prefabs apply component bundles.

## `0x31023000` Runtime Flow

~~~mermaid
flowchart TD
    A["AddComponent(name)"]
    A --> B["LoadComponent(name)"]
    B --> C["ReplicateComponent(name)"]
    C --> D["cmp(self)"]
    D --> E["ComponentPostInit"]
    E --> F["RegisterComponentActions(name)"]
    D --> G["optional update channel"]
    D --> H["optional OnSave / OnLoad"]
    D --> I["optional ListenForEvent / WatchWorldState"]
    I --> J["RemoveComponent -> OnRemoveFromEntity"]
    I --> K["Entity Remove -> OnRemoveEntity"]
~~~

### `0x31023111` Attachment Boundaries

- `AddComponent` checks `lower_components_shadow` to prevent case-only duplicates.
- `LoadComponent` injects `WatchWorldState` and `StopWatchingWorldState` into the component class.
- `ReplicateComponent` runs before the component constructor.
- `RegisterComponentActions` runs after `ComponentPostInit`.
- Only components that request updates enter `StartUpdatingComponent`.
- Update paths include `OnUpdate`, `OnStaticUpdate`, `OnWallUpdate`, and `LongUpdate`.

## `0x31024111` Component Storage and Updates

- `AddComponent` loads the class with `require("components/"..name)`.
- `self.components[name] = loadedcmp` uses the original component name as its key.
- `lower_components_shadow[string.lower(name)]` exists only for duplicate detection.
- `StartUpdatingComponent` records every component in `updatecomponents`.
- `do_static_update` also records it in `updatestaticcomponents`.
- `StopUpdatingComponent` writes to `StopUpdatingComponents`, and deferred processing performs the removal.
- `StartWallUpdatingComponent` enters the wall-update set used by `update.lua` to call `OnWallUpdate(dt)`.
- `EntityScript:LongUpdate(dt)` calls each component that defines `LongUpdate(dt)`.

## `0x31024121` Update Channels

- A component registers normal updates with `inst:StartUpdatingComponent(self)`.
- The static flag adds paused `OnStaticUpdate(0)` without replacing normal `OnUpdate(dt)` registration.
- Real-time logic uses `inst:StartWallUpdatingComponent(self)` and `OnWallUpdate(dt)`.
- Day skips, cave migration, and simulation jumps use `LongUpdate(dt)` instead of frame updates.
- `StopUpdatingComponent_Deferred` removes components safely during the global update loop.

## `0x31024131` Persistence

- `Health:OnSave` stores health, penalties, and an optional maximum.
- `Health:OnLoad` restores optional maximum, penalty, `data.invincible`, and health or percentage.
- Value and penalty data trigger the HUD update path.
- `Inventory:OnSave` calls `GetSaveRecord` for item slots, equipment slots, and the active item.
- `Inventory:OnLoad` recreates items with `SpawnSaveRecord` and returns them to inventory or equipment slots.
- `SetPersistData` handles `add_component_if_missing` and the entity `OnPreLoad` first.
- Components that need reference repair use `LoadPostPass(newents, savedata)`.

## `0x31024141` Listener Cleanup

- `ListenForEvent` writes both `source.event_listeners` and `listener.event_listening`.
- External listeners should call `RemoveEventCallback` from `OnRemoveFromEntity` or `OnRemoveEntity`.
- `LoadComponent` injects `WatchWorldState`, but subscriptions remain recorded on `inst.worldstatewatching`.
- Entity `Remove` calls `StopAllWatchingWorldStates` and `RemoveAllEventCallbacks` as final cleanup.

## `0x31024151` Removal Hooks

- `RemoveComponent` calls component `OnRemoveFromEntity`, then unregisters its replica and actions.
- `EntityScript:Remove` calls global `OnRemoveEntity(self.GUID)` before `PushEvent("onremove")`.
- After `onremove`, it clears watchers, event callbacks, and pending tasks.
- Full entity removal calls `OnRemoveEntity` on components and replica components.
- `mainfunctions.lua` removes the entity from `Ents`, `BrainManager`, `SGManager`, and update tables.

## `0x31024161` Standard Assembly Helpers

- `MakeSmallBurnable`, `MakeMediumBurnable`, and `MakeLargeBurnable` attach `burnable`.
- They call `inst:AddComponent("burnable")`.
- These helpers configure callbacks without changing the `AddComponent` lifecycle.
- `MakeNoGrowInWinter` demonstrates a component calling `WatchWorldState`.
- Use these helpers to study prefab composition, not component class mechanics.

## `0x31025100` Verification

~~~bash
rg -n "AddComponent|StartUpdatingComponent|StopUpdatingComponent_Deferred|RemoveComponent" \
  scripts/entityscript.lua \
  scripts/standardcomponents.lua
rg -n "StartWallUpdatingComponent|OnStaticUpdate|OnWallUpdate|LongUpdate" scripts/entityscript.lua scripts/update.lua
rg -n "OnSave|OnLoad|OnRemoveFromEntity|OnRemoveEntity" \
  scripts/entityscript.lua \
  scripts/components/health.lua \
  scripts/components/inventory.lua
rg -n "ListenForEvent|RemoveEventCallback|WatchWorldState|StopAllWatchingWorldStates" \
  scripts/entityscript.lua \
  scripts/components/health.lua \
  scripts/components/inventory.lua
~~~

### `0x31025111` Minimal Trace

Trace `Health:DoFireDamage` into `StartUpdatingComponent`, then `Health:OnUpdate` into `StopUpdatingComponent`.
Trace `Inventory:OnSave` into child `GetSaveRecord` calls.
Finish by comparing an external `ListenForEvent`, `RemoveComponent`, and `EntityScript:Remove`.
