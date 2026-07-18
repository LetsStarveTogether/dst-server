# `0x21020000` 主循环

主循环页以 `scripts/update.lua` 为中心。
`scheduler.lua`、组件更新、StateGraph 和 Brain 是 `Update(dt)` 中相邻的更新阶段，不是彼此的调用链。

## `0x21021111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

目标是确认每个 tick 的 Lua 更新顺序。
普通游戏 tick 进入 `Update(dt)`。
静态 tick 进入 `StaticUpdate(dt)`。
暂停服务器不会执行普通 `Update(dt)`，而是由 `StaticUpdate(dt)` 处理静态 scheduler 和暂停态静态组件。

## `0x21022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/update.lua` | `Update` | 非暂停游戏 tick 的 Lua 主更新入口 |
| `scripts/update.lua` | `StaticUpdate` | 静态 tick、暂停态静态组件和静态 SG 事件入口 |
| `scripts/update.lua` | `PostUpdate` | emitter 和 `UpdateLooper_PostUpdate` 阶段 |
| `scripts/scheduler.lua` | `RunScheduler` | 普通 scheduler 的 `OnTick` 与 `Run` |
| `scripts/scheduler.lua` | `RunStaticScheduler` | 静态 scheduler 的 `OnTick` 与 `Run` |
| `scripts/stategraph.lua` | `StateGraphWrangler:Update` | 状态机 tick 更新和事件处理 |
| `scripts/brain.lua` | `BrainWrangler:Update` | Brain tick 更新和 sleep tick 管理 |
| `scripts/mainfunctions.lua` | `GetTickTime` | Lua tick 时间换算来源 |

### `0x21022111` 主锚点 / `scripts/update.lua` / 搜索信号

先搜索 `function Update`。
再在同一个文件里确认 `RunScheduler`、组件 `OnUpdate`、`SGManager:Update` 和 `BrainManager:Update` 的顺序。

## `0x21023000` 运行流程

~~~mermaid
flowchart TD
    A["engine tick"] --> B["update.lua: Update(dt)"]
    B --> C["RunScheduler for each unseen tick"]
    C --> D["StaticComponentUpdates"]
    D --> E["component OnUpdate(dt)"]
    E --> F["SGManager:Update(i)"]
    F --> G["BrainManager:Update(i)"]
    H["engine static tick"] --> I["StaticUpdate(dt)"]
    I --> J["RunStaticScheduler for each unseen static tick"]
    J --> K["TickRPCQueue"]
    K --> L["paused static component OnStaticUpdate"]
    L --> M["SGManager:UpdateEvents when paused"]
    N["engine post update"] --> O["PostUpdate(dt)"]
    O --> P["EmitterManager and UpdateLooper_PostUpdate"]
~~~

### `0x21023111` 流程分段 / `Update(dt)` 普通 Tick / 边界条件

`Update(dt)` 首先断言服务器没有暂停。
它用 `TheSim:GetTick()` 和 `last_tick_seen` 补跑尚未处理的 tick，并逐个调用 `RunScheduler(i)`。
组件 `OnUpdate(dt)` 在 scheduler 之后、StateGraph 和 Brain 之前执行。

### `0x21023121` 流程分段 / `SGManager` 与 `BrainManager` / 边界条件

`SGManager:Update(i)` 和 `BrainManager:Update(i)` 都在 `update.lua` 的 tick 循环尾部调用。
`RunScheduler()` 不会调用它们。
读 StateGraph 或 Brain 时，应把 `update.lua` 当成主循环来源，把各自 manager 当成子系统入口。

### `0x21023131` 流程分段 / `StaticUpdate(dt)` 静态 Tick / 边界条件

`StaticUpdate(dt)` 使用 `TheSim:GetStaticTick()` 和 `last_static_tick_seen` 补跑 `RunStaticScheduler(i)`。
只有 `TheNet:IsServerPaused()` 为真时，它才遍历静态组件并调用 `OnStaticUpdate(0)`。
暂停态下还会逐 tick 调用 `SGManager:UpdateEvents()`，但不调用 `SGManager:Update()`。

## `0x21024111` 结构细节 / 数据结构与生命周期 / Tick 去重 / 需要核对的字段

`last_tick_seen` 和 `last_static_tick_seen` 防止同一 tick 被重复处理。
如果引擎一次推进多个 tick，Lua 用 `for i = last_tick_seen + 1, tick do` 补跑 scheduler、SG 和 Brain。

## `0x21024121` 结构细节 / 数据结构与生命周期 / 更新集合 / 需要核对的字段

组件更新来自 `UpdatingEnts`、`NewUpdatingEnts` 和 `StopUpdatingComponents`。
StateGraph 使用 `SGManager` 内部的 `updaters`、`tickwaiters`、`haveEvents`。
Brain 使用 `BrainManager` 内部的 `updaters`、`tickwaiters` 和 `_safe_updaters`。

## `0x21025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n \
  -e "function Update" \
  -e "function StaticUpdate" \
  -e "RunScheduler" \
  -e "RunStaticScheduler" \
  -e "OnUpdate\\(dt\\)" \
  -e "SGManager:Update" \
  -e "BrainManager:Update" \
  -e "GetTickTime" \
  scripts/update.lua \
  scripts/scheduler.lua \
  scripts/stategraph.lua \
  scripts/brain.lua \
  scripts/mainfunctions.lua
~~~

### `0x21025111` 推荐顺序 / 最小闭环

从 `update.lua` 的 `Update(dt)` 开始画顺序。
然后分别跳到 `scheduler.lua`、`stategraph.lua`、`brain.lua` 确认每个 manager 内部如何处理 sleep、wake 和 updater 列表。
