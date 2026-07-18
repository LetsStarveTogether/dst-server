# `0x60000000` 玩法系统

本区按玩家可感知的运行链路组织玩法系统。
阅读顺序从角色实体进入，再追战斗、生存、制作、船、活动和追随关系。

目录级语义由本 README 承载，独立专题文件只解释具体运行链路。

## `0x60001111` 区域定位 / 阅读问题 / 运行时入口 / 验证点

玩法系统不是 prefab 名称清单。
一个完整行为通常跨 `Prefab`、component、action、StateGraph、Brain、world component 和 UI/replica。
先找权威 server 组件，再回看 client replica 和 UI 输入。

## `0x60002000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/prefabs/player_common.lua` | `MakePlayerCharacter` | 玩家角色装配 |
| `scripts/components/combat.lua` | `Combat` | 目标、攻击和受击 |
| `scripts/components/health.lua` | `Health` | 伤害与生命值 |
| `scripts/components/builder.lua` | `Builder` | 制作权威执行 |
| `scripts/components/inventory.lua` | `Inventory` | 物品容器与给予 |
| `scripts/prefabs/boat.lua` | `walkableplatform` / `boatphysics` | 船体平台与物理 |
| `scripts/constants.lua` | `SPECIAL_EVENTS` | 活动开关 |
| `scripts/components/leader.lua` | `AddFollower` | 追随关系 |

### `0x60002111` 入口选择 / `scripts/prefabs/player_common.lua` / 搜索信号

用 `MakePlayerCharacter` 判断玩家实体具备哪些组件。
再从 action 或 component 进入具体玩法专题。
如果行为涉及 UI 或预测，继续追 `replica`、`playercontroller` 和 RPC。

## `0x60003000` 运行关系图

~~~mermaid
flowchart TD
    A["player_common.lua: MakePlayerCharacter"]
    A --> B["components: combat / builder / inventory"]
    B --> C["actions.lua / bufferedaction.lua"]
    C --> D["StateGraph: SGwilson"]
    D --> E["Prefab side effects"]
    B --> F["world components"]
    F --> G["events / managers / spawners"]
    B --> H["replica / playercontroller / RPC"]
    H --> B
~~~

### `0x60003111` 导航原则 / 专题入口 / 本区不放穷举清单

本区索引只放运行关系和源码入口。
完整 Prefab、Component、StateGraph、Brain、Widget、Screen 目录统一进入 `0x8000-reference`。

## `0x60004111` 目录索引 / README 载体 / 二级目录 / 链接校验

以下入口先进入目录 README，再进入具体专题文件。

- [角色与生物](0x6100-characters-creatures/README.md)
- [角色与技能树](0x6100-characters-creatures/0x6101-characters-skilltrees.md)
- [生物与 Boss](0x6100-characters-creatures/0x6102-creatures-bosses.md)
- [生存与战斗](0x6200-survival-combat/README.md)
- [Combat 与 Damage](0x6200-survival-combat/0x6201-combat-damage.md)
- [生存食物与农场](0x6200-survival-combat/0x6202-survival-food-farming.md)
- [物品制作与事件](0x6300-items-crafting-events/README.md)
- [Items 与 Crafting](0x6300-items-crafting-events/0x6301-items-crafting.md)
- [Ocean 与 Boating](0x6300-items-crafting-events/0x6302-ocean-boating.md)
- [Events 与 Factions](0x6300-items-crafting-events/0x6303-events-factions.md)

## `0x60005100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "MakePlayerCharacter|AddComponent\\(\"combat\"\\)|AddComponent\\(\"builder\"\\)" \
  scripts/prefabs/player_common.lua

rg -n "ACTIONS.ATTACK|ACTIONS.BUILD|DoBuild|Combat:DoAttack|Health:DoDelta" \
  scripts/actions.lua \
  scripts/components/builder.lua \
  scripts/components/combat.lua \
  scripts/components/health.lua

rg -n "Recipe2|boatphysics|SPECIAL_EVENTS|AddFollower|SetLeader" \
  scripts/recipes.lua \
  scripts/prefabs/boat.lua \
  scripts/constants.lua \
  scripts/components/leader.lua \
  scripts/components/follower.lua
~~~

### `0x60005111` 最小闭环 / 抽样动作

抽样一条战斗链路：`ACTIONS.ATTACK -> Combat:DoAttack -> Health:DoDelta`。
再抽样一条制作链路：`Recipe2 -> Builder:MakeRecipe -> ACTIONS.BUILD -> Builder:DoBuild`。
最后抽样一条事件链路：`SPECIAL_EVENTS -> IsSpecialEventActive -> world manager -> prefab behavior`。
