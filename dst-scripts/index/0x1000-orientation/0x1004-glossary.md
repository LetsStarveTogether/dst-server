# `0x10040000` Glossary

This glossary defines common runtime terms for source reading without replacing the reference inventory.

## `0x10041111` Purpose

Use these definitions to distinguish `Prefab`, `EntityScript`, `Component`, `Replica`, `StateGraph`, and `Brain`.

Each definition points to source code rather than describing only a game concept.

## `0x10042000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/prefabs.lua` | `Prefab = Class` | Defines Prefab objects |
| `scripts/entityscript.lua` | `AddComponent` | Attaches components |
| `scripts/entityreplica.lua` | `ReplicateComponent` | Attaches client replicas |
| `scripts/stategraph.lua` | `StateGraphInstance` | Implements presentation state-machine instances |
| `scripts/brain.lua` | `BrainWrangler` / `BrainManager` | Schedules AI updates |

### `0x10042111` Primary Inspection

Find `AddComponent` in `scripts/entityscript.lua`.

Then find `ReplicateComponent` in `scripts/entityreplica.lua`.

It marks the boundary between server components and client replicas.

## `0x10043000` Runtime Relationships

~~~mermaid
flowchart TD
    A["Prefab"] --> B["EntityScript"]
    B --> C["Components"]
    B --> D["Replicas"]
    B --> E["StateGraph"]
    B --> F["Brain"]
    C -. client-facing API .-> D
~~~

### `0x10043111` Core Concepts

- A `Prefab` is a descriptor for an entity-construction function, assets, and dependencies, not a live entity instance.
- `EntityScript` is the Lua-side entity shell that holds `components`, `replica`, event listeners, and buffered actions.
- A `Component` usually holds authoritative behaviour state.
- A `*_replica` exposes a client-readable or predictive interface.
- A `StateGraph` coordinates action presentation, state tags, events, and animation windows.
- A `Brain` uses a behaviour tree to produce intent and usually does not play animations directly.

## `0x10044111` Terms and Boundaries

| Term | Source anchor | Reading boundary |
| --- | --- | --- |
| `Prefab` | `scripts/prefabs.lua` | Entity assembly, asset dependencies, and child prefabs |
| `EntityScript` | `scripts/entityscript.lua` | Entity lifecycle, components, events, actions, and persistence |
| `Component` | `scripts/components/*.lua` | Server-authoritative behaviour state |
| `Replica` | `scripts/components/*_replica.lua` | Client-side read-only or predictive interfaces |
| `Classified` | `scripts/prefabs/*_classified.lua` | Network-variable containers |
| `StateGraph` | `scripts/stategraph.lua` | Animation states, action windows, and event responses |
| `Brain` | `scripts/brain.lua` | AI scheduling and behaviour-tree entry points |

## `0x10045100` Verification

~~~bash
rg -n "Prefab = Class|AddComponent|ReplicateComponent|SetStateGraph|SetBrain" \
  scripts/prefabs.lua \
  scripts/entityscript.lua \
  scripts/entityreplica.lua
rg -n "StateGraphInstance|BrainWrangler|BrainManager" \
  scripts/stategraph.lua \
  scripts/brain.lua
~~~

### `0x10045111` Next Step

Use the glossary to establish ownership boundaries before opening a topic page.

Move long instance lists to the reference section instead of expanding this page.
