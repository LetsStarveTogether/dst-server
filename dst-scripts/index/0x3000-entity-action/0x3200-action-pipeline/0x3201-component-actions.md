# `0x32010000` ComponentActions

ComponentActions 页按“候选动作如何被收集”阅读。
它不从 `ACTIONS.*.fn` 开始，也不把候选收集误写成动作执行。

## `0x32011111` 本页定位 / 要回答的运行时问题 / 候选动作从哪里来 / 验证点

目标是从 `PlayerActionPicker` 的左键、右键、场景、物品、点位和装备分支追到
`EntityScript:CollectActions`。

## `0x32011211` 本页定位 / 与执行链路的边界 / 本页只解释候选生成 / 边界条件

`componentactions.lua` 往 `actions` 数组里追加 `ACTIONS.*` 候选。
真正生成 `BufferedAction` 的位置在 `components/playeractionpicker.lua` 的排序和包装逻辑之后。
真正执行副作用的位置在 `bufferedaction.lua` 和 `actions.lua`。

## `0x32012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/components/playeractionpicker.lua` | `GetLeftClickActions` | 左键候选动作总分派 |
| `scripts/components/playeractionpicker.lua` | `GetRightClickActions` | 右键候选动作总分派 |
| `scripts/components/playeractionpicker.lua` | `GetSceneActions` | `SCENE` action type 收集 |
| `scripts/components/playeractionpicker.lua` | `GetUseItemActions` | `USEITEM` action type 收集 |
| `scripts/components/playeractionpicker.lua` | `GetPointActions` | `POINT` action type 收集 |
| `scripts/components/playeractionpicker.lua` | `GetEquippedItemActions` | `EQUIPPED` action type 收集 |
| `scripts/components/playeractionpicker.lua` | `GetInventoryActions` | `INVENTORY` action type 收集 |
| `scripts/componentactions.lua` | `COMPONENT_ACTIONS` | 原生组件动作注册表 |
| `scripts/componentactions.lua` | `AddComponentAction` | mod 组件动作注册入口 |
| `scripts/componentactions.lua` | `EntityScript:CollectActions` | 按 action type 和组件列表调用 collector |
| `scripts/componentactions.lua` | `EntityScript:IsActionValid` | 用 `ISVALID` 表做 action 可见性验证 |
| `scripts/actions.lua` | `ACTIONS` | 候选项引用的动作定义表 |

### `0x32012111` 主锚点 / `scripts/components/playeractionpicker.lua` / 搜索信号

先找 `GetLeftClickActions` 和 `GetRightClickActions`。
这两个函数决定当前鼠标状态应该进入 `SCENE`、`USEITEM`、`POINT`、`EQUIPPED` 还是 `INVENTORY`。

### `0x32012121` 主锚点 / `scripts/componentactions.lua` / 搜索信号

再找 `COMPONENT_ACTIONS` 和 `EntityScript:CollectActions`。
如果一个 action 来自 mod，还要一起核对 `AddComponentAction`、`modactioncomponents` 和
`CheckModComponentActions`。

## `0x32013000` 运行流程

~~~mermaid
flowchart TD
    A["PlayerController 更新鼠标目标"]
    A --> B["PlayerActionPicker:DoGetMouseActions"]
    B --> C{"左键或右键"}
    C --> D["GetLeftClickActions"]
    C --> E["GetRightClickActions"]
    D --> F{"选择 action type"}
    E --> F
    F --> G["非组件候选来源"]
    F --> H["SCENE / USEITEM / POINT / EQUIPPED / INVENTORY"]
    H --> I["EntityScript:CollectActions"]
    I --> J["COMPONENT_ACTIONS[actiontype][component]"]
    J --> K["collector 向 actions 追加 ACTIONS.*"]
    G --> L["override / steering / cannon / inherent / point special"]
    K --> M["SortActionList 包装为 BufferedAction 候选"]
    L --> M
~~~

### `0x32013111` 鼠标候选分段 / `DoGetMouseActions` / 验证点

`DoGetMouseActions` 会处理 HUD 遮挡、AOE reticule、视线过滤和鼠标目标。
它返回的左键和右键结果会被 `PlayerController` 缓存在 `LMBaction` 和 `RMBaction`。

### `0x32013121` 鼠标候选分段 / `GetLeftClickActions` / 验证点

左键会混合多种候选来源。
它包含 steering、boat cannon、active item、强制检查、强制攻击、装备动作、场景动作和点位动作。
当地面可走且没有实体目标时，它还会尝试装备物品的 `POINT` 动作和 `pointspecialactionsfn`。

### `0x32013131` 鼠标候选分段 / `GetRightClickActions` / 验证点

右键先处理 `disable_right_click` 和 `rightclickoverride`。
随后才进入 steering、boat cannon、容器 widget、active item、装备右键动作和场景右键动作。
最后再处理 walkable peripheral 和 AOE/reticule 点位动作。
右键并不等价于单独的 action type，而是给 collector 传入 `right = true`。

### `0x32013211` 组件收集分段 / `EntityScript:CollectActions` / 验证点

`CollectActions(actiontype, ...)` 先取 `COMPONENT_ACTIONS[actiontype]`。
然后遍历实体的 `self.actioncomponents`，用 `ACTION_COMPONENT_NAMES` 还原组件名并调用 collector。
mod 分支会遍历 `self.modactioncomponents`，再从 `MOD_COMPONENT_ACTIONS` 找同名 action type。

### `0x32013221` 组件收集分段 / 非组件候选来源 / 验证点

并非所有候选都来自 `COMPONENT_ACTIONS`。
`PlayerActionPicker` 会直接加入 steering、boat cannon、强制 `LOOKAT`、强制 `ATTACK`、`DROP` 和 `WALKTO` fallback。
它也会直接加入 `inherentsceneaction`、`inherentscenealtaction`、`pointspecialactionsfn` 和 `doubleclickactionsfn`。
因此 `componentactions.lua` 是候选来源之一，不是整个候选系统的唯一入口。

## `0x32014111` 结构细节 / `COMPONENT_ACTIONS` 表 / 原生组件 Collector / 需要核对的字段

表的第一层是 action type。
原生顶层键就是 `SCENE`、`USEITEM`、`POINT`、`EQUIPPED`、`INVENTORY` 和 `ISVALID`。
第二层是组件名，值是 collector 函数。

| action type | collector 签名 | 源码范围 |
| --- | --- | --- |
| `SCENE` | `fn(inst, doer, actions, right)` | `scripts/componentactions.lua` 的 `COMPONENT_ACTIONS.SCENE` |
| `USEITEM` | `fn(inst, doer, target, actions, right)` | `scripts/componentactions.lua` 的 `COMPONENT_ACTIONS.USEITEM` |
| `POINT` | `fn(inst, doer, pos, actions, right, target)` | `scripts/componentactions.lua` 的 `COMPONENT_ACTIONS.POINT` |
| `EQUIPPED` | `fn(inst, doer, target, actions, right)` | `scripts/componentactions.lua` 的 `COMPONENT_ACTIONS.EQUIPPED` |
| `INVENTORY` | `fn(inst, doer, actions, right)` | `scripts/componentactions.lua` 的 `COMPONENT_ACTIONS.INVENTORY` |
| `ISVALID` | `fn(inst, action, right)` | `scripts/componentactions.lua` 的 `COMPONENT_ACTIONS.ISVALID` |

## `0x32014211` 结构细节 / `ACTION_COMPONENT_NAMES` 与实体组件 / 组件名映射 / 需要核对的字段

实体保存的是 action component id。
`CollectActions` 用 `ACTION_COMPONENT_NAMES[v]` 找 collector。
这也是为什么读一个 prefab 的动作时，要同时看组件是否被加进 action component 集合。
组件集合的真实写入点是 `EntityScript:AddComponent(name)` 调用 `RegisterComponentActions(name)`。
删除组件时，`EntityScript:RemoveComponent(name)` 调用 `UnregisterComponentActions(name)`。
网络实体还会通过 `actionreplica.actioncomponents` 和 `actionreplica.modactioncomponents` 同步这些 id。

## `0x32014311` 结构细节 / `ISVALID` 表 / 候选可见性验证 / 需要核对的字段

`EntityScript:IsActionValid(action, right)` 只在 `action.rmb` 为 truthy 时强制要求当前输入也是右键。
`Action` 构造会把 `data.rmb = false` 归一成 `nil`。
然后遍历 `COMPONENT_ACTIONS.ISVALID`。
原生 validator 或 mod validator 至少要有一个返回 `true`，否则默认结果是 `false`。
它回答的是“这个 action 是否可作为候选”，不是 `BufferedAction:IsValid()` 那种执行前检查。

## `0x32014411` 结构细节 / `SortActionList` / 排序、过滤与包装 / 需要核对的字段

`PlayerActionPicker` 在 collector 追加完 `ACTIONS.*` 后，还会调用 `SortActionList`。
它先按 `Action.priority` 降序排序。
随后通过 `actionfilterstack` 和 `actionfilter` 过滤候选。
最后它才把 `ACTIONS.*` 包装为 `BufferedAction`。

包装时目标类型不同，生成的动作上下文也不同。
目标是实体时，`BufferedAction` 会携带 target。
目标是 `Vector3` 或点位时，`BufferedAction` 会携带 pos。
没有实体也没有点位时，它仍可能生成只带 doer 和 action 的候选。
`CASTAOE` 这类范围动作还会在包装时写入距离约束。

## `0x32014511` 结构细节 / Mod 注册与网络同步 / `AddComponentAction` / 需要核对的字段

`AddComponentAction` 会把 collector 写入 `MOD_COMPONENT_ACTIONS`。
它还维护 `MOD_ACTION_COMPONENT_NAMES` 和 `MOD_ACTION_COMPONENT_IDS`。
实体添加组件时，`RegisterComponentActions(name)` 会把原生或 mod action component id 写到实体上。
实体删除组件时，`UnregisterComponentActions(name)` 会移除这些 id。
网络同步由 `entityscript.lua` 里的 `actionreplica.actioncomponents` 和 `actionreplica.modactioncomponents` 承接。

## `0x32014611` 结构细节 / 客户端请求动作上下文 / `CLIENT_REQUESTED_ACTION` / 边界条件

远端点击到达服务端后，服务端不是直接信任客户端传来的 `BufferedAction`。
`PlayerController:OnRemoteLeftClick` 和 `OnRemoteRightClick` 会先用 action code 和 mod 名设置 `CLIENT_REQUESTED_ACTION`。
随后服务端重新执行 `DoGetMouseActions(position, target)`。
收集完成后再清理请求上下文。

这个上下文会影响部分 collector 的候选选择。
典型例子包括划船失败动作、植物注册表研究动作，以及部署类动作在不同点位目标之间的选择。
因此排查“客户端看到了一个动作，服务端却重建成另一个动作”时，必须同时看 `actions.lua`、`playercontroller.lua` 和 `componentactions.lua`。

## `0x32014711` 结构细节 / Carnival 高尔夫动作 / `GOLF_START_AIMING` 与 `TERRAFORM_REMOVE` / 搜索信号

Carnival 高尔夫提供一个候选动作样例。
`componentactions.lua` 的 `EQUIPPED.golfclub` 分支在右键目标带 `golfable` 且目标未被占用时追加 `GOLF_START_AIMING`。
`PlayerActionPicker` 对已经瞄准的球杆有额外分支，停止瞄准和开始蓄力不只依赖组件 collector。
`USEITEM.terraformer` 对带 `terraformerremoveable` 的目标追加 `TERRAFORM_REMOVE`。
最终执行仍回到 `actions.lua`，再调用 `golfclub:StartAiming`、`golfclub_reticule:StartCharging` 或 `terraformerremoveable:TryToRemove`。

## `0x32014811` 结构细节 / 动作边界 / Pet 与 Open Crafting / 验证点

`COMPONENT_ACTIONS.SCENE.petleash` 只检查目标是否没有 replica container。
`nopet` tag 不参与 `ACTIONS.PET` 候选过滤。
`ACTIONS.OPEN_CRAFTING.fn` 先确认 doer 有 `builder`，再按目标是否带 `hideprototyperaction` 决定是否传入 prototyper。
因此 Carnival 高尔夫 tee 这类临时 crafting 入口要同时看 `componentactions.lua` 的候选和 `actions.lua` 的执行保护。

## `0x32015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "DoGetMouseActions|GetLeftClickActions|GetRightClickActions|GetSceneActions" \
  scripts/components/playeractionpicker.lua
rg -n "GetUseItemActions|GetPointActions|GetEquippedItemActions|GetInventoryActions" \
  scripts/components/playeractionpicker.lua
rg -n "COMPONENT_ACTIONS|AddComponentAction|EntityScript:CollectActions" \
  scripts/componentactions.lua scripts/entityscript.lua
rg -n "EntityScript:IsActionValid|RegisterComponentActions|modactioncomponents|CLIENT_REQUESTED_ACTION" \
  scripts/componentactions.lua scripts/entityscript.lua
rg -n "ACTIONS =|Action\\(|\\.rmb|\\.validfn" scripts/actions.lua
rg -n "SortActionList|actionfilter|BufferedAction\\(" \
  scripts/components/playeractionpicker.lua
rg -n "GOLF_START_AIMING|GOLF_START_CHARGING|TERRAFORM_REMOVE|golfclub|terraformerremoveable" \
  scripts/componentactions.lua \
  scripts/components/playeractionpicker.lua \
  scripts/actions.lua

rg -n "ACTIONS\\.PET|OPEN_CRAFTING|hideprototyperaction|nopet" \
  scripts/componentactions.lua \
  scripts/actions.lua \
  scripts/prefabs/critters.lua
~~~

### `0x32015111` 推荐顺序 / 最小闭环

先用 `PlayerActionPicker` 判断 action type。
再用 `CollectActions` 查组件 collector。
最后只把 `ACTIONS.*` 当作候选定义，不提前跳到 `ACTIONS.*.fn`。
