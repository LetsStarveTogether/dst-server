# `0x32020000` BufferedAction

BufferedAction 页解释一个候选动作如何变成可排队、可预测、可失败的动作对象。
核心边界是：`BufferedAction` 保存动作上下文，`EntityScript` 安排动作生命周期，StateGraph 决定动画状态，
`BufferedAction:Do` 才调用 `ACTIONS.*.fn`。

## `0x32021111` 本页定位 / 要回答的运行时问题 / 候选动作如何被提交 / 验证点

目标是把 `BufferedAction(...)`、`EntityScript:PushBufferedAction`、`StateGraphInstance:StartAction`、
`EntityScript:PerformBufferedAction` 和 `BufferedAction:Do` 串成一条可复查链路。

## `0x32021211` 本页定位 / 与 `pre_action_cb` 的边界 / 回调不在 `BufferedAction:Do` 中触发 / 验证点

`BufferedAction:Do` 只检查 `IsValid()`，然后调用 `self.action.fn(self)`。
`ACTIONS.*.pre_action_cb` 通常在 `PlayerController:DoAction` 或其他动作发起路径中先触发。
不要把 `pre_action_cb` 写成 `BufferedAction:Do` 的一部分。

## `0x32022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/bufferedaction.lua` | `BufferedAction` | 保存 doer、target、action、invobject、pos、recipe、distance 等上下文 |
| `scripts/bufferedaction.lua` | `BufferedAction:IsValid` | 执行前验证 doer、target、invobject、pos、validfn |
| `scripts/bufferedaction.lua` | `BufferedAction:Do` | 调用 `action.fn` 并分派 success/fail 回调 |
| `scripts/entityscript.lua` | `EntityScript:PushBufferedAction` | 接收动作并决定 WALKTO、instant 或 SG 路径 |
| `scripts/entityscript.lua` | `EntityScript:PerformBufferedAction` | 面向目标、推送 `performaction`、调用 `bufferedaction:Do` |
| `scripts/entityscript.lua` | `EntityScript:GetBufferedAction` | 返回实体或 locomotor 当前动作 |
| `scripts/stategraph.lua` | `StateGraphInstance:StartAction` | 用 `sg.actionhandlers[action]` 选择状态或直接执行 |
| `scripts/stategraphs/SGwilson.lua` | `ActionHandler(ACTIONS.*)` | 服务端玩家动作到状态的映射 |
| `scripts/stategraphs/SGwilson_client.lua` | `ActionHandler(ACTIONS.*)` | 客户端预测动作到状态的映射 |

### `0x32022111` 主锚点 / `scripts/entityscript.lua` / 搜索信号

先找 `PushBufferedAction`。
它是判断动作进入 WALKTO、instant、StateGraph 或失败路径的最短入口。

### `0x32022121` 主锚点 / `scripts/bufferedaction.lua` / 搜索信号

再找 `BufferedAction:Do` 和 `BufferedAction:IsValid`。
这里能确认最终副作用不是在候选收集阶段发生的。

## `0x32023000` 运行流程

~~~mermaid
flowchart TD
    A["PlayerActionPicker / PlayerController 生成 BufferedAction"]
    A --> B["EntityScript:PushBufferedAction"]
    B --> C{"TestForStart / IsValid"}
    C -->|失败| D["PushEvent actionfailed"]
    C -->|WALKTO| E["PushEvent performaction + Succeed"]
    C -->|instant| F["PushEvent performaction + BufferedAction:Do"]
    C -->|普通动作| G["StateGraphInstance:StartAction"]
    C -->|无 SG| R["PushEvent startaction"]
    G -->|有 deststate| H["GoToState 动画状态"]
    G -->|无 deststate| I["EntityScript:PerformBufferedAction"]
    H --> J["状态帧或事件调用 PerformBufferedAction"]
    I --> K["BufferedAction:Do"]
    J --> K
    K --> L["ACTIONS.*.fn"]
    L --> M{"success"}
    M -->|true| N["OnUsedAsItem + Succeed"]
    M -->|false| O["Fail"]
~~~

### `0x32023111` 提交分段 / `PushBufferedAction` / 验证点

`PushBufferedAction` 会先消除重复动作，并让已有的 `self.bufferedaction` 失败。
然后用 `bufferedaction:TestForStart()` 做执行前验证。
`TestForStart` 指向 `BufferedAction:IsValid`。
如果这里失败，函数只推送 `actionfailed` 并返回。
它不会调用 `bufferedaction:Fail()`。

### `0x32023121` 提交分段 / WALKTO 与 Instant / 验证点

`ACTIONS.WALKTO` 是特殊路径。
它推送 `performaction` 后直接 `Succeed()`，不会调用 `BufferedAction:Do`。
`action.instant` 或 `bufferedaction.options.instant` 会推送 `performaction`，再立即调用 `bufferedaction:Do()`。

### `0x32023131` 提交分段 / StateGraph 路径 / 验证点

普通动作会进入 `self.sg:StartAction(bufferedaction)`。
如果匹配到 `actionhandlers[action]` 且有 `deststate`，SG 会 `GoToState`。
如果 handler 没有 `deststate`，SG 会直接调用 `inst:PerformBufferedAction()`。
如果没有可用 handler，`PushBufferedAction` 会推送 `performaction`，再让动作失败。
如果实体没有 `self.sg`，`PushBufferedAction` 只推送 `startaction`。
它不会在这一分支直接执行 `BufferedAction:Do()`。

### `0x32023211` 执行分段 / `PerformBufferedAction` / 验证点

`PerformBufferedAction` 会在调用 `bufferedaction:Do()` 前推送 `performaction`。
它会暂存局部变量 `bufferedaction`，因为 `Do()` 期间仍可能读取 `inst.bufferedaction`。

### `0x32023221` 执行分段 / `BufferedAction:Do` / 验证点

`Do()` 的顺序是 `IsValid()`、`action.fn(self)`、记录 `reason`、成功时 `OnUsedAsItem` 和 `Succeed()`、
失败时 `Fail()`。
`EntityScript:PerformBufferedAction` 在失败后还会推送 `actionfailed`。

### `0x32023231` 执行分段 / 失败路径矩阵 / 边界条件

不是所有 `BufferedAction:Do()` 失败都会触发 `actionfailed`。

| 失败位置 | 事件与回调 |
| --- | --- |
| `PushBufferedAction` 的 `TestForStart()` 失败 | 只推送 `actionfailed`，然后直接返回。 |
| `instant` 分支中 `Do()` 返回 false | 已推送 `performaction`，`Do()` 内部 `Fail()`，不由这一层额外推 `actionfailed`。 |
| SG 状态调用 `PerformBufferedAction()` 后 `Do()` 返回 false | `Do()` 内部 `Fail()`，随后 `PerformBufferedAction` 推送 `actionfailed` 并再次调用已清空回调的 `Fail()`。 |
| 没有可用 SG action handler | 推送 `performaction`，然后 `Fail()`。 |

## `0x32024111` 结构细节 / `BufferedAction` 字段 / 上下文字段 / 需要核对的字段

构造函数保存 `doer`、`target`、`initialtargetowner`、`action`、`invobject`、`doerownsobject`、`pos`、
`rotation`、`recipe`、`distance`、`arrivedist`、`forced`、`autoequipped`、`skin`、`onsuccess`、`onfail` 和 `options`。
`pos` 会包装为 `DynamicPosition`。

## `0x32024121` 结构细节 / `BufferedAction` 字段 / `IsValid` 校验字段 / 需要核对的字段

`BufferedAction:IsValid` 不只检查 doer 和 target 是否有效。
`initialtargetowner` 用来防止目标所属容器变化。
`doerownsobject` 要求物品仍由 doer 持有。
`autoequipped` 要求 active item 为空。
`pos.walkable_platform` 必须仍有效。
`self.validfn` 会参与校验。
master sim 上的 `action.validfn` 也会参与校验。

## `0x32024211` 结构细节 / Success/fail 回调 / `AddSuccessAction` 与 `AddFailAction` / 需要核对的字段

`Succeed()` 遍历 `onsuccess` 后清空 `onsuccess` 和 `onfail`。
`Fail()` 遍历 `onfail` 后同样清空两个表。
动作失败可能既在 `BufferedAction:Do` 内部触发 `Fail()`，也在 `PerformBufferedAction` 失败分支再次处理
`actionfailed` 事件。

## `0x32024311` 结构细节 / 客户端预测 / `PreviewAction` 与 `PerformPreviewBufferedAction` / 需要核对的字段

预测客户端通常经 `PlayerController:DoAction` 进入 `locomotor:PreviewAction`。
`EntityScript:PreviewBufferedAction` 会先特殊处理 `ACTIONS.WALKTO`。
`bufferedaction.options.instant` 或 `action.instant` 会直接进入 `PerformPreviewBufferedAction`。
有 SG 时，预览会先尝试 `sg:PreviewAction(bufferedaction)`。
没有可用 handler 且不是 instant 时，`StateGraphInstance:PreviewAction` 会进入 `previewaction` 状态。
这里的 `previewaction` 是 SG 状态名，不是实体事件名。
`EntityScript:PerformPreviewBufferedAction` 会调用 `playercontroller:RemoteBufferedAction` 并标记
`ispreviewing`。
`RemoteBufferedAction` 执行的是 `buffaction.preview_cb()`。
`StateGraphInstance:StartAction` 在预测场景中还可能快进动画帧。

## `0x32025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "function EntityScript:PushBufferedAction|function EntityScript:PerformBufferedAction" \
  scripts/entityscript.lua
rg -n "function EntityScript:GetBufferedAction" \
  scripts/entityscript.lua
rg -n "function BufferedAction:Do|function BufferedAction:IsValid|AddFailAction|AddSuccessAction|Succeed|Fail" \
  scripts/bufferedaction.lua
rg -n "function StateGraphInstance:StartAction|actionhandlers|PerformPreviewBufferedAction" \
  scripts/stategraph.lua scripts/entityscript.lua
rg -n "PreviewBufferedAction|RemoteBufferedAction|PreviewAction" \
  scripts/entityscript.lua scripts/stategraph.lua scripts/components/playercontroller.lua
rg -n "ActionHandler\\(ACTIONS\\.(CHOP|ATTACK|DEPLOY|PICKUP|WALKTO)" \
  scripts/stategraphs/SGwilson.lua scripts/stategraphs/SGwilson_client.lua
~~~

### `0x32025111` 推荐顺序 / 最小闭环

先从 `PushBufferedAction` 判断分支。
再读 `StartAction` 看 SG 是否接管。
最后读 `PerformBufferedAction` 和 `BufferedAction:Do`，确认哪个 `ACTIONS.*.fn` 真正产生副作用。
