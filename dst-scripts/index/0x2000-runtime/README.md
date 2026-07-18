# `0x20000000` 运行时

本区按 Lua 运行时的真实阅读顺序组织。
先读 `main.lua` 顶层启动，再读 `Start()` 加载 `gamelogic.lua`，最后进入 `update.lua` 与 `scheduler.lua`。

目录级语义由本 README 承载，独立专题文件只解释具体运行链路。

## `0x20001111` 区域定位 / 阅读问题 / 运行时边界 / 验证点

本区解释 Lua 侧能看到的入口、回调和调度表。
不要把引擎回调写成 Lua 文件之间的直接调用。

## `0x20002000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/main.lua` | `ModSafeStartup` | 加载 mod、注册资源、建立全局实体 |
| `scripts/mainfunctions.lua` | `Start` | 创建 `TheFrontEnd` 并 `require("gamelogic")` |
| `scripts/mainfunctions.lua` | `GlobalInit` | 加载 `global` 与全局事件 prefab |
| `scripts/gamelogic.lua` | `DoResetAction` | 按 `Settings.reset_action` 进入前端、读档、生成或联网 |
| `scripts/gamelogic.lua` | `DoInitGame` | 用 `savedata` 填充世界并进入加载完成状态 |
| `scripts/update.lua` | `Update` | 非暂停游戏 tick 的 Lua 更新入口 |
| `scripts/update.lua` | `StaticUpdate` | 静态 tick、暂停态静态组件和静态调度入口 |
| `scripts/scheduler.lua` | `RunScheduler` | 普通 coroutine 与定时回调调度入口 |
| `scripts/scheduler.lua` | `RunStaticScheduler` | 静态 scheduler 调度入口 |

### `0x20002111` 入口选择 / `scripts/main.lua` / 搜索信号

用 `ModSafeStartup` 读顶层启动副作用。
用 `Start` 读引擎进入前端和 `gamelogic.lua` 的阶段。
用 `Update` 读每 tick 的实际 Lua 更新顺序。

## `0x20003000` 运行关系图

~~~mermaid
flowchart TD
    A["engine loads main.lua"]
    A --> B["require core modules"]
    B --> C["ModSafeStartup"]
    C --> D["optional GlobalInit"]
    A --> E["engine calls Start"]
    E --> F["require gamelogic"]
    F --> G["Profile and index Load callbacks"]
    G --> H["DoResetAction"]
    H --> I["LoadSlot or frontend or client join"]
    I --> J["DoInitGame when world data is ready"]
    A --> K["engine tick callbacks"]
    K --> L["update.lua: Update or StaticUpdate"]
    L --> M["scheduler, components, SGManager, BrainManager"]
~~~

### `0x20003111` 导航原则 / 两条主线 / 边界条件

`GlobalInit` 由 `ModSafeStartup` 在 `RUN_GLOBAL_INIT` 下调用。
`Start` 不调用 `GlobalInit`，它创建前端并加载 `gamelogic.lua`。
`RunScheduler` 不调用 `SGManager:Update` 或 `BrainManager:Update`，它们同在 `Update` 中由 tick 循环分段调用。

## `0x20004111` 目录索引 / README 载体 / 二级目录 / 链接校验

以下入口先进入目录 README，再进入具体专题文件。

- [启动循环](0x2100-boot-loop/README.md)
- [启动序列](0x2100-boot-loop/0x2101-boot-sequence.md)
- [主循环](0x2100-boot-loop/0x2102-main-loop.md)
- [调度器](0x2100-boot-loop/0x2103-scheduler.md)
- [引擎服务](0x2200-engine-services/README.md)
- [引擎全局](0x2200-engine-services/0x2201-engine-globals.md)
- [存档与读档](0x2200-engine-services/0x2202-save-load.md)
- [网络运行时](0x2200-engine-services/0x2203-network-runtime.md)
- [工具与调试](0x2300-tooling/README.md)
- [模组与调试](0x2300-tooling/0x2301-mods-debug.md)
- [平台工具](0x2300-tooling/0x2302-platform-tools.md)

## `0x20005100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "ModSafeStartup|function Start|function GlobalInit|local function DoResetAction|function Update|RunScheduler" \
  scripts/main.lua \
  scripts/mainfunctions.lua \
  scripts/gamelogic.lua \
  scripts/update.lua \
  scripts/scheduler.lua
~~~

### `0x20005111` 最小闭环 / 抽样动作

先确认 `main.lua` 顶层 require 与 `ModSafeStartup` 的副作用。
再确认 `Start()` 加载 `gamelogic.lua` 后，底部 Profile 和索引回调如何进入 `DoResetAction()`。
最后用 `update.lua` 验证 tick 内 scheduler、组件、StateGraph 和 Brain 的相对顺序。
