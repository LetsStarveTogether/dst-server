# `0x41030000` SGwilson

SGwilson 是玩家表现层的核心样例。
它同时展示服务端权威动作、客户端预测状态、动作失败回退和大量专用状态。

## `0x41031111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

目标是从玩家动作抽样追踪到 `ActionHandler`、目标状态、timeline 和 `PerformBufferedAction`。
不要先通读整份 `SGwilson.lua`。

## `0x41031121` 本页定位 / 要回答的运行时问题 / 服务端与客户端分工 / 验证点

`scripts/stategraphs/SGwilson.lua` 返回 `StateGraph("wilson", ...)`。
`scripts/stategraphs/SGwilson_client.lua` 返回 `StateGraph("wilson_client", ...)`。
客户端状态通过 `server_states` 说明可接受的服务端状态集合。

## `0x41032000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/stategraphs/SGwilson.lua` | `local actionhandlers` | 玩家服务端动作到状态的映射 |
| `scripts/stategraphs/SGwilson.lua` | `name = "doshortaction"` | 常见短动作执行状态 |
| `scripts/stategraphs/SGwilson.lua` | `name = "attack"` | 常见战斗动作执行状态 |
| `scripts/stategraphs/SGwilson.lua` | `CommonStates.AddHopStates` | 玩家划船跳跃复用状态 |
| `scripts/stategraphs/SGwilson_client.lua` | `server_states` | 客户端预测状态匹配表 |
| `scripts/actions.lua` | `ACTIONS` | 动作常量来源 |

### `0x41032111` 主锚点 / `scripts/stategraphs/SGwilson.lua` / 搜索信号

先搜 `ActionHandler(ACTIONS.PICKUP`、`ActionHandler(ACTIONS.CHOP` 或 `ActionHandler(ACTIONS.ATTACK`。
然后搜对应状态名，例如 `name = "doshortaction"`、`name = "chop"` 或 `name = "attack"`。

### `0x41032121` 主锚点 / `scripts/stategraphs/SGwilson_client.lua` / 搜索信号

搜同一个 `ACTIONS.*` 和同名状态。
再确认客户端状态里是否声明了 `server_states` 或调用 `PerformPreviewBufferedAction()`。

## `0x41033000` 运行流程

~~~mermaid
flowchart TD
    A["BufferedAction: ACTIONS.PICKUP / CHOP / ATTACK"]
    A --> B["SGwilson actionhandler"]
    B --> C["目标 State"]
    C --> D["onenter 缓存 action 或 target"]
    D --> E["timeline / timeout / animover"]
    E --> F["PerformBufferedAction"]
    F --> G["组件或 prefab 侧副作用"]
    A --> H["SGwilson_client actionhandler"]
    H --> I["预测 State"]
    I --> J["PerformPreviewBufferedAction"]
    J --> K["server_states 匹配服务端纠正"]
~~~

### `0x41033111` 短动作链路 / `ACTIONS.PICKUP` 到 `doshortaction` / 边界条件

服务端 `SGwilson.lua` 中 `ACTIONS.PICKUP` 会根据物品、骑乘、忙碌状态等条件选择不同目标状态。
进入短动作状态后，真实副作用通常由 timeline 中的 `inst:PerformBufferedAction()` 触发。

### `0x41033211` 工作动作链路 / `ACTIONS.CHOP` 到 `chop_start` / `chop` / 边界条件

`CHOP` 不是简单的一帧动作。
服务端状态会使用 `chop_start` 和 `chop` 等状态表达起手、循环和工作帧。
客户端 `SGwilson_client.lua` 对应状态用 `server_states = { "chop_start", "chop" }` 匹配服务端。

### `0x41033311` 攻击动作链路 / `ACTIONS.ATTACK` 到 `attack` / 边界条件

攻击状态会缓存目标、处理武器类型和特殊攻击分支。
阅读时不要只搜第一个 `PerformBufferedAction`。
应同时核对 `combat:StartAttack()`、timeline 中的动作帧和失败回退。

## `0x41034111` 结构细节 / ActionHandler 规模 / `local actionhandlers` / 需要核对的字段

`SGwilson.lua` 的 actionhandlers 是玩家动作入口总表。
表项可以返回字符串状态，也可以用函数检查装备、目标、骑乘、船和平台条件。
这正是 SGwilson 不能按文件顺序线性阅读的原因。

## `0x41034211` 结构细节 / 状态内副作用 / `timeline` 与 `PerformBufferedAction` / 需要核对的字段

SGwilson 的真实动作通常不在 actionhandler 里执行。
actionhandler 只选择状态。
状态内的 `onenter`、`timeline`、`ontimeout` 或 event handler 再决定何时调用 `PerformBufferedAction`。

## `0x41034311` 结构细节 / 客户端预测 / `server_states` / 需要核对的字段

客户端状态的 `server_states` 用于判断当前预测状态是否能接受服务端状态。
`forward_server_states = true` 表示状态切换时继续沿用前一组服务端匹配状态。
这和 `stategraph.lua` 中 `StateGraphInstance:ServerStateMatches`、`GoToState` 的客户端分支对应。

## `0x41035100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "ActionHandler\\(ACTIONS\\.(PICKUP|CHOP|ATTACK)|PerformBufferedAction|server_states" \
  scripts/stategraphs/SGwilson.lua \
  scripts/stategraphs/SGwilson_client.lua

rg -n "name = \\\"(doshortaction|chop_start|chop|attack)\\\"" \
  scripts/stategraphs/SGwilson.lua \
  scripts/stategraphs/SGwilson_client.lua
~~~

### `0x41035111` 推荐顺序 / 最小闭环

先选一个动作，例如 `ACTIONS.PICKUP`。
在服务端找 actionhandler 返回的状态名。
再跳到状态定义，找 `PerformBufferedAction` 的实际帧。
最后在客户端文件找同动作的预测状态和 `server_states`。

### `0x41035112` 推荐顺序 / 失败路径

继续搜 `actionfailed`。
很多状态会在动作失败时回到 `idle` 或进入专门回退状态。
这能验证文档中的流程图没有把成功路径误写成唯一路径。
