# `0x31030000` Tags and Events

Tags provide fast state checks.
Events notify listeners, the StateGraph, and the Brain about entity state changes.
World state watchers provide a third notification path.

## `0x31031111` Purpose

Tags delegate to the underlying `entity`.
Event subscriptions maintain indexes on both source and listener.
World state subscriptions register with `TheWorld.components.worldstate`.

## `0x31032000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/entityscript.lua` | `AddTag` | Tag API. |
| `scripts/entityscript.lua` | `ListenForEvent` | Event subscription. |
| `scripts/entityscript.lua` | `PushEvent_Internal` | Event dispatch. |
| `scripts/entityscript.lua` | `WatchWorldState` | World state subscription. |
| `scripts/stategraph.lua` | `HandleEvent` | SG event handling. |
| `scripts/brain.lua` | `Brain:PushEvent` | Brain event handling. |
| `scripts/componentutil.lua` | Tag constants and `PushEvent` calls | Gameplay-level examples. |
| `scripts/prefabs/spider_healer_item.lua` | `SPIDER_TAGS` / `SPIDER_IGNORE_TAGS` | Include and exclude filters for healing targets. |

### `0x31032111` Primary Anchors

Find `AddTag`, `ListenForEvent`, and `PushEvent_Internal` in `entityscript.lua`.
Use tag constants and `FindEntities` calls in `componentutil.lua` to see gameplay-level filtering.

## `0x31033000` Runtime Flow

~~~mermaid
flowchart TD
    A["AddTag / RemoveTag"]
    A --> B["entity:AddTag / entity:RemoveTag"]
    C["ListenForEvent(event, fn, source)"]
    C --> D["source.event_listeners"]
    C --> E["listener.event_listening"]
    F["PushEvent_Internal"]
    F --> G["callbacks fn(self, data)"]
    F --> H["SG immediate or buffered event"]
    F --> I["Brain:PushEvent"]
    J["WatchWorldState"]
    J --> K["TheWorld.components.worldstate"]
~~~

### `0x31033111` Dispatch Boundaries

- `ListenForEvent` defaults `source` to `self`.
- `source.event_listeners[event][listener]` finds callbacks during dispatch.
- `listener.event_listening[event][source]` supports reverse cleanup.
- `PushEvent_Internal` calls listener callbacks synchronously first.
- The SG path differs between `PushEvent` and `PushEventImmediate`.
- The Brain receives `Brain:PushEvent(event, data)` after the SG branch.

## `0x31034111` Tags

- `AddTag`, `RemoveTag`, `HasTag`, and `HasTags` delegate to `self.entity`.
- `HasTags` is equivalent to `HasAllTags`.
- `HasOneOfTags` is equivalent to `HasAnyTag`.
- Tags commonly drive action collection, target filtering, collision rules, and SG conditions.

## `0x31034121` SG and Brain Dispatch

- `PushEvent` calls `PushEvent_Internal(event, data, false)`.
- `PushEventImmediate` calls `PushEvent_Internal(event, data, true)`.
- A non-immediate event enters the SG buffer only when the SG listens for it and `SGManager:OnPushEvent` accepts it.
- An immediate event calls `self.sg:HandleEvent(event, data)` directly.
- `Brain:PushEvent` looks up `self.events[event]` and invokes its handler.

## `0x31034131` World State Watchers

- `EntityScript:WatchWorldState` records the subscription and calls `TheWorld.components.worldstate:AddWatcher`.
- `StopWatchingWorldState` calls the corresponding `RemoveWatcher`.
- `LoadComponent` injects `WatchWorldState` and `StopWatchingWorldState` into component classes.
- A component call to `self:WatchWorldState` is still recorded on `self.inst.worldstatewatching`.
- Entity `Remove` calls `StopAllWatchingWorldStates`.

## `0x31034141` Gameplay Examples

- `NON_LIFEFORM_TARGET_TAGS` and `SOULLESS_TARGET_TAGS` in `componentutil.lua` define shared tag filters.
- The include and exclude arguments to `TheSim:FindEntities` consume these lists.
- `componentutil.lua` also calls `inst:HasTag`, `ent:PushEvent`, and `PushEventImmediate`.
- Tags and events are shared by components, prefabs, and helpers rather than private component state.
- The AoE spider scan requires the `spider` tag and excludes `creaturecorpse`.
- A required tag alone does not guarantee that a later component access is valid.

## `0x31035100` Verification

~~~bash
rg -n "AddTag|HasTag|ListenForEvent|RemoveEventCallback|PushEvent_Internal" \
  scripts/entityscript.lua \
  scripts/stategraph.lua \
  scripts/brain.lua \
  scripts/componentutil.lua
rg -n "PushEventImmediate|WatchWorldState|StopAllWatchingWorldStates" scripts/entityscript.lua scripts/componentutil.lua
rg -n "SPIDER_TAGS|SPIDER_IGNORE_TAGS|FindEntities|components.health" scripts/prefabs/spider_healer_item.lua
~~~

### `0x31035111` Minimal Trace

Trace one `ListenForEvent` and confirm both source and listener indexes.
Trace one ordinary `PushEvent` and compare SG buffering with the direct Brain handler.
Finish with a component `WatchWorldState` call and its cleanup during entity removal.
