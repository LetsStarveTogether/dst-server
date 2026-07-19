# `0x21000000` Boot and Main Loop

This directory covers startup, per-tick updates, and task scheduling.

## `0x21001111` Scope

Use these pages to trace the Lua-visible runtime from engine startup through world activation and recurring updates.

## `0x21002111` Pages

- [Boot Sequence](0x2101-boot-sequence.md)
- [Main Loop](0x2102-main-loop.md)
- [Scheduler](0x2103-scheduler.md)

## `0x21003111` Reading Order

Start with `0x2101-boot-sequence.md`, then continue to `0x2102-main-loop.md`.

Open the scheduler page when tracing delayed callbacks or coroutines.

For exhaustive inventories, see [Reference](../../0x8000-reference/README.md).
