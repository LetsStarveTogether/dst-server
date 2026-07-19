# `0x10050000` Maintenance Rules

Verify source anchors first, explain the smallest useful runtime path, and keep large inventories in the reference section.

## `0x10051111` Purpose

Keep source facts, topic boundaries, and the reference inventory consistent.

Documentation changes must remain in the index Markdown and must not modify `scripts` Lua to make a claim true.

Keep BBC codes, heading levels, and source paths simple, readable, and traceable.

## `0x10052000` Source Anchors

| File | Entry point | Purpose |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `SaveGame` | Audits the save path |
| `scripts/worldgen_main.lua` | `CheckMapSaveData` | Validates generated map data |
| `scripts/entityscript.lua` | `GetPersistData` | Traces entity persistence |
| `scripts/entityreplica.lua` | `ReplicateEntity` | Traces client replicas |

### `0x10052111` Source Checks

Find `SaveGame` in `scripts/mainfunctions.lua` first.

For world-generation pages, also inspect `CheckMapSaveData`.

For entity or networking pages, inspect `GetPersistData`, `ReplicateEntity`, or `ReplicateComponent`.

## `0x10053000` Documentation Workflow

1. Locate each claimed path and symbol in the current `scripts` snapshot.
2. Trace enough callers and callees to establish the execution boundary.
3. Edit only the smallest relevant topic or reference page.
4. Run scoped Markdown, path, source-query, and diff checks.

### `0x10053111` Content Boundaries

- Topic pages explain runtime relationships and do not contain large complete inventories.
- Reference pages contain complete file inventories and directory indexes.
- Every new source anchor must exist under `scripts`.
- Snapshot pages must name the source commit and define their counting scope.
- Every BBC code in a heading must remain inside a code span.

## `0x10054111` Page Requirements

- Start each non-reference topic with a clear H2 and add H3 or H4 only when the content needs them.
- Do not create empty intermediate headings merely to fill H5.
- Every literal source path must point to a real file.
- Maintain complete coverage inventories only in `0x8000-reference`.
- Use a table or diagram only when it makes a relationship easier to verify.
- Include a runnable source query when a page depends on specific symbols or file counts.

## `0x10055100` Verification

Run this source check from `dst-scripts`.

~~~bash
rg -n "SaveGame|CheckMapSaveData|GetPersistData|ReplicateEntity" \
  scripts/mainfunctions.lua \
  scripts/worldgen_main.lua \
  scripts/entityscript.lua \
  scripts/entityreplica.lua
~~~

### `0x10055111` Next Step

Confirm source anchors before removing repeated prose or tightening the topic boundary.
