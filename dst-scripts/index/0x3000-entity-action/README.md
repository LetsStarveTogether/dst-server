# `0x30000000` Entities and Actions

This section starts at `EntityScript` and follows components, events, actions, prefab assembly, and replicas.
Exhaustive catalogs remain in `0x8000-reference`.

## `0x30001111` Purpose

This section answers two questions.
How is an entity created, extended with components, subscribed to events, and persisted?
How does player input become a candidate, `BufferedAction`, StateGraph transition, and component side effect?

## `0x30002000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `CreateEntity` | Create an `EntityScript` and register it in `Ents`. |
| `scripts/entityscript.lua` | `AddComponent` | Load, replicate, and register component actions. |
| `scripts/entityscript.lua` | `PushEvent_Internal` | Dispatch events to listeners, the SG, and the Brain. |
| `scripts/components/playercontroller.lua` | `PlayerController:DoAction` | Submit player input as an action. |
| `scripts/components/playeractionpicker.lua` | `GetLeftClickActions` | Select mouse action candidates. |
| `scripts/componentactions.lua` | `EntityScript:CollectActions` | Collect available actions from components. |
| `scripts/bufferedaction.lua` | `BufferedAction` | Store action execution context. |

### `0x30002111` Starting Points

Use `CreateEntity` to find entity creation.
Use `AddComponent` to follow component attachment.
Use `CollectActions` to enter the component action pipeline.

## `0x30003000` Runtime Map

~~~mermaid
flowchart TD
    A["CreateEntity"]
    A --> B["EntityScript"]
    B --> C["AddComponent"]
    C --> D["Component state"]
    B --> E["ListenForEvent / WatchWorldState"]
    E --> F["PushEvent_Internal"]
    F --> G["SG or Brain handler"]
    D --> H["PlayerActionPicker"]
    H --> I["CollectActions"]
    I --> J["BufferedAction"]
    J --> K["StateGraph action handler"]
    K --> L["Component side effect"]
~~~

### `0x30003111` Scope

This section covers shared entity and action mechanics, not feature-specific gameplay behaviour.

## `0x30004111` Pages

- [Entity Model](0x3100-entity-model/README.md)
- [EntityScript](0x3100-entity-model/0x3101-entityscript.md)
- [Component Lifecycle](0x3100-entity-model/0x3102-component-lifecycle.md)
- [Tags and Events](0x3100-entity-model/0x3103-tags-events.md)
- [Action Pipeline](0x3200-action-pipeline/README.md)
- [ComponentActions](0x3200-action-pipeline/0x3201-component-actions.md)
- [BufferedAction](0x3200-action-pipeline/0x3202-buffered-actions.md)
- [Player Input to Action](0x3200-action-pipeline/0x3203-player-input-action.md)
- [Prefab Assembly](0x3300-prefab-assembly/README.md)
- [Prefab Assembly Contract](0x3300-prefab-assembly/0x3301-prefab-contract.md)
- [Replicas and Classifieds](0x3300-prefab-assembly/0x3302-replica-classified.md)

## `0x30005100` Verification

~~~bash
rg -n "EntityScript|PlayerActionPicker|CollectActions|BufferedAction|PushBufferedAction" \
  scripts/mainfunctions.lua \
  scripts/entityscript.lua \
  scripts/components/playercontroller.lua \
  scripts/components/playeractionpicker.lua \
  scripts/componentactions.lua \
  scripts/bufferedaction.lua
~~~

### `0x30005111` Minimal Trace

Trace `CreateEntity` through `AddComponent`.
Then trace `input.lua`, `PlayerController`, and `PlayerActionPicker` into `BufferedAction`.
Finish at the SG or component code that changes state.
