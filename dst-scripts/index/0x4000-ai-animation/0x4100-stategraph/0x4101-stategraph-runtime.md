# `0x41010000` StateGraph 运行时

StateGraph 运行时页解释 `StateGraph` 定义如何变成实体上的 `StateGraphInstance`。
重点是动作、事件、timeline 和 timeout 的实际调度顺序。

## `0x41011111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

目标是从 `StateGraphInstance:StartAction` 读懂动作为什么经常先进入动画状态。
再从 `StateGraphInstance:UpdateState` 读懂真正副作用为什么常落在 timeline 或 timeout。

## `0x41011121` 本页定位 / 要回答的运行时问题 / 调度边界 / 验证点

`SGManager` 维护 `updaters`、`tickwaiters`、`hibernaters` 和 `haveEvents`。
`StateGraphWrangler:Update` 先更新可运行实例，再调用 `UpdateEvents` 处理已缓冲事件。

## `0x41012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/stategraph.lua` | `StateGraphWrangler:Update` | 驱动 SG 实例更新和事件处理 |
| `scripts/stategraph.lua` | `ActionHandler` | 把 `ACTIONS.*` 映射到目标状态或函数 |
| `scripts/stategraph.lua` | `EventHandler` | 定义全局或状态局部事件响应 |
| `scripts/stategraph.lua` | `State` | 收集 tags、events、timeline、timeout 回调 |
| `scripts/stategraph.lua` | `StateGraph` | 把 states、events、actionhandlers 重建为索引表 |
| `scripts/stategraph.lua` | `StateGraphInstance` | 绑定实体、当前状态、statemem 和 mem |
| `scripts/stategraph.lua` | `SoundFrameEvent` | 按帧播放声音并可传入音量 |

### `0x41012111` 主锚点 / `scripts/stategraph.lua` / 搜索信号

先搜 `StateGraphWrangler:Update`、`StateGraphInstance:StartAction` 和 `StateGraphInstance:GoToState`。
这三个锚点分别对应调度、动作入口和状态切换。

### `0x41012121` 主锚点 / `State` 构造器 / 搜索信号

搜 `State = Class` 可看到 `args.events` 被转换成 `self.events`。
搜 `table.sort(self.timeline, Chronological)` 可确认 timeline 按时间排序。

## `0x41013000` 运行流程

~~~mermaid
flowchart TD
    A["StateGraph(name, states, events, defaultstate, actionhandlers)"]
    A --> B["StateGraphInstance(stategraph, inst)"]
    B --> C["StartAction(bufferedaction)"]
    C --> D["ActionHandler.deststate"]
    D --> E["GoToState(state)"]
    E --> F["State.onenter(params)"]
    F --> G["SetTimeout / timelineindex"]
    G --> H["UpdateState(dt)"]
    H --> I["ontimeout 或 TimeEvent.fn"]
    I --> J["PerformBufferedAction / GoToState"]
    B --> K["PushEvent(event, data)"]
    K --> L["HandleEvents"]
    L --> M["State:HandleEvent 后退到 sg.events"]
~~~

### `0x41013111` 动作入口 / `StateGraphInstance:StartAction` / 边界条件

`StartAction` 只在找到 `handler` 且 `condition` 通过时继续。
如果 `deststate` 返回状态名，就设置 `statemem.is_going_to_action_state` 并调用 `GoToState`。
如果没有 `deststate`，它直接调用 `inst:PerformBufferedAction()`。

### `0x41013211` 事件入口 / `StateGraphInstance:HandleEvent` / 边界条件

`HandleEvent` 先调用当前状态的 `State:HandleEvent`。
当前状态 handler 只有返回 truthy 时才阻止回退。
如果状态 handler 不存在或返回 nil，流程会继续回退到 `self.sg.events[eventname]`。
`PushEvent` 会把当前状态名写入 `data.state`，避免过期状态事件影响目标状态。

### `0x41013311` 时间入口 / `StateGraphInstance:UpdateState` / 边界条件

`timeout` 先扣减，触发 `ontimeout` 后如果状态已变更就停止当前轮。
timeline 之后按 `timelineindex` 顺序执行。
如果 timeline 回调切换状态，源码会用 `extra_time` 递归补跑新状态。

## `0x41014111` 结构细节 / 定义与实例 / `StateGraph` / 需要核对的字段

`StateGraph` 是静态定义。
构造时把 actionhandlers 按 `v.action` 重建索引，把 events 按小写事件名重建索引，把 states 按 `v.name` 重建索引。
它还会应用 `ModManager:GetPostInitData` 和 `StategraphPostInit`。

## `0x41014121` 结构细节 / 定义与实例 / `StateGraphInstance` / 需要核对的字段

`StateGraphInstance` 保存 `currentstate`、`timeinstate`、`timelineindex`、`bufferedevents`、`tags`、`statemem` 和 `mem`。
`statemem` 在每次 `GoToState` 时清空。
`mem` 跨状态保留，适合放长期状态机记忆。

## `0x41014211` 结构细节 / 标签同步 / `SGTagsToEntTags` / 需要核对的字段

`GoToState` 会把状态 tags 复制到 `self.tags`。
服务端或无 Network 实体会把 `busy`、`moving`、`attack` 等 SG tag 同步为实体 tag。

## `0x41014311` 结构细节 / Timeline 声音 Helper / `SoundFrameEvent` / 验证点

`SoundFrameEvent(frame, sound_event, vol)` 是 `TimeEvent(frame * FRAMES, ...)` 的薄包装。
`vol` 参数会传给 `SoundEmitter:PlaySound(sound_event, nil, vol)`。
`SGchester.lua` 已用这个路径给跳船声音设置了较低音量。

## `0x41015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "StateGraphWrangler:Update|StateGraphInstance:StartAction|StateGraphInstance:GoToState" \
  scripts/stategraph.lua

rg -n "StateGraphInstance:UpdateState|State:HandleEvent" \
  scripts/stategraph.lua

rg -n "SoundFrameEvent|PlaySound\\(sound_event" \
  scripts/stategraph.lua \
  scripts/stategraphs/SGchester.lua
~~~

### `0x41015111` 推荐顺序 / 最小闭环

先读 `ActionHandler` 如何保存 `deststate`。
再读 `StartAction` 如何调用 `GoToState`。
最后读 `UpdateState` 如何触发 `TimeEvent` 或 `ontimeout`。

### `0x41015112` 推荐顺序 / 抽样验证

用 `scripts/stategraphs/SGrabbit.lua` 的 `eat` 状态验证 timeout 路径。
用 `hop` 状态验证 timeline 与 onupdate 同时存在时，`Update` 必须保持每帧运行。
