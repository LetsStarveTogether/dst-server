# `0x31020000` 组件生命周期

组件生命周期不是一条固定线性流程。
`AddComponent`、更新注册、存档恢复、组件移除和实体移除是几组相互独立但共享 `EntityScript` 的入口。

## `0x31021111` 本页定位 / 要回答的运行时问题 / 源码阅读目标 / 验证点

目标是看懂组件如何被装载、如何注册不同更新通道、如何参与存档、如何清理监听，以及 `RemoveComponent` 和实体 `Remove` 的差异。

## `0x31022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/entityscript.lua` | `AddComponent` | 组件挂载 |
| `scripts/entityscript.lua` | `StartUpdatingComponent` | 组件更新注册 |
| `scripts/entityscript.lua` | `StartWallUpdatingComponent` | 真实墙钟更新注册 |
| `scripts/entityscript.lua` | `StopUpdatingComponent_Deferred` | 延迟移出更新表 |
| `scripts/entityscript.lua` | `LongUpdate` | 跨时间跳跃更新入口 |
| `scripts/update.lua` | `Update` / `StaticUpdate` / `WallUpdate` / `LongUpdate` | 驱动普通、static、wall 和 long update |
| `scripts/entityscript.lua` | `RemoveComponent` | 移除单个组件 |
| `scripts/entityscript.lua` | `Remove` | 整个实体销毁 |
| `scripts/components/health.lua` | `Health` | 典型状态组件 |
| `scripts/components/inventory.lua` | `Inventory` | 复杂容器组件 |
| `scripts/standardcomponents.lua` | `MakeSmallBurnable` | Prefab 常用组件装配现场 |

### `0x31022111` 主锚点 / `scripts/entityscript.lua` / 搜索信号

先在 `entityscript.lua` 搜索 `AddComponent`。
再用 `health.lua` 看会注册更新的状态组件。
最后用 `inventory.lua` 看会保存子实体 references 的容器组件。
如果要看 Prefab 如何批量套用组件，再回到 `standardcomponents.lua` 的 `Make*` 函数。

## `0x31023000` 运行流程

~~~mermaid
flowchart TD
    A["AddComponent(name)"]
    A --> B["LoadComponent(name)"]
    B --> C["ReplicateComponent(name)"]
    C --> D["cmp(self)"]
    D --> E["ComponentPostInit"]
    E --> F["RegisterComponentActions(name)"]
    D --> G["optional update channel"]
    D --> H["optional OnSave / OnLoad"]
    D --> I["optional ListenForEvent / WatchWorldState"]
    I --> J["RemoveComponent -> OnRemoveFromEntity"]
    I --> K["Entity Remove -> OnRemoveEntity"]
~~~

### `0x31023111` 流程分段 / 入口到副作用 / 边界条件

- `AddComponent` 会先检查 `lower_components_shadow`，避免大小写重复组件。
- `LoadComponent` 会给组件类挂上 `WatchWorldState` 和 `StopWatchingWorldState`。
- `ReplicateComponent` 在组件构造之前执行。
- `RegisterComponentActions` 在 `ComponentPostInit` 之后执行。
- `StartUpdatingComponent` 只在组件主动要求更新时出现，不是所有组件都会走。
- 更新通道不只有普通 `OnUpdate`，还包括 `OnStaticUpdate`、`OnWallUpdate` 和 `LongUpdate`。

## `0x31024111` 结构细节 / 数据结构与生命周期 / 具体 Lua 结构 / 需要核对的字段

- `AddComponent` 通过 `require("components/"..name)` 装载组件类。
- `self.components[name] = loadedcmp` 使用传入的原始组件名作为 key。
- `lower_components_shadow[string.lower(name)]` 只用于重复检测。
- `StartUpdatingComponent` 把实体放进 `NewUpdatingEnts` 或 `NewStaticUpdatingEnts`。
- `StopUpdatingComponent` 先写入全局 `StopUpdatingComponents`，实际清理由 deferred 路径完成。
- `StartWallUpdatingComponent` 把实体放进 wall update 集合，并由 `update.lua` 调用 `OnWallUpdate(dt)`。
- `EntityScript:LongUpdate(dt)` 会遍历组件并调用存在的 `LongUpdate(dt)`。

## `0x31024121` 结构细节 / 数据结构与生命周期 / 更新通道 / 普通、静态、墙钟与长更新

- 普通更新由组件调用 `inst:StartUpdatingComponent(self)` 注册。
- 如果传入 static 标记，暂停期间仍可由 `OnStaticUpdate(0)` 接收静态更新。
- 真实时间相关逻辑使用 `inst:StartWallUpdatingComponent(self)` 和组件 `OnWallUpdate(dt)`。
- 跨天、洞穴迁移或模拟跳跃会走 `LongUpdate(dt)`，不是普通帧更新。
- `StopUpdatingComponent_Deferred` 负责在全局 update 循环里安全移除组件。

## `0x31024131` 结构细节 / 数据结构与生命周期 / 保存与恢复 / `Health` 和 `Inventory`

- `Health:OnSave` 保存生命值、惩罚和可选最大生命值。
- `Health:OnLoad` 会恢复 penalty、invincible 和 health，并触发 HUD 更新路径。
- `Inventory:OnSave` 会对 itemslots、equipslots 和 activeitem 调 `GetSaveRecord`。
- `Inventory:OnLoad` 通过 `SpawnSaveRecord` 重建物品，再放回背包或装备槽。
- `SetPersistData` 先处理 `add_component_if_missing` 和实体 `OnPreLoad`。
- 需要二阶段引用修复的组件再参与 `LoadPostPass(newents, savedata)`。

## `0x31024141` 结构细节 / 数据结构与生命周期 / 事件监听清理 / Source 与 Listener 双向索引

- `ListenForEvent` 会同时写 `source.event_listeners` 和 `listener.event_listening`。
- 组件监听外部 source 时，应在自己的 `OnRemoveFromEntity` 或 `OnRemoveEntity` 中显式 `RemoveEventCallback`。
- `WatchWorldState` 由 `LoadComponent` 注入组件类，但记录仍挂在 `inst.worldstatewatching` 上。
- 实体 `Remove` 会调用 `StopAllWatchingWorldStates` 和 `RemoveAllEventCallbacks` 兜底清理。

## `0x31024151` 结构细节 / 数据结构与生命周期 / 移除差异 / `OnRemoveFromEntity` 与 `OnRemoveEntity`

- `RemoveComponent` 调用组件 `OnRemoveFromEntity`，然后取消 replica 和 action 注册。
- `EntityScript:Remove` 先调用全局 `OnRemoveEntity(self.GUID)`，再 `PushEvent("onremove")`。
- 推送 `onremove` 后，它会清理 watcher、event callback 和 pending task。
- 整体移除时组件和 replica 组件会收到 `OnRemoveEntity`。
- `mainfunctions.lua` 的 `OnRemoveEntity` 负责把实体从 `Ents`、BrainManager、SGManager 和更新表移出。

## `0x31024161` 结构细节 / 数据结构与生命周期 / 标准装配函数 / `standardcomponents.lua`

- `MakeSmallBurnable`、`MakeMediumBurnable` 和 `MakeLargeBurnable` 都通过 `inst:AddComponent("burnable")` 装配组件。
- 这些函数配置组件回调，不改变 `AddComponent` 的核心生命周期。
- `MakeNoGrowInWinter` 展示组件实例调用 `WatchWorldState` 的路径。
- 这类函数适合验证 Prefab 如何组合组件，不适合当成组件基类机制阅读。

## `0x31025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "AddComponent|StartUpdatingComponent|StopUpdatingComponent_Deferred|RemoveComponent" \
  scripts/entityscript.lua \
  scripts/standardcomponents.lua

rg -n "StartWallUpdatingComponent|OnStaticUpdate|OnWallUpdate|LongUpdate" \
  scripts/entityscript.lua \
  scripts/update.lua

rg -n "OnSave|OnLoad|OnRemoveFromEntity|OnRemoveEntity" \
  scripts/entityscript.lua \
  scripts/components/health.lua \
  scripts/components/inventory.lua

rg -n "ListenForEvent|RemoveEventCallback|WatchWorldState|StopAllWatchingWorldStates" \
  scripts/entityscript.lua \
  scripts/components/health.lua \
  scripts/components/inventory.lua
~~~

### `0x31025111` 推荐顺序 / 最小闭环

先追 `Health:DoFireDamage` 到 `StartUpdatingComponent`，再追 `Health:OnUpdate` 到 `StopUpdatingComponent`。
再追 `Inventory:OnSave` 到子实体 `GetSaveRecord`。
然后追一个跨 source 的 `ListenForEvent`。
最后比较 `RemoveComponent` 和 `EntityScript:Remove` 调用的清理钩子。
