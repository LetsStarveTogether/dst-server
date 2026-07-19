# `0x33000000` Prefab Assembly

This section follows prefabs from declaration and registration through spawning and network projection.
Complete `scripts/prefabs/` catalogs remain in `0x8000-reference`.

## `0x33001111` Scope

Use this section for registration, spawning, and network projection boundaries.

## `0x33001121` Catalog Boundaries

`0x8201-prefab-catalog.md` covers every file under `scripts/prefabs/`.
`0x8202-component-catalog.md` covers every `scripts/components/*_replica.lua` file.
This section keeps only runtime paths, key source anchors, and important boundaries.

## `0x33002111` Pages

- [Prefab Assembly Contract](0x3301-prefab-contract.md)
- [Replicas and Classifieds](0x3302-replica-classified.md)

## `0x33003111` Reading Order

Read `0x3301-prefab-contract.md` to connect `Prefab`, `LoadPrefabFile`, `RegisterPrefabsImpl`, and `SpawnPrefabFromSim`.
Then read `0x3302-replica-classified.md` to connect replica and classified files.
Follow their netvars and `Network:SetClassifiedTarget` visibility.
Use `0x8000-reference/README.md` for complete paths.

## `0x33004111` Confirmed Layout

There is no `scripts/replica` directory.
Built-in replica files are in `scripts/components/*_replica.lua`.
Classified files are in `scripts/prefabs/*_classified.lua`.
Prefab helpers are in `scripts/prefabutil.lua` and `scripts/standardcomponents.lua`.

## `0x33004121` Confirmed Counts

`entityreplica.lua` lists 19 built-in replicatable components.
`scripts/components` contains 19 tracked `_replica.lua` files.
`scripts/prefabs` contains 15 tracked `_classified.lua` files.
