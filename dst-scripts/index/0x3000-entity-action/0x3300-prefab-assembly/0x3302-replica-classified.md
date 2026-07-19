# `0x33020000` Replicas and Classifieds

Replicas project server component state as read-only, predicted, or privately visible state.
There is no `scripts/replica` directory.
Replicatable components live in `scripts/components/*_replica.lua`.
Classifieds are hidden network prefabs in `scripts/prefabs/*_classified.lua`.

## `0x33021111` Component to Replica

`EntityScript:AddComponent(name)` loads the server component and calls `self:ReplicateComponent(name)`.
It then constructs the component and stores it in `self.components[name]`.
When `name` belongs to `REPLICATABLE_COMPONENTS`, `ReplicateComponent` loads `components/name_replica.lua`.
Both server and client can own replica objects, so replicas are not client-only.

## `0x33021121` Tag-Driven Reconstruction

On the master simulation, `ReplicateComponent` adds a `"_"..name` tag.
After initial tag deserialization, `EntityScript:ReplicateEntity()` scans `_name` and `__name`.
It calls `ReplicateComponent(name)` for each match.
`ValidateReplicaComponent(name, cmp)` also uses the `_name` tag to filter visible replicas.

## `0x33021131` Classified Role

A classified is a hidden network prefab that usually has `Network`, the `CLASSIFIED` tag, and netvars.
A classified such as `writeable_classified` can instead carry only private visibility and binding state.
Classifieds are optional.
They suit private, high-frequency, numerous, owner-specific, container, or item fields.

## `0x33022000` Source Anchors

| File | Entry | Purpose |
| --- | --- | --- |
| `scripts/entityscript.lua` | `AddComponent`, `RemoveComponent` | Change a server component and its replica. |
| `scripts/entityreplica.lua` | `REPLICATABLE_COMPONENTS` | List built-in replicatable components. |
| `scripts/entityreplica.lua` | `ReplicateComponent` | Add `_name` and construct `components/name_replica.lua`. |
| `scripts/entityreplica.lua` | `UnreplicateComponent` | Remove `_name` and leave a `__name` marker. |
| `scripts/entityreplica.lua` | `TryAttachClassifiedToReplicaComponent` | Attach a classified to a replica. |
| `scripts/netvars.lua` | `net_event`, `GetIdealUnsignedNetVarForCount` | Define netvars and event pulses. |
| `scripts/components/health_replica.lua` | `AttachClassified`, `SetCurrent` | Read player health classified state. |
| `scripts/components/inventory_replica.lua` | `AttachClassified` | Read the inventory classified state table. |
| `scripts/components/container_replica.lua` | `Network:SetClassifiedTarget` | Target container state. |
| `scripts/prefabs/player_classified.lua` | `OnEntityReplicated` | Bind player state to replicas. |
| `scripts/prefabs/inventory_classified.lua` | `OnEntityReplicated` | Provide private inventory state. |

### `0x33022111` Built-in Replica Components

`REPLICATABLE_COMPONENTS` contains 19 components, each with a matching `scripts/components/*_replica.lua` file.

| Group | Components |
| --- | --- |
| Actions and crafting | `builder`, `combat`, `constructionsite` |
| Containers and items | `container`, `inventory`, `inventoryitem`, `stackable`, `writeable` |
| Equipment and tools | `equippable`, `fishingrod`, `oceanfishingrod` |
| Character state | `health`, `hunger`, `moisture`, `rider`, `sanity`, `sheltered` |
| Relationships and names | `follower`, `named` |

`AddReplicableComponent(name)` lets mods extend this set.

### `0x33022121` `_name` and `__name`

`ReplicateComponent` adds `_name` on the master simulation.
If `__name` already exists, it removes `__name` and returns without constructing a replica.
This marks a prereplicated or previously unreplicated component shape.
`UnreplicateComponent` removes `_name` and adds `__name` on the master simulation.
`PrereplicateComponent` is equivalent to replicating and then unreplicating.
The player prefab uses manual `_health`, `_hunger`, `_sanity`, and similar tags to expose replica shape early.

### `0x33022211` Classified Binding

A typical classified stores `_parent`.
Client `OnEntityReplicated` calls `TryAttachClassifiedToReplicaComponent` on the parent.
If the target replica does not exist yet, the classified temporarily remains at `parent.<name>_classified`.
The replica constructor checks that field later and retries `AttachClassified`.
This two-sided fallback removes any strict construction-order requirement.

### `0x33022221` Classified Visibility

`Network:SetClassifiedTarget(target)` controls who can see a classified.
`inventory_replica.lua`, `container_replica.lua`, and `inventoryitem_replica.lua` change the target.
They use the opener, owner, or item state.
`player_classified` attaches to the player as a state bus.
The server writes it, and the local player reads it.
Item-specific classifieds are commonly spawned by their item prefab and bound to an owner or target entity.

## `0x33023000` Replication Flow

~~~mermaid
flowchart TD
    A["server prefab fn"]
    A --> B["inst:AddComponent(name)"]
    B --> C["EntityScript:ReplicateComponent(name)"]
    C --> D["add _name tag"]
    C --> E["construct components/name_replica.lua"]
    E --> F["server component writes inst.replica.name"]
    D --> G["network tag synchronization"]
    F --> H["netvar or classified synchronization"]
    G --> I["client ReplicateEntity"]
    I --> J["client constructs name_replica"]
    H --> K{"classified present"}
    K -->|no| L["replica reads netvars / tags / server component"]
    K -->|yes| M["OnEntityReplicated or constructor fallback calls AttachClassified"]
    M --> N["replica reads classified fields"]
~~~

### `0x33023111` Replicas on the Server

`AddComponent` replicates before constructing the server component.
A server component constructor or setter can therefore write `inst.replica.<name>` immediately.
`components/health.lua` writes current health, maximum health, and penalty through `inst.replica.health`.
`components/combat.lua` also uses `inst.replica.combat` for shared checks.
These calls are not client-only APIs.

### `0x33023121` Replicas Without Classifieds

Some replica files define netvars directly.
Examples are `combat_replica.lua`, `moisture_replica.lua`, `named_replica.lua`, and `stackable_replica.lua`.
Other replicas mainly read server components or tags.
For example, `health_replica.lua` prefers `inst.components.health` on a local host.
A replica does not require a classified.

### `0x33023211` `player_classified`

`player_classified` adds `Transform`, `MapExplorer`, `Network`, and the `CLASSIFIED` tag.
It declares health, hunger, sanity, builder, combat, rider, SG, and locomotor fields.
It also carries HUD, camera, frontend, and related state.
`OnEntityReplicated` first calls the parent `AttachClassified`.
It then tries to attach the classified to multiple replica components.
Player server components also write fields on `inst.player_classified` directly.

### `0x33023221` Inventory and Container Classifieds

`inventory_classified` and `container_classified` are large slot state tables.
On the server, `inventory_replica.lua` spawns `inventory_classified`.
It listens for active-item, slot, and equipment events.
On the server, `container_replica.lua` spawns `container_classified` and changes its visible target for each opener.
Both project high-frequency slot state to permitted clients through classifieds.

### `0x33023231` Item and Construction Classifieds

`inventoryitem_replica.lua` spawns `inventoryitem_classified`.
It carries image, atlas, owner, deployment mode, moisture, temperature, recharge, and related item state.
`constructionsite_replica.lua` spawns `constructionsite_classified`.
It carries construction slot counts and targets the classified to the builder.
`writeable_replica.lua` spawns `writeable_classified`, which has no netvar fields of its own.

## `0x33024111` Classified Catalog

There are 15 `*_classified.lua` files under `scripts/prefabs`.

| File | Primary role |
| --- | --- |
| `scripts/prefabs/player_classified.lua` | Private player state bus. |
| `scripts/prefabs/inventory_classified.lua` | Inventory slots and RPC forwarding state. |
| `scripts/prefabs/container_classified.lua` | Container slots and opener-visible state. |
| `scripts/prefabs/inventoryitem_classified.lua` | Item image, owner, deployment, durability, and temperature state. |
| `scripts/prefabs/constructionsite_classified.lua` | Construction slot count. |
| `scripts/prefabs/writeable_classified.lua` | Private binding signal for the writeable replica. |
| `scripts/prefabs/container_closed_receiveitem_classified.lua` | Short-lived closed-container receive-item event. |
| `scripts/prefabs/attunable_classified.lua` | Attunable player and source relationship. |
| `scripts/prefabs/pet_hunger_classified.lua` | Pet hunger and HUD state. |
| `scripts/prefabs/woby_commands_classified.lua` | Woby commands and bag state. |
| `scripts/prefabs/wx78_classified.lua` | WX78 energy, modules, shield, and UI state. |
| `scripts/prefabs/lucy_classified.lua` | Private Lucy speech state. |
| `scripts/prefabs/shadow_battleaxe_classified.lua` | Private shadow battleaxe speech state. |
| `scripts/prefabs/voidcloth_scythe_classified.lua` | Private voidcloth scythe speech state. |
| `scripts/prefabs/wagpunkhat_classified.lua` | Private wagpunk hat speech state. |

## `0x33024211` Large State Buses

`player_classified`, `inventory_classified`, and `container_classified` contain many netvars.
They also contain dirty events and forwarding functions.
Treat them as state buses rather than ordinary FX prefabs.

## `0x33024221` Replica-Owned Classifieds

Replicas manage `inventoryitem_classified`, `constructionsite_classified`, and `writeable_classified`.
Their lifecycle follows the parent entity or replica.
A client may receive the classified before the replica and attach it when the replica appears.

## `0x33024231` Feature-Specific Classifieds

`pet_hunger_classified`, `woby_commands_classified`, and `wx78_classified` carry feature-specific private state.
Weapon speech classifieds do the same.
They are not templates for every replica.
Trace each one back to the prefab or component that spawns it.

## `0x33025111` Netvar Types

Comments in `scripts/netvars.lua` list `net_bool`, `net_byte`, and `net_shortint`.
They also list `net_ushortint`, `net_float`, and `net_hash`.
`net_hash` accepts a string and converts it to a hash.
`net_entity` stores an entity instance.
`GetIdealUnsignedNetVarForCount` chooses an unsigned netvar type from the maximum count.

## `0x33025211` `net_event`

`net_event` wraps `net_bool`.
Its `push()` method toggles the Boolean value to fire the corresponding dirty event.
Use it for one-shot pulses rather than persistent state.
Fields such as `buildevent`, `attackedpulseevent`, and `learnrecipeevent` are event pulses.

## `0x33026111` Catalog Scope

`0x8202-component-catalog.md` covers every `components/*_replica.lua` path.
`0x8201-prefab-catalog.md` covers every `prefabs/*_classified.lua` path.
This page keeps only the conceptual lists of 19 built-in replicas and 15 classified files.

## `0x33026211` Common Misreadings

- Do not refer to `scripts/replica`; that directory does not exist.
- Classifieds are not required for every replica.
- Replicas are not client-only.
- The `__name` tag is part of the lifecycle.
- `net_event` is not ordinary Boolean state.

## `0x33027100` Verification

~~~bash
rg -n "function EntityScript:AddComponent|ReplicateComponent|ReplicateEntity|TryAttachClassified" \
  scripts/entityscript.lua \
  scripts/entityreplica.lua
rg -n "AttachClassified|OnEntityReplicated|net_|SetClassifiedTarget|TryAttachClassifiedToReplicaComponent" \
  scripts/components/health_replica.lua \
  scripts/components/inventory_replica.lua \
  scripts/components/container_replica.lua \
  scripts/prefabs/player_classified.lua \
  scripts/prefabs/inventory_classified.lua \
  scripts/prefabs/container_classified.lua
~~~

### `0x33027111` Minimal Trace

Read `EntityScript:AddComponent` to confirm that replication precedes server component construction.
Read `entityreplica.lua` to connect `_name`, `__name`, and `components/name_replica.lua`.
Trace `health.lua` into `health_replica.lua` to verify server writes to a replica.
Trace `inventory_replica.lua` into `inventory_classified.lua` to verify a large classified state table.
Finish with `container_replica.lua` to verify visibility changes through `Network:SetClassifiedTarget`.
