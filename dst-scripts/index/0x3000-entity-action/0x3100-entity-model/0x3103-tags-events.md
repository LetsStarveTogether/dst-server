# `0x31030000` 标签与事件

标签负责快速状态判断。
事件负责把一个实体上的状态变化同步通知监听者、StateGraph 和 Brain。
world state watcher 是同一页需要并读的第三条通知路径。

## `0x31031111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

目标是确认三件事。
标签只是转发到底层 `entity`。
事件监听同时写入 source 和 listener 两边的索引表。
world state watcher 最终注册在 `TheWorld.components.worldstate`。

## `0x31032000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/entityscript.lua` | `AddTag` | 标签入口 |
| `scripts/entityscript.lua` | `ListenForEvent` | 事件订阅 |
| `scripts/entityscript.lua` | `PushEvent_Internal` | 事件分发 |
| `scripts/entityscript.lua` | `WatchWorldState` | world state 订阅 |
| `scripts/stategraph.lua` | `HandleEvent` | SG 事件响应 |
| `scripts/brain.lua` | `Brain:PushEvent` | Brain 事件响应 |
| `scripts/componentutil.lua` | tag 常量与 `PushEvent` 用法 | gameplay 工具层验证样本 |

### `0x31032111` 主锚点 / `scripts/entityscript.lua` / 搜索信号

先在 `entityscript.lua` 搜索 `AddTag` 和 `ListenForEvent`。
再搜索 `PushEvent_Internal`，确认事件真正分发给哪些对象。
需要验证 tag 在玩法工具层如何使用时，读 `componentutil.lua` 的 tag 常量和 `FindEntities` 调用。

## `0x31033000` 运行流程

~~~mermaid
flowchart TD
    A["AddTag / RemoveTag"]
    A --> B["entity:AddTag / entity:RemoveTag"]
    C["ListenForEvent(event, fn, source)"]
    C --> D["source.event_listeners"]
    C --> E["listener.event_listening"]
    F["PushEvent_Internal"]
    F --> G["callbacks fn(self, data)"]
    F --> H["SG immediate or buffered event"]
    F --> I["Brain:PushEvent"]
    J["WatchWorldState"]
    J --> K["TheWorld.components.worldstate"]
~~~

### `0x31033111` 流程分段 / 入口到副作用 / 边界条件

- `ListenForEvent` 的 `source` 默认是 `self`。
- `source.event_listeners[event][listener]` 用于事件触发时找到回调。
- `listener.event_listening[event][source]` 用于移除监听时反向清理。
- `PushEvent_Internal` 先同步调用监听器回调。
- SG 处理取决于 `PushEvent` 还是 `PushEventImmediate`。
- Brain 始终在 SG 分支之后收到 `Brain:PushEvent(event, data)`。

## `0x31034111` 结构细节 / 数据结构与生命周期 / 具体 Lua 结构 / 需要核对的字段

- `AddTag`、`RemoveTag`、`HasTag`、`HasTags` 都转发到底层 `self.entity`。
- `HasTags` 等价于 `HasAllTags`。
- `HasOneOfTags` 等价于 `HasAnyTag`。
- 标签常用于动作收集、目标过滤、碰撞或 SG 条件判断。

## `0x31034121` 结构细节 / 数据结构与生命周期 / 事件分发 / SG 与 Brain 顺序

- `PushEvent` 调 `PushEvent_Internal(event, data, false)`。
- `PushEventImmediate` 调 `PushEvent_Internal(event, data, true)`。
- 非 immediate 事件只有在 SG 正在监听且 `SGManager:OnPushEvent` 接受时才进入 SG 缓冲队列。
- immediate 事件直接调用 `self.sg:HandleEvent(event, data)`。
- `Brain:PushEvent` 只查 `self.events[event]` 并执行 handler。

## `0x31034131` 结构细节 / 数据结构与生命周期 / World State Watcher / 组件注入路径

- `EntityScript:WatchWorldState` 会记录 `worldstatewatching`，再调用 `TheWorld.components.worldstate:AddWatcher`。
- `StopWatchingWorldState` 对应调用 `RemoveWatcher`。
- `LoadComponent` 会把 `WatchWorldState` 和 `StopWatchingWorldState` 注入到组件类。
- 组件调用 `self:WatchWorldState` 时，记录仍挂在 `self.inst.worldstatewatching` 上。
- 实体 `Remove` 会调用 `StopAllWatchingWorldStates` 清理所有 world state watcher。

## `0x31034141` 结构细节 / 数据结构与生命周期 / 玩法工具层验证 / `componentutil.lua`

- `componentutil.lua` 中的 `NON_LIFEFORM_TARGET_TAGS`、`SOULLESS_TARGET_TAGS` 等是 tag 过滤约定。
- `TheSim:FindEntities` 的 include/exclude tag 参数会消费这些 tag 列表。
- `componentutil.lua` 也会直接调用 `inst:HasTag`、`ent:PushEvent` 和 `PushEventImmediate`。
- 这说明 tag/event 是跨组件、Prefab、工具函数共享的运行时约定，不属于单一组件私有状态。

## `0x31035100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "AddTag|HasTag|ListenForEvent|RemoveEventCallback|PushEvent_Internal" \
  scripts/entityscript.lua \
  scripts/stategraph.lua \
  scripts/brain.lua \
  scripts/componentutil.lua

rg -n "PushEventImmediate|WatchWorldState|StopAllWatchingWorldStates" \
  scripts/entityscript.lua \
  scripts/componentutil.lua
~~~

### `0x31035111` 推荐顺序 / 最小闭环

先追一个 `ListenForEvent`，确认 source 与 listener 两张表都被写入。
再追一个普通 `PushEvent`，确认 SG 缓冲和 Brain 直接 handler 的差异。
最后追一个组件 `self:WatchWorldState`，确认清理点在实体移除流程里。
