# `0x42020000` Behaviour Tree

Behaviour Tree 页解释 `BT:Update` 的三段更新、节点状态机、组合节点和常用 leaf behaviour。

这里的关键结论是：behaviour tree 不只是“返回动作意图”。
有些节点会创建 `BufferedAction` 并交给 `locomotor`。
有些节点会直接调用 `combat`、`locomotor` 或实体事件。

## `0x42021100` 本页定位 / 要回答的运行时问题

一个 Brain 的 `OnStart` 通常创建 root node，再用 `BT(self.inst, root)` 包装。
每次 Brain 更新时，`BT:Update` 依次执行 `Visit`、`SaveStatus` 和 `Step`。

### `0x42021110` 状态常量

`scripts/behaviourtree.lua` 定义四个字符串状态。
它们是 `SUCCESS`、`FAILED`、`READY` 和 `RUNNING`。

#### `0x42021111` 验证点

- `Visit` 决定本次 tick 状态。
- `SaveStatus` 把 `status` 保存到 `lastresult`。
- `Step` 在非 `RUNNING` 时重置节点，或推进仍在运行的子树。
- `GetTreeSleepTime` 从正在运行的节点中取最小 sleep time。

## `0x42022000` 源码锚点

| 文件 | 入口 | 需要核对的事实 |
| --- | --- | --- |
| `scripts/behaviourtree.lua` | `BT:Update` | 顺序是 `Visit()`、`SaveStatus()`、`Step()`。 |
| `scripts/behaviourtree.lua` | `PriorityNode:Visit` | 到期后重新评估子节点，并保留成功或运行中的最高优先级节点。 |
| `scripts/behaviourtree.lua` | `SequenceNode:Visit` | 子节点失败或运行时立即返回。 |
| `scripts/behaviourtree.lua` | `ParallelNode:Visit` | 任一子节点失败则整体失败，全部完成则成功。 |
| `scripts/behaviourtree.lua` | `EventNode:OnEvent` | 设置 `triggered`，强制 Brain 更新，并清掉父级 `PriorityNode.lasttime`。 |
| `scripts/behaviours/doaction.lua` | `DoAction:Visit` | 获取 `BufferedAction`，注册成功失败回调，再 `PushAction`。 |
| `scripts/behaviours/chaseandattack.lua` | `ChaseAndAttack:Visit` | 直接驱动 `combat` 和 `locomotor`。 |
| `scripts/behaviours/runaway.lua` | `RunAway:Visit` | 查找威胁，驱动逃跑移动，并设置 sleep。 |

### `0x42022110` 主锚点 / `scripts/behaviourtree.lua`

`BehaviourNode` 是基类。
默认 `Visit` 会失败，所以具体节点必须覆写它。
节点树的 sleep time 由正在 `RUNNING` 的节点贡献。

#### `0x42022111` 搜索信号

~~~bash
rg -n "BT:Update|BehaviourNode|PriorityNode|SequenceNode|ParallelNode|EventNode|WhileNode|GetTreeSleepTime" \
  scripts/behaviourtree.lua
~~~

## `0x42023000` 运行流程

~~~mermaid
flowchart TD
    A["Brain:OnUpdate"]
    A --> B["BT:Update"]
    B --> C["root:Visit"]
    C --> D{"node status"}
    D -- "SUCCESS/FAILED" --> E["SaveStatus 后 Step 重置"]
    D -- "RUNNING" --> F["保留运行子树"]
    F --> G["leaf node Sleep 或 period"]
    E --> H["BT:GetSleepTime"]
    G --> H
    H --> I["BrainManager 调度下一次更新"]
    C --> J["DoAction / ChaseAndAttack / RunAway"]
    J --> K["locomotor、combat、BufferedAction 或实体事件"]
~~~

### `0x42023110` 组合节点 / `PriorityNode`

`PriorityNode` 有 `period`。
到期时它从前到后访问子节点。
子节点返回 `SUCCESS` 或 `RUNNING` 后，当前索引会被记录为 `self.idx`。
未到期时，它只继续访问当前正在运行的子节点。

#### `0x42023111` 边界条件

- `period = 0` 的 Brain 每轮都可重新评估。
- `EventNode` 可以清掉父级 `PriorityNode.lasttime`，让高优先级事件立即被评估。
- `PriorityNode:GetSleepTime` 在 `RUNNING` 时返回下一次评估间隔。

### `0x42023210` 顺序与并行节点 / `SequenceNode`、`SelectorNode`、`ParallelNode`

`SequenceNode` 在子节点 `FAILED` 或 `RUNNING` 时返回对应状态。
`SelectorNode` 在子节点 `SUCCESS` 或 `RUNNING` 时返回对应状态。
`ParallelNode` 会访问多个子节点，任一失败则失败，全部完成则成功，否则继续运行。

#### `0x42023211` `WhileNode` 的真实结构

`WhileNode(cond, name, node)` 不是独立类。
它返回一个 `ParallelNode`，子节点是 `ConditionNode(cond, name)` 和业务节点。
因此条件每次更新都会重新检查，条件失败会中断业务节点。

### `0x42023310` 叶子节点 / `DoAction`

`DoAction` 的 `getactionfn` 返回 `BufferedAction` 或 `nil`。
有 action 时，它注册失败和成功回调，调用 `self.inst.components.locomotor:PushAction(action, shouldrun)`，并把自身设为 `RUNNING`。
之后它根据回调、超时或 action 失效改成 `SUCCESS` 或 `FAILED`。

#### `0x42023311` 行为边界

- `DoAction` 不直接调用 `PerformBufferedAction`。
- `PerformBufferedAction` 通常在 SG 的 action state 中执行。
- `DoAction` 需要实体有 `locomotor`。

### `0x42023320` 叶子节点 / `ChaseAndAttack`

`ChaseAndAttack` 不是 `BufferedAction` 包装器。
它通过 `combat:ValidateTarget()`、`combat:TryAttack()`、`locomotor:GoToPoint()` 和 `locomotor:Stop()` 驱动战斗。
成功、失败和追击超时都在该节点内判断。

#### `0x42023321` 行为边界

- 攻击目标死亡时返回 `SUCCESS`。
- 目标无效、放弃距离或追击时间超限时返回 `FAILED`。
- 仍在追击时调用 `self:Sleep(.125)`。

### `0x42023330` 叶子节点 / `RunAway`

`RunAway` 查找威胁实体。
找到后它直接通过 `locomotor:RunInDirection()`、`WalkInDirection()` 或 `homeseeker:GoHome(true)` 逃离。
到安全距离后返回 `SUCCESS`。

#### `0x42023331` 行为边界

- 找不到威胁时返回 `FAILED`。
- 威胁无效时停止移动并返回 `FAILED`。
- 仍在逃跑时调用 `self:Sleep(.25)`。

## `0x42024110` 结构细节 / Sleep Time / 从节点到 BrainManager

`BT:GetSleepTime` 会调用 root 的 `GetTreeSleepTime`。
如果 `BT.forceupdate` 为真，`GetSleepTime` 直接返回 `0`。
`GetTreeSleepTime` 只向正在 `RUNNING` 的子节点递归。
没有 sleep time 时返回 `nil`，BrainManager 会把该 Brain 放入 `hibernaters`。

### `0x42024111` 需要核对的字段

- `BehaviourNode:Sleep(t)` 写入 `nextupdatetime`。
- `BT:ForceUpdate()` 会让下一轮 sleep time 变成 `0`。
- leaf `RUNNING` 且非 `ConditionNode` 时，`GetSleepTime` 返回剩余时间。
- `PriorityNode:GetSleepTime` 由 `period` 和 `lasttime` 决定。

## `0x42024210` 结构细节 / 事件节点 / `EventNode`

`EventNode` 在构造时监听实体事件。
事件触发后，它设置 `triggered` 和 `data`，调用 `brain:ForceUpdate()`，并让父级 `PriorityNode` 重新评估。

### `0x42024211` 验证动作

用 `rabbitbrain.lua` 的 `"gohome"` 事件验证。
`prefabs/rabbit.lua` 被攻击时会向附近兔子 `PushEvent("gohome")`。
`EventNode(self.inst, "gohome", DoAction(...))` 会因此立即尝试回家动作。

## `0x42025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "DoAction:Visit|ChaseAndAttack:Visit|RunAway:Visit|Sleep\\(|PushAction|TryAttack|RunInDirection" \
  scripts/behaviourtree.lua \
  scripts/behaviours/doaction.lua \
  scripts/behaviours/chaseandattack.lua \
  scripts/behaviours/runaway.lua
~~~

### `0x42025110` 推荐顺序

先读 `BT:Update`。
再读 `PriorityNode:Visit`。
最后任选一个 leaf behaviour，看它是创建 `BufferedAction`，还是直接驱动组件。

#### `0x42025111` 最小闭环

用 `DoAction` 追 `BufferedAction` 链路。
用 `ChaseAndAttack` 追直接组件链路。
这两条路径能覆盖 Behaviour Tree 最容易混淆的两类副作用。
