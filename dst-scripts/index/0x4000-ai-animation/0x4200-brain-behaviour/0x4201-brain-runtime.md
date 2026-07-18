# `0x42010000` Brain 运行时

Brain 运行时页说明实体如何绑定 Brain，Brain 如何构造 behaviour tree，以及 `BrainManager` 如何按 sleep time 调度更新。

本页把 Brain 与 StateGraph 分开看。
Brain 负责决策和唤醒节奏。
StateGraph 负责动作、动画和事件状态。

## `0x42011100` 本页定位 / 要回答的运行时问题

Brain 的最小闭环是 `EntityScript:SetBrain`、`Brain:_Start_Internal`、`BrainManager:Update` 和 `BT:Update`。
读这一页时先确认 Brain 何时被创建，再确认它何时被暂停、休眠或重新唤醒。

### `0x42011110` Brain 与 SG 的分工

`SetBrain` 不会创建 StateGraph。
`SetStateGraph` 也不会创建 Brain。
Prefab 通常在 master sim 侧分别调用二者，让同一个实体同时拥有决策器和状态机。

#### `0x42011111` 验证点

- `scripts/entityscript.lua` 中 `SetBrain` 保存 `brainfn`，并在可用时立即实例化 `self.brain`。
- `scripts/entityscript.lua` 中 `SetStateGraph` 通过 `LoadStateGraph` 创建 `StateGraphInstance`。
- `scripts/brain.lua` 中 `Brain:OnUpdate` 只调用 `DoUpdate` 和 `bt:Update`。
- 具体动作是否改变世界状态，要继续追 `behaviours/*.lua`、`locomotor`、`combat` 或 SG 的 action handler。

## `0x42012000` 源码锚点

| 文件 | 入口 | 需要核对的事实 |
| --- | --- | --- |
| `scripts/entityscript.lua` | `EntityScript:SetBrain` | 保存 `brainfn`，按睡眠、limbo、停止标记决定是否创建 Brain。 |
| `scripts/entityscript.lua` | `EntityScript:RestartBrain` | 清理 `_brainstopped` 原因后才调用 `brain:_Start_Internal()`。 |
| `scripts/entityscript.lua` | `_DisableBrain_Internal` | 实体睡眠或离场时停止并丢弃当前 Brain 实例。 |
| `scripts/entityscript.lua` | `_EnableBrain_Internal` | 实体醒来或回场时用 `brainfn()` 重新创建 Brain。 |
| `scripts/brain.lua` | `Brain:_Start_Internal` | 调用 `OnStart` 构造 `BT`，再注册到 `BrainManager`。 |
| `scripts/brain.lua` | `BrainWrangler:Update` | 更新 `updaters`，并按 `GetSleepTime()` 放入 `tickwaiters` 或 `hibernaters`。 |
| `scripts/brain.lua` | `Brain:ForceUpdate` | 强制 `BT` 立即更新，并把 Brain 放回 `updaters`。 |
| `scripts/brains/wilsonbrain.lua` | `WilsonBrain:OnStart` | 最小 Brain 样例，root 是 `PriorityNode`。 |

### `0x42012110` 主锚点 / `scripts/entityscript.lua`

`SetBrain` 是绑定入口。
它先记录 `self.brainfn = brainfn`。
如果实体处于 `sleepstatepending`、`IsInLimbo()` 或 `IsAsleep()`，它只记录函数并设置 `_braindisabled`。
如果允许启动，它调用 `brainfn()`，设置 `self.brain.inst = self`，再进入 `brain:_Start_Internal()`。

#### `0x42012111` 搜索信号

~~~bash
rg -n "SetBrain|RestartBrain|StopBrain|_DisableBrain_Internal|_EnableBrain_Internal|SetStateGraph" \
  scripts/entityscript.lua
~~~

## `0x42013000` 运行流程

~~~mermaid
flowchart TD
    A["Prefab master sim"]
    A --> B["EntityScript:SetBrain(brainfn)"]
    B --> C{"实体可运行 Brain?"}
    C -- "否" --> D["保存 brainfn 与 _braindisabled"]
    C -- "是" --> E["brainfn() 创建 Brain"]
    E --> F["brain.inst = entity"]
    F --> G["Brain:_Start_Internal"]
    G --> H["Brain:OnStart 构造 BT root"]
    H --> I["BrainManager:AddInstance"]
    I --> J["BrainWrangler:Update"]
    J --> K["Brain:OnUpdate"]
    K --> L["BT:Update"]
    L --> M{"BT:GetSleepTime()"}
    M -- "nil" --> N["BrainManager:Hibernate"]
    M -- "> GetTickTime()" --> O["BrainManager:Sleep 到 tickwaiters"]
    M -- "0 或短间隔" --> P["保留在 updaters"]
~~~

### `0x42013110` 启动阶段 / `SetBrain` 到 `_Start_Internal`

`SetBrain` 会先停止已有 Brain。
这意味着重新绑定 Brain 不是叠加行为树，而是替换当前 Brain 实例。
`_Start_Internal` 会跳过已经启动的 Brain，然后执行 `OnStart`。

#### `0x42013111` 边界条件

- `_brainstopped` 存在时，`SetBrain` 会创建 Brain，但不会启动它。
- `_braindisabled` 存在时，`SetBrain` 不创建 Brain，直到 `_EnableBrain_Internal` 重新调用 `brainfn()`。
- `Brain:Start` 是兼容入口，源码注释要求改用 `EntityScript:RestartBrain`。

### `0x42013210` 更新阶段 / `BrainWrangler:Update`

`BrainManager` 是 `BrainWrangler()` 的全局实例。
它维护 `instances`、`updaters`、`tickwaiters` 和 `hibernaters`。
每个 tick 会先把到期的 `tickwaiters[current_tick]` 放回 `updaters`，再复制一份安全更新列表。

#### `0x42013211` 调度验证点

- 只有 `k.inst.entity:IsValid()` 且 `not k.inst:IsAsleep()` 的 Brain 会执行 `k:OnUpdate()`。
- `sleep_amount == nil` 时进入 `Hibernate`。
- `sleep_amount > GetTickTime()` 时进入 `Sleep`。
- `sleep_amount <= GetTickTime()` 时不移动列表，下一轮仍在 `updaters`。

### `0x42013310` 停止与唤醒阶段 / `_Stop_Internal`、`Pause`、`Resume`

`_Stop_Internal` 会调用 `OnStop`，停止 `BT`，并从 `BrainManager` 移除实例。
`Pause` 不改变 `stopped`，但会把运行中的 Brain 从调度器移除。
`Resume` 会在未停止时重新 `AddInstance`。

#### `0x42013311` 事件唤醒点

- `Brain:ForceUpdate` 会调用 `bt:ForceUpdate()` 并 `BrainManager:Wake(self)`。
- `EventNode:OnEvent` 会在实体仍有 Brain 时调用 `self.inst.brain:ForceUpdate()`。
- 实体从睡眠或 limbo 恢复时走 `_EnableBrain_Internal`，不是直接恢复已有 Brain 对象。

## `0x42014110` 结构细节 / Brain 对象字段 / `Brain = Class(...)`

Brain 初始化字段包括 `inst`、`currentbehaviour`、`behaviourqueue`、`events`、`thinkperiod`、`paused` 和 `stopped`。
更新路径主要依赖 `bt`，而不是 `behaviourqueue`。

### `0x42014111` 字段核对

- `self.stopped` 初始为 `true`。
- `self.paused` 初始为 `false`。
- `self.bt` 由具体 Brain 的 `OnStart` 赋值。

## `0x42014210` 结构细节 / Behaviour Tree 接口 / `Brain:OnUpdate`

`Brain:OnUpdate` 先调用可选的 `DoUpdate`。
之后如果存在 `self.bt`，就调用 `self.bt:Update()`。
Brain 本身不解释 `PriorityNode` 或 `DoAction` 的语义，这些在 `behaviourtree.lua` 和 `behaviours/*.lua` 内完成。

### `0x42014211` 不要混淆的边界

- Brain 不等于 behaviour tree。
- Brain 拥有 `BT` 实例。
- `BT` 拥有 root node。
- 具体 leaf node 可以返回状态，也可以直接调用组件方法。

## `0x42015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "SetBrain|RestartBrain|_Start_Internal|BrainWrangler:Update|Brain:OnUpdate|ForceUpdate" \
  scripts/entityscript.lua \
  scripts/brain.lua \
  scripts/behaviourtree.lua \
  scripts/brains/wilsonbrain.lua
~~~

### `0x42015110` 推荐顺序

先读 `EntityScript:SetBrain`，确认 Brain 是否被创建。
再读 `Brain:_Start_Internal`，确认 `OnStart` 和 `BrainManager:AddInstance` 的顺序。
最后读 `BrainWrangler:Update`，确认 sleep、hibernate 和 force update 的差异。

#### `0x42015111` 最小闭环

用 `scripts/brains/wilsonbrain.lua` 验证最小 Brain。
它在 `OnStart` 中创建 `PriorityNode`，并把 `BT(self.inst, root)` 写入 `self.bt`。
这条链路不需要先理解复杂 Boss AI。
