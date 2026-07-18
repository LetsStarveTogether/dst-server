# `0x32030000` Player Input 到 Action

本页按 `input.lua -> playercontroller.lua -> playeractionpicker.lua -> componentactions.lua -> bufferedaction.lua
-> stategraph/action fn` 写完整链路。
它用鼠标输入作为主线，同时标出客户端预测和 server 权威执行的分叉。

## `0x32031111` 本页定位 / 要回答的运行时问题 / 玩家点击后发生什么 / 验证点

目标是验收从鼠标目标、候选动作、`BufferedAction`、RPC/preview 到服务端 `ACTIONS.*.fn` 的关键链路。

## `0x32031211` 本页定位 / 输入层与动作层的边界 / 输入不直接执行组件副作用 / 边界条件

`input.lua` 提供鼠标位置、HUD 下实体、世界下实体和控制状态。
`PlayerController` 才把这些状态转成 `LMBaction`、`RMBaction`、RPC 或 locomotor preview。
组件副作用仍由服务端动作执行链路完成。

## `0x32032000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/input.lua` | `TheInput:GetWorldPosition` | 鼠标世界坐标来源 |
| `scripts/input.lua` | `TheInput:GetWorldEntityUnderMouse` | 鼠标实体目标来源 |
| `scripts/input.lua` | `TheInput:GetHUDEntityUnderMouse` | HUD 阻挡判断 |
| `scripts/components/playercontroller.lua` | `PlayerController:OnUpdate` | 缓存 `LMBaction` 和 `RMBaction` |
| `scripts/components/playercontroller.lua` | `PlayerController:OnLeftClick` | 左键点击提交 |
| `scripts/components/playercontroller.lua` | `PlayerController:OnRightClick` | 右键点击提交 |
| `scripts/components/playercontroller.lua` | `PlayerController:DoAction` | 本地、非预测客户端、预测客户端三路提交 |
| `scripts/components/playeractionpicker.lua` | `DoGetMouseActions` | 计算左键和右键候选 |
| `scripts/componentactions.lua` | `EntityScript:CollectActions` | 按 action type 收集候选 |
| `scripts/bufferedaction.lua` | `BufferedAction` | 动作上下文对象 |
| `scripts/entityscript.lua` | `EntityScript:PushBufferedAction` | 权威动作提交入口 |
| `scripts/stategraphs/SGwilson.lua` | `ActionHandler(ACTIONS.*)` | 服务端玩家动作状态映射 |
| `scripts/stategraphs/SGwilson_client.lua` | `ActionHandler(ACTIONS.*)` | 客户端预测动作状态映射 |

### `0x32032111` 主锚点 / `scripts/components/playercontroller.lua` / 搜索信号

先找 `LMBaction` 和 `RMBaction`。
它们在 `OnUpdate` 中由 `playeractionpicker:DoGetMouseActions()` 刷新，点击时由
`GetLeftMouseAction()` 和 `GetRightMouseAction()` 读取。

### `0x32032121` 主锚点 / `scripts/input.lua` / 搜索信号

再找 `GetWorldPosition`、`GetWorldEntityUnderMouse` 和 `GetHUDEntityUnderMouse`。
这些函数解释为什么 UI 上的点击不会直接进入世界动作链路。

## `0x32033000` 运行流程

~~~mermaid
flowchart TD
    A["Input: mouse/control state"]
    A --> B["PlayerController:OnUpdate"]
    B --> C["PlayerActionPicker:DoGetMouseActions"]
    C --> D["LMBaction / RMBaction cache"]
    D --> E{"OnLeftClick / OnRightClick"}
    E --> F["GetLeftMouseAction / GetRightMouseAction"]
    F --> G["PlayerController:DoAction"]
    G --> H{"运行端"}
    H -->|mastersim| I["locomotor:PushAction"]
    H -->|client without locomotor| J["non_preview_cb -> RPC"]
    H -->|predicting client| K["locomotor:PreviewAction + preview_cb -> RPC"]
    I --> L["EntityScript:PushBufferedAction"]
    J --> M["server OnRemote* rebuild action"]
    K --> M
    M --> L
    L --> N["SGwilson / SGwilson_client ActionHandler"]
    N --> O["PerformBufferedAction"]
    O --> P["BufferedAction:Do"]
    P --> Q["ACTIONS.*.fn / component side effect"]
~~~

### `0x32033111` 缓存分段 / `PlayerController:OnUpdate` / 验证点

非手柄模式下，`OnUpdate` 会把 `DoGetMouseActions()` 的结果写入 `self.LMBaction` 和 `self.RMBaction`。
手柄模式会清空鼠标动作并转去 `UpdateControllerTargets(dt)`。

### `0x32033121` 缓存分段 / `DoGetMouseActions` / 验证点

`DoGetMouseActions` 会在没有显式 position 时读取 `TheInput:GetWorldPosition()` 和
`TheInput:GetWorldEntityUnderMouse()`。
如果 HUD 下有实体，它会提前返回。
AOE targeting 时，position 会来自 reticule，而不是普通鼠标世界点。

### `0x32033211` 点击分段 / `OnLeftClick` / 验证点

左键先处理拖拽、放置、AOE 复施、双击动作和地图目标。
常规路径读取 `GetLeftMouseAction()`。
如果没有左键动作，它会构造 `BufferedAction(self.inst, nil, ACTIONS.WALKTO, nil, position)`。

### `0x32033221` 点击分段 / `OnRightClick` / 验证点

右键会先关闭放置或 AOE targeting。
常规路径读取 `GetRightMouseAction()`。
没有右键动作时，它可能归还 active item、尝试 AOE targeting 或只发送空 `RPC.RightClick`。

### `0x32033311` 提交分段 / `PlayerController:DoAction` / 验证点

`DoAction` 大多数路径会先检查 `buffaction`、目标有效性、忙碌状态和重复动作。
非预测客户端没有 locomotor、存在 `non_preview_cb` 且没有 `pre_action_cb` 时，会先发送 RPC。
然后处理攻击 retarget、高亮、自动装备和 hold action。
`ismastersim` 时走 `locomotor:PushAction(buffaction, true)`。
没有 locomotor 的非预测客户端通过 `non_preview_cb` 发送 RPC。
有 locomotor 且可移动时走 `locomotor:PreviewAction(buffaction, true)`。

### `0x32033321` 提交分段 / 非鼠标入口 / 验证点

鼠标只是本页主线，不是玩家动作的唯一入口。

`PlayerController:DoActionButton` 覆盖 action button。

controller attack、controller action、inventory tile action 和 map action 也会构造 `BufferedAction` 或远端 RPC。

这些入口最终仍回到 `DoAction`、`locomotor:PreviewAction`、`OnRemote*` 或 `RemoteBufferedAction`。
`OnRemoteControllerActionButtonPoint` 的 boat cannon 分支会把玩家实例显式传给 action picker。
具体调用是 `GetCannonAimActions(self.inst, position, false)`。
读手柄炮击时要确认 action picker 的参数签名，而不是套用鼠标路径。

### `0x32033331` 提交分段 / RPC 与预测 / 验证点

客户端点击路径会为 `act` 填入 `preview_cb` 或 `non_preview_cb`。
这些回调发送 `RPC.LeftClick`、`RPC.RightClick`、`RPC.ActionButton` 等消息。
服务端 `OnRemote*` 路径会根据 action code、target、position 和 mod 名重建对应动作。

## `0x32034111` 结构细节 / 输入来源 / 鼠标目标与 HUD 阻挡 / 需要核对的字段

`TheInput:GetHUDEntityUnderMouse()` 是输入链路进入世界前的重要过滤点。
`TheInput:GetWorldEntityUnderMouse()` 提供实体目标。
`TheInput:GetWorldPosition()` 或 `TheSim:ProjectScreenPos` 提供点位。

## `0x32034211` 结构细节 / 候选动作缓存 / `LMBaction` 与 `RMBaction` / 需要核对的字段

`GetLeftMouseAction()` 只是返回 `self.LMBaction`。
`GetRightMouseAction()` 只是返回 `self.RMBaction`。
真正复杂的候选计算在 `PlayerActionPicker:DoGetMouseActions()`。

## `0x32034311` 结构细节 / 服务端权威执行 / `PushAction` 到 `BufferedAction:Do` / 需要核对的字段

本地权威端通常经 `locomotor:PushAction` 推进到实体动作。
服务端接收 RPC 后也会走同一类权威动作执行路径。
最终验证点仍是 `EntityScript:PushBufferedAction`、`StateGraphInstance:StartAction` 和
`BufferedAction:Do`。

## `0x32035100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "LMBaction|RMBaction|DoGetMouseActions|GetLeftMouseAction|GetRightMouseAction" \
  scripts/components/playercontroller.lua
rg -n "OnLeftClick|OnRightClick|function PlayerController:DoAction" \
  scripts/components/playercontroller.lua
rg -n "OnRemoteControllerActionButtonPoint|GetCannonAimActions|BOAT_CANNON_SHOOT" \
  scripts/components/playercontroller.lua \
  scripts/components/playeractionpicker.lua
rg -n "GetWorldPosition|GetWorldEntityUnderMouse|GetHUDEntityUnderMouse|IsControlPressed" \
  scripts/input.lua
rg -n "GetLeftClickActions|GetRightClickActions|GetSceneActions|GetUseItemActions|GetPointActions|CollectActions" \
  scripts/components/playeractionpicker.lua scripts/componentactions.lua
rg -n "PushBufferedAction|PerformBufferedAction|BufferedAction:Do|StateGraphInstance:StartAction" \
  scripts/entityscript.lua scripts/bufferedaction.lua scripts/stategraph.lua
rg -n "ActionHandler\\(ACTIONS\\." \
  scripts/stategraphs/SGwilson.lua scripts/stategraphs/SGwilson_client.lua
~~~

### `0x32035111` 推荐顺序 / 最小闭环

先读 `PlayerController:OnUpdate`，确认 `LMBaction/RMBaction` 何时刷新。
再读 `OnLeftClick` 或 `OnRightClick`，确认点击如何提交。
然后读 `DoAction`，区分 mastersim、非预测客户端和预测客户端。
最后回到 `PushBufferedAction` 与 `BufferedAction:Do` 验证最终副作用。
