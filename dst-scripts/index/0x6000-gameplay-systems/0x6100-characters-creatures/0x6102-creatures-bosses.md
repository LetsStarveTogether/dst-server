# `0x61020000` 生物与 Boss

生物与 Boss 都是 prefab 装配出来的实体。
差别主要在组件参数、Brain 复杂度、StateGraph、阶段事件和掉落逻辑。

## `0x61021000` 本页定位

本页用 `rabbit.lua` 和 `deerclops.lua` 对比普通生物与 Boss。
目标是建立一套可复用的源码检查模板。

### `0x61021100` 要回答的运行时问题

普通生物如何组合移动、生命、掉落和 AI。
Boss 如何在相同组件基础上增加目标选择、阶段、特殊攻击和变体。

#### `0x61021110` 源码阅读目标

确认 prefab 是组件和事件的装配层。
确认行为决策在 Brain，动画状态在 StateGraph，数值变化在组件。

##### `0x61021111` 验证点

`rabbit.lua` 使用 `rabbitbrain` 与 `SGrabbit`。
`deerclops.lua` 使用 `deerclopsbrain` 与 `SGdeerclops`。
两者都通过 `health`、`combat`、`lootdropper` 等组件把实体接入通用系统。

## `0x61022000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/prefabs/rabbit.lua` | `Prefab("rabbit", fn, ...)` | 普通生物装配样例 |
| `scripts/prefabs/deerclops.lua` | `Prefab("deerclops", normalfn, ...)` | Boss 装配样例 |
| `scripts/prefabs/critters.lua` | `MakeCritter` | 宠物类生物装配样例 |
| `scripts/stategraphs/SGcritter_common.lua` | `SGCritterStates` | 宠物通用状态工厂 |
| `scripts/stategraphs/SGcritter_eets.lua` | `StateGraph("SGcritter_eets", ...)` | Eets 专用宠物状态图 |
| `scripts/components/combat.lua` | `Combat:SetRetargetFunction` | 目标选择与受击处理 |
| `scripts/components/health.lua` | `Health:SetMaxHealth` | 生命上限、死亡事件 |
| `scripts/brains/deerclopsbrain.lua` | `Brain` | Boss 行为树 |
| `scripts/standardcomponents.lua` | `MakeCharacterPhysics` | 通用物理、可燃、可冻结 helper |

### `0x61022110` 主锚点 / `scripts/prefabs/rabbit.lua`

`rabbit.lua` 先设置物理、tags、locomotor、StateGraph 和 Brain。
随后添加 `eater`、`inventoryitem`、`health`、`lootdropper` 和可选 `combat`。

#### `0x61022111` 搜索信号

搜索 `SetStateGraph("SGrabbit")`、`SetBrain(brain)`、`SetMaxHealth`、`LootSetupFunction` 和
`AddComponent("combat")`。

### `0x61022120` 主锚点 / `scripts/prefabs/deerclops.lua`

`deerclops.lua` 在基础组件外增加 retarget、keep target、sanity aura、timer、knownlocations、变体和阶段逻辑。
普通形态与 mutated 形态分别设置生命、伤害、攻击距离和掉落表。

#### `0x61022121` 搜索信号

搜索 `RetargetFn`、`KeepTargetFn`、`SetStateGraph("SGdeerclops")`、`SetBrain(brain)`、`normalfn` 和
`mutatedfn`。

### `0x61022130` 主锚点 / `scripts/components/combat.lua`

生物是否能攻击、攻击谁、受击后是否掉目标，都要回到 `combat` 组件核对。
Boss 常通过 prefab 设置 `SetRetargetFunction`、`SetKeepTargetFunction` 和 `SetRange`。

#### `0x61022131` 搜索信号

搜索 `SetRetargetFunction`、`SetKeepTargetFunction`、`CanTarget`、`SuggestTarget` 和 `GetAttacked`。

### `0x61022140` 主锚点 / `scripts/standardcomponents.lua`

很多 prefab 通过标准 helper 复用物理、可燃、可冻结和腐败逻辑。
读到 `MakeCharacterPhysics`、`MakeMediumBurnableCharacter` 或 `MakeLargeFreezableCharacter` 时要跳到这里确认副作用。

#### `0x61022141` 搜索信号

搜索 `MakeCharacterPhysics`、`MakeGiantCharacterPhysics`、`Make.*BurnableCharacter` 和
`Make.*FreezableCharacter`。

## `0x61023000` 运行流程

~~~mermaid
flowchart TD
    A["creature prefab"]
    A --> B["基础 tags 和 physics"]
    B --> C["locomotor"]
    C --> D["StateGraph"]
    C --> E["Brain"]
    D --> F["动画事件和状态标签"]
    E --> G["目标选择和行为节点"]
    G --> H["combat"]
    H --> I["health"]
    I --> J["death 与 lootdropper"]
~~~

### `0x61023110` 普通生物链路 / `rabbit` 的最小装配

`rabbit` 的复杂度主要来自普通组件组合。
它有 `locomotor`、`SGrabbit`、`rabbitbrain`、`eater`、`health`、`lootdropper` 和可选 `combat`。

#### `0x61023111` 边界条件

不要默认所有普通生物都有 combat。
`rabbit.lua` 中 `combat` 是在非 cave context 条件下添加。

### `0x61023210` Boss 链路 / `deerclops` 的扩展点

`deerclops` 明确设置目标函数和保持目标函数。
它把 `health`、`combat`、`lootdropper`、`knownlocations`、`timer` 与 Brain/SG 连接起来。

#### `0x61023211` 验证点

`normalfn` 设置 `TUNING.DEERCLOPS_HEALTH` 和 `TUNING.DEERCLOPS_DAMAGE`。
mutated 版本设置 `TUNING.MUTATED_DEERCLOPS_HEALTH` 和 planar 相关组件。

### `0x61023220` 宠物链路 / `critter_eets` 行为

`MakeCritter` 始终加载 emotes 和 traits 资源，并始终添加 `sleeper` 和 `crittertraits`。
`critter_eets` 走通用宠物链路，不使用 `no_pet`、`no_emotes`、`no_traits`、`no_sleep` 或 `no_crafty_emote` 特判退出。
`SGcritter_common.lua` 的 `oneat` 会把 food 传入 `eat` 状态，`AddEat` 可以用 food 决定后续 queue state。
`SGcritter_eets.lua` 包含 pepper、onion、mushroom、cute、combat、hungry、nuzzle、pet 和 sleep/wake 相关表现。

## `0x61024110` 结构细节 / Prefab 是装配层 / 组件、Brain 与 SG 的分工

prefab 负责把组件挂到实体上，并设置初始参数。
Brain 负责持续选择行为。
StateGraph 负责状态、动画窗口和事件反应。
组件负责真实状态变化。

### `0x61024111` 需要核对的字段

读生物时固定检查 `SetStateGraph`、`SetBrain`、`SetMaxHealth`、`SetDefaultDamage`、`SetRange`、
`SetAttackPeriod` 和 `SetLootSetupFn`。
如果 prefab 调用标准 helper，还要跳到 `standardcomponents.lua` 核对物理、燃烧、冻结和腐败副作用。

## `0x61024210` 结构细节 / 变体与阶段 / Boss 变体不是简单换皮

`deerclops.lua` 同时返回普通和 mutated prefab。
mutated 形态不仅改资源，还改生命、伤害、攻击范围、掉落设置和 planar 组件。

### `0x61024211` 边界条件

只读普通 prefab 会漏掉变体调参。
检查 Boss 时要搜索同一文件内所有 `Prefab(...)` 返回项。

## `0x61025100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "SetStateGraph|SetBrain|SetMaxHealth|lootdropper|AddComponent\\(\"combat\"\\)" \
  scripts/prefabs/rabbit.lua \
  scripts/prefabs/deerclops.lua

rg -n "SetRetargetFunction|SetKeepTargetFunction|GetAttacked|CanTarget|SuggestTarget" \
  scripts/components/combat.lua

rg -n "SetMaxHealth|SetVal|DoDelta|death" \
  scripts/components/health.lua

rg -n "MakeCharacterPhysics|Make.*BurnableCharacter|Make.*FreezableCharacter" \
  scripts/standardcomponents.lua

rg -n "MakeCritter|critter_eets|SGCritterStates\\.AddEat|QueueStateAfterEat|OnTraitChanged|AddSleepExStates" \
  scripts/prefabs/critters.lua \
  scripts/stategraphs/SGcritter_common.lua \
  scripts/stategraphs/SGcritter_eets.lua
~~~

### `0x61025110` 推荐顺序

先用 `rabbit.lua` 建立普通装配模板。
再读 `deerclops.lua`，把目标函数、阶段逻辑和变体逐项标出。

#### `0x61025111` 最小闭环

从 `deerclops.lua` 的 `RetargetFn` 开始。
追到 `combat:SetTarget`、`combat:DoAttack`、目标 `combat:GetAttacked`、`health:DoDelta` 和掉落逻辑。
