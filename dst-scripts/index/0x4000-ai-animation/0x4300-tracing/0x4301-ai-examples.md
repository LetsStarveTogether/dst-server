# `0x43010000` AI 追踪案例

AI 追踪案例页用兔子、Wilson 和 Deerclops 验证 Prefab、Brain、Behaviour、StateGraph 和 Component 的闭环。

本页不把 Brain 写成动画状态机。
Brain 选择或推进行为。
StateGraph 响应 action、event、locomote 和 attack 状态。

## `0x43011100` 本页定位 / 要回答的运行时问题

AI prefab 需要先满足 SG 和 Brain 依赖的组件，再按实体行为继续装配其他组件。
`SetStateGraph` 和 `SetBrain` 不要求一定排在所有组件之后。
Brain 节点可以通过 `DoAction` 交给 `locomotor`，也可以直接驱动 `combat` 或移动组件。
SG 负责把 `BufferedAction`、移动意图和事件映射成具体状态。

### `0x43011110` 三个样例

兔子展示普通小生物的逃跑、回家和吃食物。
Wilson 展示极简 Brain 如何用 `ChaseAndAttack` 包住玩家控制条件。
Deerclops 展示 Boss Brain 如何混合 `ActionNode`、`AttackWall`、`ChaseAndAttack`、`DoAction` 和 `Wander`。

#### `0x43011111` 验证点

- Prefab 中 `SetStateGraph` 和 `SetBrain` 是两个独立调用。
- `DoAction` 的 `BufferedAction` 最终要由 SG 中的 action state 执行。
- `ChaseAndAttack` 这类 behaviour 会直接调用 `combat` 和 `locomotor`。
- Boss AI 并不天然不同于普通 AI，只是 root node 更复杂。

## `0x43012000` 源码锚点

| 文件 | 入口 | 需要核对的事实 |
| --- | --- | --- |
| `scripts/prefabs/rabbit.lua` | `fn` | master sim 侧先建 `locomotor`，再 `SetStateGraph("SGrabbit")` 和 `SetBrain(brain)`。 |
| `scripts/brains/rabbitbrain.lua` | `RabbitBrain:OnStart` | root 是 `.25` period 的 `PriorityNode`。 |
| `scripts/stategraphs/SGrabbit.lua` | `actionhandlers` | `ACTIONS.EAT` 到 `eat`，`ACTIONS.GOHOME` 到 `action`。 |
| `scripts/stategraphs/SGrabbit.lua` | `State "action"` | 进入状态后调用 `inst:PerformBufferedAction()`。 |
| `scripts/brains/wilsonbrain.lua` | `WilsonBrain:OnStart` | `WhileNode` 检查 `CONTROL_PRIMARY`，节点体是 `ChaseAndAttack`。 |
| `scripts/prefabs/deerclops.lua` | `fn` | `SetStateGraph("SGdeerclops")` 早于 `SetBrain(brain)`。 |
| `scripts/brains/deerclopsbrain.lua` | `DeerclopsBrain:OnStart` | root 混合直接事件、攻击、拆建筑和巡游。 |
| `scripts/stategraphs/SGdeerclops.lua` | `ActionHandler(ACTIONS.HAMMER, "attack")` | `DoAction(BaseDestroy)` 产出的 hammer action 进入 attack 状态。 |

### `0x43012110` 主锚点 / `scripts/prefabs/rabbit.lua`

兔子 Prefab 在 `TheWorld.ismastersim` 之后才装配 server 侧组件。
源码注释写明 `locomotor` 必须在 stategraph 之前构造。
之后顺序是 `inst:SetStateGraph("SGrabbit")` 和 `inst:SetBrain(brain)`。

#### `0x43012111` 搜索信号

~~~bash
rg -n "locomotor|SetStateGraph|SetBrain|PushEvent\\(\"gohome\"\\)|PerformBufferedAction|ActionHandler" \
  scripts/prefabs/rabbit.lua \
  scripts/brains/rabbitbrain.lua \
  scripts/stategraphs/SGrabbit.lua
~~~

## `0x43013000` 运行流程

~~~mermaid
flowchart TD
    A["prefabs/rabbit.lua master sim fn"]
    A --> B["AddComponent(locomotor)"]
    B --> C["SetStateGraph(SGrabbit)"]
    C --> D["SetBrain(rabbitbrain)"]
    D --> E["RabbitBrain:OnStart"]
    E --> F["PriorityNode root"]
    F --> G{"选中 behaviour"}
    G -- "DoAction(EatFoodAction)" --> H["BufferedAction(ACTIONS.EAT)"]
    G -- "EventNode(gohome)" --> I["BufferedAction(ACTIONS.GOHOME)"]
    G -- "RunAway / Wander" --> J["locomotor 移动"]
    H --> K["SGrabbit ActionHandler EAT -> eat"]
    I --> L["SGrabbit ActionHandler GOHOME -> action"]
    K --> M["PerformBufferedAction"]
    L --> M
~~~

### `0x43013110` 兔子链路 / Prefab 装配

`prefabs/rabbit.lua` 先 `require("brains/rabbitbrain")`。
在 master sim 分支里，它添加 `locomotor`、`drownable`，然后绑定 `SGrabbit` 和 `RabbitBrain`。
后续再添加 `eater`、`inventoryitem`、`knownlocations`、`timer`、`health`、`combat` 等组件。

#### `0x43013111` 装配验证点

- `locomotor` 在 `SetStateGraph` 之前。
- `SetBrain` 在 `SetStateGraph` 之后。
- client pristine 分支不会运行 Brain。

### `0x43013120` 兔子链路 / Brain 决策

`RabbitBrain:OnStart` 的 root 是 `PriorityNode(..., .25)`。
优先级从 panic、电围栏、逃离威胁开始。
之后才是 `"gohome"` 事件、夜晚回家、春季回家、吃食物和游荡。

#### `0x43013121` 行为验证点

- `GoHomeAction` 返回 `BufferedAction(inst, home, ACTIONS.GOHOME)`。
- `EatFoodAction` 找到 bait 后返回 `BufferedAction(inst, target, ACTIONS.EAT)`。
- `RunAway` 和 `Wander` 直接驱动移动，不经过 `PerformBufferedAction`。

### `0x43013130` 兔子链路 / StateGraph 执行

`SGrabbit.lua` 的 action handler 把 `ACTIONS.EAT` 映射到 `eat`。
它把 `ACTIONS.GOHOME` 映射到 `action`。
`action` 状态在 `onenter` 中调用 `inst:PerformBufferedAction()`。
`eat` 状态在 timeout 后调用 `inst:PerformBufferedAction()`。

#### `0x43013131` SG 验证点

- `locomote` 事件读取 `locomotor:WantsToMoveForward()` 和 `WantsToRun()`。
- `run`、`run_loop`、`hop` 负责移动动画。
- `trapped` 状态会 `ClearBufferedAction()`。

### `0x43013210` Wilson 链路 / `brains/wilsonbrain.lua`

Wilson 的 Brain 很小。
它 require `behaviours/chaseandattack`。
root 是 `PriorityNode`，包含一个检查 `CONTROL_PRIMARY` 的 `WhileNode` 和一个兜底 `ChaseAndAttack`。

#### `0x43013211` 读法边界

- 这个文件适合理解 Brain 结构，不适合单独解释完整玩家输入。
- 玩家输入到动作的主链路仍应回到 `input.lua`、`playercontroller`、`BufferedAction` 和 `SGwilson`。
- `ChaseAndAttack` 会直接驱动 `combat` 与 `locomotor`。

### `0x43013310` Deerclops 链路 / Prefab 到 Boss Brain

`prefabs/deerclops.lua` 在 master sim 分支添加 `locomotor`，然后 `SetStateGraph("SGdeerclops")`。
添加 `knownlocations` 后调用 `SetBrain(brain)`。
Brain 来源是文件顶部的 `require "brains/deerclopsbrain"`。

#### `0x43013311` Boss 装配验证点

- `SetStateGraph("SGdeerclops")` 在添加多数 gameplay 组件之前。
- `SetBrain(brain)` 在 `knownlocations` 之后，因为 Brain 会读写已知位置。
- `combat:SetRetargetFunction` 和 `SetKeepTargetFunction` 在 Brain 启动前已经存在。

### `0x43013320` Deerclops 链路 / Boss Brain Root

`DeerclopsBrain:OnStart` 的 root 是 `PriorityNode(..., 0.5)`。
高优先级分支会通过 `ActionNode` 推送 `"doicegrow"`。
中间分支包含 `AttackWall`、`Leash`、`FaceEntity` 和 `ChaseAndAttack`。
拆建筑时 `BaseDestroy` 返回 `BufferedAction(inst, target, ACTIONS.HAMMER)`，再由 `DoAction` 推入 locomotor。

#### `0x43013321` Boss 行为验证点

- `ActionNode` 可以直接 `PushEvent` 或改组件字段。
- `ChaseAndAttack` 直接尝试攻击，不创建 `BufferedAction`。
- `DoAction(BaseDestroy, "DestroyBase", true)` 创建 `ACTIONS.HAMMER`。

### `0x43013330` Deerclops 链路 / Boss SG 承接

`SGdeerclops.lua` 将 `ACTIONS.HAMMER` 映射到 `attack`。
`attack` 状态在 timeline 中检查 `inst.bufferedaction.action == ACTIONS.HAMMER`。
随后它清理 buffered action，并直接调用 workable target 的 `Destroy(inst)`。
因此拆建筑行为跨过 Brain、DoAction、locomotor、SG action handler 和 `workable` 组件。

#### `0x43013331` SG 验证点

- `SGdeerclops.lua` 的 `ActionHandler(ACTIONS.HAMMER, "attack")` 是拆建筑 action 的入口。
- `doattack` 事件与 `combat` 目标相关，不等同于 `DoAction(BaseDestroy)`。
- `ACTIONS.HAMMER` 不走通用 `PerformBufferedAction()`，而是在 `attack` timeline 中特殊处理。

## `0x43014110` 结构细节 / 追踪时的顺序 / 从 Prefab 读到 Behaviour

先在 Prefab 找 `require "brains/..."`、`SetStateGraph` 和 `SetBrain`。
再进 Brain 的 `OnStart` 看 root node。
最后按 node 类型决定下一跳。

### `0x43014111` 下一跳规则

- `DoAction` 下一跳是 action function、`BufferedAction`、`locomotor:PushAction` 和 SG action handler。
- `ChaseAndAttack` 下一跳是 `combat` 与 `locomotor`。
- `RunAway` 下一跳是威胁搜索和 `locomotor`。
- `ActionNode` 下一跳是它包住的 Lua 函数。

## `0x43014210` 结构细节 / Brain 与 SG 不互相包含 / 运行时协作

Brain 不调用 `SetStateGraph`。
SG 不调用 `SetBrain`。
它们通过实体上的组件、事件和 buffered action 协作。

### `0x43014211` 常见误读

- 看到 `inst.sg:HasStateTag` 不代表 Brain 属于 SG。
- 看到 `ActionHandler` 不代表 SG 负责选择行为。
- 看到 `DoAction` 不代表 action 已经执行。

## `0x43015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "SetStateGraph|SetBrain|PriorityNode|DoAction|ChaseAndAttack|ActionHandler|PerformBufferedAction" \
  scripts/prefabs/rabbit.lua \
  scripts/brains/rabbitbrain.lua \
  scripts/stategraphs/SGrabbit.lua \
  scripts/brains/wilsonbrain.lua \
  scripts/prefabs/deerclops.lua \
  scripts/brains/deerclopsbrain.lua \
  scripts/stategraphs/SGdeerclops.lua
~~~

### `0x43015110` 推荐顺序

先跑兔子链路，因为文件短且覆盖 `DoAction`、`RunAway`、`Wander` 和 SG action handler。
再读 Wilson，确认极简 Brain 的结构。
最后读 Deerclops，把普通链路扩展到 Boss 行为。

#### `0x43015111` 最小闭环

用兔子的 `EatFoodAction` 验证一条完整动作线。
它从 `RabbitBrain` 产生 `BufferedAction(ACTIONS.EAT)`。
随后它经 `DoAction` 推给 `locomotor`，再由 `SGrabbit` 的 `eat` 状态调用 `PerformBufferedAction()`。
