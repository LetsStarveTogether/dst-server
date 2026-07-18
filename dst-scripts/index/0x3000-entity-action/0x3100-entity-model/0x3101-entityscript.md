# `0x31010000` EntityScript

`EntityScript` 是 C++ 实体对象的 Lua 包装层。
它把组件表、事件表、world state watcher、StateGraph、Brain、任务和存档入口放在同一个运行时对象上。

## `0x31011111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

目标是先确认实体从哪里来，再确认 `EntityScript` 自己只做调度、登记和生命周期桥接。
具体 gameplay 规则通常落在组件、Prefab 初始化函数、StateGraph 或 Brain 中。

## `0x31012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `CreateEntity` | `TheSim:CreateEntity` 后创建 `EntityScript` |
| `scripts/entityscript.lua` | `EntityScript = Class` | 初始化组件、事件、任务和 replica 容器 |
| `scripts/entityscript.lua` | `SetStateGraph` | 装载 `stategraphs/<name>` 并创建 `StateGraphInstance` |
| `scripts/entityscript.lua` | `SetBrain` | 保存 `brainfn` 并按睡眠状态决定是否启动 |
| `scripts/entityscript.lua` | `GetPersistData` | 汇总组件与实体自身的保存数据 |

### `0x31012111` 主锚点 / `scripts/entityscript.lua` / 搜索信号

先在 `mainfunctions.lua` 搜索 `CreateEntity`。
再跳到 `entityscript.lua` 的构造函数，确认字段是不是由源码实际初始化。

## `0x31013000` 运行流程

~~~mermaid
flowchart TD
    A["TheSim:CreateEntity"]
    A --> B["EntityScript(ent)"]
    B --> C["Ents[GUID] = scr"]
    B --> D["components / replica"]
    B --> E["event_listeners / event_listening"]
    B --> F["worldstatewatching / pendingtasks"]
    B --> G["SetStateGraph"]
    B --> H["SetBrain"]
    D --> I["GetPersistData / SetPersistData"]
~~~

### `0x31013111` 流程分段 / 入口到副作用 / 边界条件

- `CreateEntity` 只创建 Lua 包装对象并登记到 `Ents`。
- `SetStateGraph` 会把已有 SG 从 `SGManager` 移除，再创建目标 `StateGraphInstance`。
- `SetBrain` 会保存 `brainfn`，并受 `sleepstatepending`、`IsInLimbo` 和 `IsAsleep` 影响。
- 如果链路跨 server/client，先读 server 侧权威路径，再回看 replica 或 classified。

## `0x31014111` 结构细节 / 数据结构与生命周期 / 具体 Lua 结构 / 需要核对的字段

- `components` 保存 server 组件实例。
- `replica._` 保存 replica 组件，外层 `replica` 通过 metatable 做访问校验。
- `event_listeners` 记录谁在监听当前实体。
- `event_listening` 记录当前实体正在监听谁。
- `worldstatewatching` 记录 world state watcher，真正注册点在 `TheWorld.components.worldstate`。
- `pendingtasks` 与实体 GUID 相关，`Remove` 时会通过 `CancelAllPendingTasks` 清理。

## `0x31014121` 结构细节 / 数据结构与生命周期 / SG 与 Brain 绑定 / 启停边界

- `SetStateGraph` 会调用 `LoadStateGraph`，再创建 `StateGraphInstance(sg, self)`。
- `SGManager:AddInstance(self.sg, self:IsAsleep())` 决定目标 SG 是否休眠登记。
- `SetBrain` 保存的是函数 `brainfn`，不是固定 Brain 实例。
- 睡眠、limbo 或 `StopBrain` 原因会阻止 Brain 立即启动。

## `0x31014131` 结构细节 / 数据结构与生命周期 / 存档入口 / 保存与恢复顺序

- `GetSaveRecord` 写位置、皮肤信息，并调用 `GetPersistData`。
- `GetPersistData` 遍历组件 `OnSave`，过滤空表，收集 references。
- `SetPersistData` 先处理 `add_component_if_missing`，再执行实体 `OnPreLoad`。
- 组件 `OnLoad` 之后才调用实体自身 `OnLoad`。
- `LoadPostPass` 是独立后处理阶段，不在 `SetPersistData` 主循环里执行。

## `0x31015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "function CreateEntity|EntityScript = Class|SetStateGraph|SetBrain|GetPersistData|SetPersistData" \
  scripts/entityscript.lua \
  scripts/mainfunctions.lua
~~~

### `0x31015111` 推荐顺序 / 最小闭环

从 `CreateEntity` 开始追一个普通 Prefab。
再追一个有 SG 和 Brain 的生物 Prefab。
最后追一次 `GetSaveRecord`，确认组件保存数据如何汇总。
