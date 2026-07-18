# `0x30000000` 实体与动作

本区从 `EntityScript` 进入运行时对象，再追到组件、事件、动作、Prefab 装配和网络副本。
专题页解释运行关系，穷举目录留在 `0x8000-reference`。

目录级语义由本 README 承载，独立专题文件只解释具体运行链路。

## `0x30001111` 区域定位 / 阅读问题 / 运行时入口 / 验证点

读者要能回答两个问题。
一个实体如何被创建、挂载组件、订阅事件并参与存档。
一次玩家动作如何穿过动作收集、`BufferedAction`、StateGraph，最后落到组件副作用。

## `0x30002000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/mainfunctions.lua` | `CreateEntity` | 创建 `EntityScript` 并登记到 `Ents` |
| `scripts/entityscript.lua` | `AddComponent` | 装载组件、复制组件、注册动作贡献 |
| `scripts/entityscript.lua` | `PushEvent_Internal` | 事件监听器、SG、Brain 的分发边界 |
| `scripts/components/playercontroller.lua` | `PlayerController:DoAction` | 玩家输入到动作提交 |
| `scripts/components/playeractionpicker.lua` | `GetLeftClickActions` | 鼠标动作候选选择 |
| `scripts/componentactions.lua` | `EntityScript:CollectActions` | 从组件收集可执行动作 |
| `scripts/bufferedaction.lua` | `BufferedAction` | 保存动作执行上下文 |

### `0x30002111` 入口选择 / `scripts/entityscript.lua` / 搜索信号

用 `CreateEntity` 确认实体入口。
用 `AddComponent` 确认组件挂载。
用 `CollectActions` 确认动作链路开始进入组件能力。

## `0x30003000` 运行关系图

~~~mermaid
flowchart TD
    A["CreateEntity"]
    A --> B["EntityScript"]
    B --> C["AddComponent"]
    C --> D["Component state"]
    B --> E["ListenForEvent / WatchWorldState"]
    E --> F["PushEvent_Internal"]
    F --> G["SG or Brain handler"]
    D --> H["PlayerActionPicker"]
    H --> I["CollectActions"]
    I --> J["BufferedAction"]
    J --> K["StateGraph action handler"]
    K --> L["Component side effect"]
~~~

### `0x30003111` 导航原则 / 专题入口 / 本区不放穷举清单

本区索引只放运行关系和源码入口。
实体、组件、Prefab、SG、Brain 的完整目录统一进入 `0x8000-reference`。

## `0x30004111` 目录索引 / README 载体 / 二级目录 / 链接校验

以下入口先进入目录 README，再进入具体专题文件。

- [实体模型](0x3100-entity-model/README.md)
- [EntityScript](0x3100-entity-model/0x3101-entityscript.md)
- [组件生命周期](0x3100-entity-model/0x3102-component-lifecycle.md)
- [标签与事件](0x3100-entity-model/0x3103-tags-events.md)
- [动作管线](0x3200-action-pipeline/README.md)
- [ComponentActions](0x3200-action-pipeline/0x3201-component-actions.md)
- [BufferedAction](0x3200-action-pipeline/0x3202-buffered-actions.md)
- [Player Input 到 Action](0x3200-action-pipeline/0x3203-player-input-action.md)
- [Prefab 装配](0x3300-prefab-assembly/README.md)
- [Prefab 装配契约](0x3300-prefab-assembly/0x3301-prefab-contract.md)
- [Replica 与 Classified](0x3300-prefab-assembly/0x3302-replica-classified.md)

## `0x30005100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "EntityScript|PlayerActionPicker|CollectActions|BufferedAction|PushBufferedAction" \
  scripts/mainfunctions.lua \
  scripts/entityscript.lua \
  scripts/components/playercontroller.lua \
  scripts/components/playeractionpicker.lua \
  scripts/componentactions.lua \
  scripts/bufferedaction.lua
~~~

### `0x30005111` 最小闭环 / 抽样动作

先抽样 `CreateEntity` 到 `AddComponent`。
再抽样 `input.lua`、`PlayerController`、`PlayerActionPicker` 到 `BufferedAction`。
最后确认 SG 或组件里真正改变状态的位置。
