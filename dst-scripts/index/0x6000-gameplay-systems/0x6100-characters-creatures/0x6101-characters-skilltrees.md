# `0x61010000` 角色与技能树

角色不是单个 prefab 的孤立定义。
运行时要从 `prefabs/player_common.lua` 的通用装配读起，再回到具体角色文件和技能树数据。

## `0x61011000` 本页定位

本页解释玩家角色如何由通用骨架、角色差异、技能树和玩家控制组件合成。
重点是区分 pristine 网络实体、客户端预测、mastersim 权威组件和技能激活回调。

### `0x61011100` 要回答的运行时问题

一个角色文件为什么通常只写 `common_postinit` 和 `master_postinit`。
技能树为什么先是数据，再通过 `skilltreeupdater` 落到组件或事件。

#### `0x61011110` 源码阅读目标

读懂 `MakePlayerCharacter` 返回的 prefab 工厂。
确认 `wilson.lua` 只传入角色差异，而不是重新实现玩家实体。

##### `0x61011111` 验证点

`player_common.lua` 在 pristine 阶段加网络变量和优化标签。
`TheWorld.ismastersim` 之后才添加权威 `combat`、`health`、`hunger`、`sanity` 和 `eater`。
`skilltree_defs.lua` 通过 `BuildAllData` 动态加载 `prefabs/skilltree_*.lua`。

## `0x61012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/prefabs/player_common.lua` | `MakePlayerCharacter` | 生成玩家 prefab 工厂 |
| `scripts/prefabs/wilson.lua` | `MakePlayerCharacter("wilson", ...)` | 角色差异样例 |
| `scripts/prefabs/skilltree_defs.lua` | `BuildAllData` | 汇总角色技能树定义 |
| `scripts/components/skilltreeupdater.lua` | `SkillTreeUpdater:ActivateSkill` | 应用技能激活和网络同步 |
| `scripts/components/playercontroller.lua` | `PlayerController:AttachClassified` | 玩家控制与 classified 数据连接 |

### `0x61012110` 主锚点 / `scripts/prefabs/player_common.lua`

这里是玩家实体的真实主体。
`fn` 先创建网络实体，再在非 mastersim 直接返回客户端实体。
mastersim 分支继续生成 `player_classified`，添加权威组件，并设置 `SGwilson`。

#### `0x61012111` 搜索信号

搜索 `MakePlayerCharacter`、`entity:SetPristine`、`TheWorld.ismastersim`、`player_classified` 和
`SetStateGraph("SGwilson")`。

### `0x61012120` 主锚点 / `scripts/prefabs/wilson.lua`

这个文件展示角色差异的标准形状。
`common_postinit` 负责 tags、reticule、客户端可见行为。
`master_postinit` 负责 starting inventory、beard、foodaffinity 和服务端事件。

#### `0x61012121` 搜索信号

搜索 `common_postinit`、`master_postinit`、`skilltreeupdater:IsActivated` 和
`MakePlayerCharacter("wilson"`。

### `0x61012130` 主锚点 / `scripts/prefabs/skilltree_defs.lua`

`BuildAllData` 遍历 `SKILLTREE_CHARACTERS`。
它按角色名拼出 `require("prefabs/skilltree_" .. character)`，再写入 `SKILLTREE_DEFS`、`SKILLTREE_ORDERS`
和 `CUSTOM_FUNCTIONS`。

#### `0x61012131` 搜索信号

搜索 `SKILLTREE_CHARACTERS`、`BuildAllData`、`CreateSkillTreeFor` 和 `DEBUG_REBUILD`。

### `0x61012140` 主锚点 / `scripts/components/skilltreeupdater.lua`

`SkillTreeUpdater` 是技能数据进入角色运行时的组件。
`ActivateSkill` 会区分 client、server 和 RPC 来源，然后调用 `ActivateSkill_Server` 中定义的 `onactivate`。

#### `0x61012141` 搜索信号

搜索 `ActivateSkill`、`ActivateSkill_Server`、`DeactivateSkill_Server`、`OnSave` 和 `SendFromSkillTreeBlob`。

## `0x61013000` 运行流程

~~~mermaid
flowchart TD
    A["角色 prefab 文件"]
    A --> B["require prefabs/player_common"]
    B --> C["MakePlayerCharacter(name, prefabs, assets, hooks)"]
    C --> D["CreateEntity 和 pristine 网络状态"]
    D --> E{"TheWorld.ismastersim"}
    E -->|false| F["客户端实体与 playercontroller"]
    E -->|true| G["player_classified 和权威组件"]
    G --> H["common_postinit 与 master_postinit"]
    H --> I["skilltreeupdater 应用技能"]
    I --> J["SGwilson 和玩家组件消费结果"]
~~~

### `0x61013110` 通用装配阶段 / Pristine 前后的职责

pristine 前后要分开读。
pristine 前设置网络实体、基础 tags、net vars 和客户端可见函数。
pristine 后的 mastersim 分支才添加会改变世界状态的组件。

#### `0x61013111` 边界条件

不要把客户端预测组件当成权威状态。
例如 `playercontroller` 读写 `player_classified`，但生命、饥饿和战斗仍以 server 组件为准。

### `0x61013210` 角色差异阶段 / Common 与 Master Hook

`common_postinit` 适合 tags、reticule、网络可见动作提示。
`master_postinit` 适合 inventory、beard、listener、食物亲和和组件参数。

#### `0x61013211` 验证点

在 `wilson.lua` 中能看到 `reticule` 出现在 common hook。
`beard` 和 `foodaffinity` 出现在 master hook。

### `0x61013310` 技能应用阶段 / 数据到组件

`skilltree_defs.lua` 只汇总技能数据。
真正激活由 `skilltreeupdater.lua` 处理，并且需要考虑 RPC 与存档恢复。

#### `0x61013311` 边界条件

不要只读 `skilltree_wilson.lua`。
必须回到 `skilltreeupdater:IsActivated` 的调用点，确认技能是否在组件、动作或 prefab hook 中被消费。

## `0x61014110` 结构细节 / 玩家实体的核心组件 / `player_common.lua` 中的权威组件簇

mastersim 分支添加 `locomotor`、`combat`、`inventory`、`health`、`hunger`、`sanity`、`builder` 和
`eater`。
这些组件构成玩家在世界中的基础能力。

### `0x61014111` 需要核对的字段

`locomotor` 必须在 `SetStateGraph("SGwilson")` 之前构造。
`combat` 设置 `TUNING.UNARMED_DAMAGE`、`TUNING.WILSON_ATTACK_PERIOD` 和默认攻击距离。
`hunger` 设置最大值、消耗速率和饥饿伤害速率。

## `0x61014210` 结构细节 / 技能树的运行边界 / `skilltreeupdater` 的同步语义

`ActivateSkill` 先更新 `skilltreedata`。
server 侧触发 `onactivate` 并向 client 同步。
client 侧在本地玩家上也可能执行激活逻辑以保持前端状态。

### `0x61014211` 验证点

检查技能效果时要确认它是 `onactivate` 直接改组件，还是通过后续 `IsActivated` 查询改变分支。

## `0x61015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "MakePlayerCharacter|entity:SetPristine|TheWorld\\.ismastersim|player_classified|SetStateGraph" \
  scripts/prefabs/player_common.lua

rg -n "common_postinit|master_postinit|skilltreeupdater:IsActivated|MakePlayerCharacter" \
  scripts/prefabs/wilson.lua

rg -n "BuildAllData|SKILLTREE_CHARACTERS|CreateSkillTreeFor|ActivateSkill|SendFromSkillTreeBlob" \
  scripts/prefabs/skilltree_defs.lua \
  scripts/components/skilltreeupdater.lua
~~~

### `0x61015110` 推荐顺序

先读 `player_common.lua` 的 prefab 工厂。
再读一个具体角色，例如 `wilson.lua`。
最后用技能名回查 `skilltree_defs.lua`、角色技能树文件和 `skilltreeupdater.lua`。

#### `0x61015111` 最小闭环

用 `wilson_torch_7` 做样例。
它在 `wilson.lua` 中通过 `skilltreeupdater:IsActivated` 改变右键特殊动作。
这条链路能同时验证角色 hook、技能数据和玩家动作系统。
