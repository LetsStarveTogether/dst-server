# `0x22030000` Network Runtime

This page connects server components, client replicas, classified entities, sessions, and shard messages.

It covers only the Lua-visible network model, not the C++ network manager.

## `0x22031111` Components and Replicas

Client action checks often read `inst.replica`, while authoritative state remains in the server component.

`entityreplica.lua` uses `_component` and `__component` tags to decide which replica components a client creates.

## `0x22031211` Classified Entities

`mainfunctions.lua` and `networking.lua` both inspect `player.player_classified`.

A classified object is a network entity, not a normal component.

It commonly carries private or frequently updated state for player replicas.

## `0x22032000` Source Anchors

| File | Entry | Role |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `ReplicateEntity` | Find an entity in `Ents` by GUID and start replication |
| `scripts/entityscript.lua` | `self.replica` | Create the replica container during entity initialization |
| `scripts/entityscript.lua` | `actionreplica` | Synchronize action-component markers through net byte arrays |
| `scripts/entityreplica.lua` | `ReplicateComponent` | Load `components/*_replica.lua` |
| `scripts/entityreplica.lua` | `ReplicateEntity` | Replicate tagged components after client deserialization |
| `scripts/entityreplica.lua` | `AddReplicableComponent` | Extend the replicable-component list from a mod |
| `scripts/netvars.lua` | `net_event` | Document and wrap Lua network-variable events |
| `scripts/netvars.lua` | `GetIdealUnsignedNetVarForCount` | Select an unsigned netvar for a count range |
| `scripts/networking.lua` | `SerializeUserSession` | Include a classified entity in player-session saves |
| `scripts/networking.lua` | `SerializeWorldSession` | Send world-session data to `TheNet` |
| `scripts/shardnetworking.lua` | `Shard_UpdateWorldState` | Record remote state and refresh shard metadata |
| `scripts/shardnetworking.lua` | `Shard_WorldSave` | Convert a shard-save callback into local `ms_save` |
| `scripts/shardnetworking.lua` | shard transaction helpers | Synchronize votes, bosses, merms, and shard transactions |

### `0x22032111` Replica Anchor

Find `REPLICATABLE_COMPONENTS`, then inspect `ReplicateComponent`.

Replica modules use `name .. "_replica"`.

### `0x22032211` Entity Anchor

Find `self.replica = { _ = {}, inst = self }`; its metatable exposes objects stored in the internal `_` table.

### `0x22032311` Action Anchor

Find `actioncomponents`, `inherentactions`, and `modactioncomponents`.

These net byte arrays synchronize component markers needed for action collection.

### `0x22032411` Netvar Anchor

Read the `netvar:set` and `netvar:value` comments in `scripts/netvars.lua`.

Server and client must declare matching netvars on an entity or deserialization fails.

## `0x22033000` Replica Flow

~~~mermaid
flowchart TD
    A["server component"]
    A --> B["EntityScript:ReplicateComponent"]
    B --> C["add _component tag"]
    C --> D["client deserializes tags"]
    D --> E["EntityScript:ReplicateEntity"]
    E --> F["require components/name_replica"]
    F --> G["inst.replica.name"]
~~~

### `0x22033111` Server Tags

`EntityScript:ReplicateComponent` accepts only names in `REPLICATABLE_COMPONENTS`.

The server builds the component tag as `"_"..name`.

If `"__"..name` already exists, it removes that tag and returns early.

### `0x22033211` Client Construction

After initial tag deserialization, the client calls `EntityScript:ReplicateEntity()`.

`EntityScript:ReplicateEntity` scans `REPLICATABLE_COMPONENTS`.

It creates replicas only for components tagged `_name` or `__name`.

### `0x22033311` Classified Attachment

A classified entity can attach to a pre-replicated or unreplicated component.

`TryAttachClassifiedToReplicaComponent` returns `false` when the replica component does not exist.

### `0x22033411` Shard Messages

Shard synchronization does not use the normal replica-component table.

`Shard_UpdateWorldState` receives remote connection state and updates `ShardConnected` and `ShardList`.

It then refreshes portal destinations, server tags, and world-generation data.

On a ready connection, `Shard_OnShardConnected` sends master settings or requests a resync.

`Shard_WorldSave` only pushes `ms_save` on the local master shard.

The engine-facing shard layer coordinates the wider save.

The same file handles portals, votes, bosses, merms, and shard transactions.

## `0x22034111` Replica Container

`self.replica._` holds replica objects by name.

Each object is stored at `self.replica._[name]`.

External code normally reads them through fields such as `inst.replica.inventory`.

## `0x22034211` Action Replication

`actionreplica` stores the network arrays for action components, inherent actions, and mod action components.

Trace action collection through both `componentactions.lua` and these network fields.

## `0x22034311` Session Serialization

Player-session saving calls `player:GetSaveRecord()`.

The server passes `player.player_classified.entity` to `TheNet:SerializeUserSession()`.

## `0x22034411` Netvar Selection

`GetIdealUnsignedNetVarForCount` returns `net_tinybyte` for counts up to `7` and `net_smallbyte` for counts up to `63`.

It returns `nil` above the `net_uint` range.

## `0x22034511` Connected Shards and Transactions

`shardnetworking.lua` tracks connected shards, portals, and server world-generation data as a cross-world message bus.

For surface, cave, or multi-shard behaviour, inspect both `networking.lua` and `shardnetworking.lua`.

## `0x22035100` Verification

~~~bash
rg -n "REPLICATABLE_COMPONENTS|ReplicateComponent|ReplicateEntity" \
  scripts/entityreplica.lua \
  scripts/entityscript.lua \
  scripts/netvars.lua \
  scripts/mainfunctions.lua \
  scripts/networking.lua
rg -n "actionreplica|player_classified|SerializeUserSession" \
  scripts/entityreplica.lua \
  scripts/entityscript.lua \
  scripts/netvars.lua \
  scripts/mainfunctions.lua \
  scripts/networking.lua
rg -n \
  -e "SerializeWorldSession" \
  -e "Shard_UpdateWorldState" \
  -e "Shard_OnShardConnected" \
  -e "Shard_SyncWorldSettings" \
  -e "Shard_WorldSave" \
  -e "ShardPortals" \
  -e "Shard_CreateTransaction" \
  scripts/networking.lua \
  scripts/shardnetworking.lua
~~~

### `0x22035111` Next Read

Read the component allowlist in `entityreplica.lua`.

Follow server tag creation through client replica construction.

For cross-world issues, add the state, portal, and transaction paths in `shardnetworking.lua`.
