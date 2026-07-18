# `0x40000000` AI 与动画

本区说明实体如何把意图交给 StateGraph，把长期选择交给 Brain，再由 behaviour tree 产出下一步动作。

目录级语义由本 README 承载，独立专题文件只解释具体运行链路。

## `0x40001111` 区域定位 / 先分清两条运行链 / `StateGraph` 负责短时表现 / 验证点

在 `scripts/stategraph.lua` 中确认 `StateGraphInstance:StartAction`、`GoToState` 和 `UpdateState`。
它们处理动作入口、状态切换、timeline、timeout 和 per-state event。

## `0x40001121` 区域定位 / 先分清两条运行链 / `Brain` 负责长期选择 / 验证点

在 `scripts/brain.lua` 和 `scripts/behaviourtree.lua` 中确认 Brain 调度 behaviour node。
Brain 不直接播放动画，而是推动实体产生可被 StateGraph 消化的意图。

## `0x40002000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/stategraph.lua` | `SGManager` | 管理 SG 实例更新、休眠和事件队列 |
| `scripts/stategraph.lua` | `StateGraphInstance` | 绑定实体的状态机实例 |
| `scripts/stategraphs/commonstates.lua` | `CommonStates.Add*` | 向 `states` 表追加通用状态 |
| `scripts/stategraphs/SGwilson.lua` | `StateGraph("wilson")` | 玩家服务端 StateGraph |
| `scripts/stategraphs/SGwilson_client.lua` | `StateGraph("wilson_client")` | 玩家客户端预测 StateGraph |
| `scripts/brain.lua` | `BrainManager` | Brain 更新入口 |
| `scripts/behaviourtree.lua` | `PriorityNode` | 行为树选择节点 |

### `0x40002111` 入口选择 / `scripts/stategraph.lua` / 搜索信号

先搜 `StateGraphInstance:StartAction` 和 `StateGraphInstance:UpdateState`。
这两个函数能把动作入口和动画帧副作用连起来。

### `0x40002121` 入口选择 / `scripts/brain.lua` / 搜索信号

再搜 `BrainManager`、`BT` 和 `OnUpdate`。
这一步用于确认 AI 决策如何在 StateGraph 之外运行。

## `0x40003000` 运行关系图

~~~mermaid
flowchart TD
    A["Prefab 设置 sgname 与 brainname"]
    A --> B["StateGraphInstance"]
    A --> C["Brain"]
    C --> D["BehaviourNode 选择意图"]
    D --> E["BufferedAction / PushEvent / direct component call"]
    E --> B
    E --> I["combat / locomotor / homeseeker 等组件"]
    B --> F["State.onenter"]
    F --> G["timeline / timeout / events"]
    G --> H["PerformBufferedAction 或 GoToState"]
~~~

### `0x40003111` 导航原则 / 先读表现层，再读决策层 / 边界条件

`StateGraph` 可以没有 Brain，例如许多物件或简单生物。
有 Brain 的实体常把 `BufferedAction` 交给 SG。
部分 behaviour leaf 也会直接驱动 `combat`、`locomotor`、`homeseeker` 等组件。

### `0x40003121` 导航原则 / 先读服务端，再读客户端预测 / 边界条件

`SGwilson.lua` 是权威状态。
`SGwilson_client.lua` 用 `server_states` 和 `forward_server_states` 描述哪些客户端状态可匹配服务端状态。

## `0x40004111` 目录索引 / README 载体 / 二级目录 / 链接校验

以下入口先进入目录 README，再进入具体专题文件。

- [StateGraph](0x4100-stategraph/README.md)
- [StateGraph Runtime](0x4100-stategraph/0x4101-stategraph-runtime.md)
- [CommonStates](0x4100-stategraph/0x4102-commonstates.md)
- [SGWilson](0x4100-stategraph/0x4103-sgwilson.md)
- [Brain 与 Behaviour](0x4200-brain-behaviour/README.md)
- [Brain Runtime](0x4200-brain-behaviour/0x4201-brain-runtime.md)
- [Behaviour Tree](0x4200-brain-behaviour/0x4202-behaviour-tree.md)
- [追踪样例](0x4300-tracing/README.md)
- [AI Examples](0x4300-tracing/0x4301-ai-examples.md)

## `0x40005100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "StateGraphInstance:StartAction|StateGraphInstance:UpdateState|BrainManager|PriorityNode" \
  scripts/stategraph.lua \
  scripts/brain.lua \
  scripts/behaviourtree.lua
~~~

### `0x40005111` 最小闭环 / 抽样动作

从 `scripts/stategraphs/SGrabbit.lua` 的 `ActionHandler(ACTIONS.EAT, "eat")` 开始。
再读 `eat` 状态里的 `SetTimeout`、`ontimeout` 和 `PerformBufferedAction`。

### `0x40005112` 最小闭环 / 抽样 AI

从 rabbit prefab 追到 rabbit brain，再回到 `SGrabbit`。
这样能验证 Brain 选择意图，StateGraph 执行表现的边界。
