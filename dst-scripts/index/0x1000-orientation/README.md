# `0x10000000` Orientation and Reading Model

This directory covers BBC orientation, the source snapshot, reading workflows, terminology, and maintenance.

Directory-level guidance belongs in this README, while each standalone page covers one topic or reading path.

## `0x10001111` Scope and Boundaries

BBC defines the Markdown document tree, not just filename prefixes.

This section explains how to enter the index, interpret its codes, and choose an initial source-reading path.

Runtime, entity, action, world-generation, and reference details belong in later sections.

The directory name `0x1000-orientation` contains only its four-digit name code.

This README uses the restored full heading code `0x10000000` for its H1.

## `0x10002111` Pages

- [BBC Document Tree Specification](0x1001-bbc-encoding.md)
- [Source Snapshot](0x1002-source-snapshot.md)
- [Reading Workflows](0x1003-reading-workflows.md)
- [Glossary](0x1004-glossary.md)
- [Maintenance Rules](0x1005-maintenance.md)

## `0x10003111` Reading Order

Read this README first to understand the boundary between a directory README and a standalone topic page.

Continue with the BBC encoding page to see why heading codes are restored by slot instead of appended to filename codes.

Use the source snapshot to confirm which DST Lua files this index covers.

Then choose one runtime, action, AI, or world-generation path from the workflow page.

## `0x10004111` Initial Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/main.lua` | `loadfn` / `ModSafeStartup` | Installs the Lua loader and starts runtime content loading |
| `scripts/mainfunctions.lua` | `Start` / `RunScript` | Creates `TheFrontEnd` and loads `gamelogic` |
| `scripts/frontend.lua` | `FrontEnd` / screen stack | Manages screens, input dispatch, and debug panels |
| `scripts/gamelogic.lua` | `DoGenerateWorld` / `LoadSlot` | Selects save loading, world generation, and front-end transitions |
| `scripts/mods.lua` | `ModWrangler:LoadMods` / `ModManager` | Loads `modmain` and mod world-generation entry points |
| `scripts/modindex.lua` | `KnownModIndex` | Manages mod configuration, dependencies, and startup order |
| `scripts/entityscript.lua` | `EntityScript` / `AddComponent` | Connects entities, components, events, and actions |
| `scripts/componentactions.lua` | `EntityScript:CollectActions` | Collects component capabilities into candidate actions |
| `scripts/worldgen_main.lua` | `GenerateNew` | Starts world generation |

`main.lua` inserts `loadfn` into `package.loaders` before requiring `mainfunctions`.

`Start()` later creates `TheFrontEnd` and requires `gamelogic`.
