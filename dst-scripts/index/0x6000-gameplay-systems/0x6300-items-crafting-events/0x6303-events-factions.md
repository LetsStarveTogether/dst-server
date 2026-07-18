# `0x63030000` 事件与阵营

事件与阵营页解释活动开关如何影响资源加载、世界组件、prefab 行为和追随/敌对关系。
这里不把节日文件做成清单。
清单型内容统一进入 `0x8000-reference`。

## `0x63031111` 本页定位 / 要回答的运行时问题 / 活动从哪里进入运行时 / 验证点

活动入口首先是 `scripts/constants.lua` 的 `SPECIAL_EVENTS`、`WORLD_SPECIAL_EVENT`
和 `WORLD_EXTRA_EVENTS`。
`ApplySpecialEvent` 与 `ApplyExtraEvent` 写入活动状态。
`mainfunctions.lua` 和 `gamelogic.lua` 再根据这些状态加载 global、backend、frontend event prefabs。
世界与 prefab 侧通过 `IsSpecialEventActive` 决定是否启用具体玩法。

## `0x63031211` 本页定位 / 阵营不是单一系统 / Tags、leader、follower 与 Combat Target / 边界条件

DST Lua 里没有一个统一的 faction 表覆盖所有敌友关系。
常见关系由 tags、`leader`、`follower`、`combat:SetTarget`、`Combat:TryRetarget`
以及具体 brain/prefab 的 retarget 函数共同决定。
因此“阵营”页要按实际调用点核对，不能只搜索 `faction` 字符串。

## `0x63032000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/constants.lua` | `SPECIAL_EVENTS` | 活动常量与开关 |
| `scripts/simutil.lua` | `ApplySpecialEvent` | 写入活动状态 |
| `scripts/mainfunctions.lua` | `GlobalInit` | global event prefab 加载 |
| `scripts/gamelogic.lua` | `LoadPrefabs` | backend/frontend event prefab 加载 |
| `scripts/prefabs/event_deps.lua` | `SPECIAL_EVENT_DEPS` | 活动资源依赖 |
| `scripts/prefabs/forest.lua` | `AddComponent("carnivalevent")` | 世界活动组件装配 |
| `scripts/components/carnivalevent.lua` | `SpawnCarnivalHost` | Carnival 世界组件样例 |
| `scripts/prefabs/carnival_plaza.lua` | `RegisterPlaza` | 活动 prefab 与世界组件连接 |
| `scripts/prefabs/carnivalgame_golfgame.lua` | `AddGolfProp` / `SaveCourseData` | Carnival 高尔夫球场装配与持久化 |
| `scripts/prefabs/carnivalgame_golfprops.lua` | `spring_*` / `movingwall_*` | 高尔夫场内机关、同步动画和物理外力 |
| `scripts/prefabs/carnivalgame_golf_tee.lua` | `SetIsCustomizable` / `OnDeactivateGame` | tee、球杆、退 token 与自定义球场入口 |
| `scripts/components/golfclub.lua` | `StartAiming` / `OnSwingHit` | 高尔夫球杆瞄准与挥杆入口 |
| `scripts/components/golfable.lua` | `OnHit` / `OnExternalPhysics` | 高尔夫球受击和外力入口 |
| `scripts/components/yoth_knightmanager.lua` | `IsKnightShrineActive` | Year of the Horse 管理器样例 |
| `scripts/components/leader.lua` | `AddFollower` | 追随者集合 |
| `scripts/components/follower.lua` | `SetLeader` | 追随关系和忠诚时间 |
| `scripts/components/combat.lua` | `TryRetarget` / `SetTarget` | 敌对目标 |

### `0x63032111` 活动开关锚点 / `scripts/constants.lua` / 搜索信号

`IsSpecialEventActive(event)` 返回 `WORLD_SPECIAL_EVENT == event` 或 `WORLD_EXTRA_EVENTS[event] == true`。
`GetValidRecipe`、prefab 装配、菜单皮肤、世界组件和战利品逻辑都会直接调用它。
这意味着活动不是只影响前端主题，也会影响 recipe、loot、spawner 和 prefab 行为。

### `0x63032211` 世界组件锚点 / `scripts/prefabs/forest.lua` / 搜索信号

`forest.lua` 在 master sim 上添加 `carnivalevent`、`yotd_raceprizemanager`、`yotc_raceprizemanager`、
`yotb_stagemanager`、`yoth_knightmanager` 和 `yoth_hecklermanager`。
其中有些组件会一直存在，但内部逻辑仍用 `IsSpecialEventActive` 或 shrine/stage 状态判断是否生效。

## `0x63033000` 运行流程

~~~mermaid
flowchart TD
    A["constants.lua: SPECIAL_EVENTS"]
    A --> B["simutil.lua: ApplySpecialEvent / ApplyExtraEvent"]
    B --> C["mainfunctions.lua: SPECIAL_EVENT_GLOBAL_PREFABS"]
    B --> D["gamelogic.lua: backend/frontend event prefabs"]
    D --> E["event_deps.lua: SPECIAL_EVENT_DEPS"]
    B --> F["forest.lua: world event components"]
    F --> G["carnivalevent.lua / YOT managers"]
    G --> H["event prefabs: plaza, shrine, stage, race"]
    H --> I["leader/follower/combat/tag checks"]
    G --> J["carnivalgame_golfgame / golfclub / golfable"]
~~~

### `0x63033111` 活动依赖加载 / Global、backend 与 Frontend / 边界条件

`GlobalInit` 加载 `SPECIAL_EVENT_GLOBAL_PREFABS`。
`gamelogic.lua` 在加载世界时会遍历 `SPECIAL_EVENTS`，加载每个活动的 backend prefab，
因为世界设置可以覆盖活动。
frontend prefab 则跟随 `SPECIAL_EVENT_FRONTEND_PREFABS`。
活动依赖资源集中在 `prefabs/event_deps.lua`。

### `0x63033211` Carnival 样例 / `carnivalevent` 到 `carnival_plaza` / 验证点

`carnivalevent` 是 master sim 组件。
`SpawnCarnivalHost` 先检查 `IsSpecialEventActive(SPECIAL_EVENTS.CARNIVAL)`。
`carnival_plaza` 在活动开启时添加 `activatable`，并通过 `TheWorld.components.carnivalevent:RegisterPlaza`
登记 plaza。
这条链路能说明活动逻辑如何从全局开关落到具体世界实体。

### `0x63033221` Carnival 高尔夫样例 / `golfclub` 到 `golfable` / 验证点

`carnivalgame_golfgame` 负责球场边界、`courseparts`、`trackedcourseparts`、`SaveCourseData` 与分数。
`recipes.lua` 先通过 `carnivalgame_golfgame_kit_*` 提供球场套件，再用 `TECH.CARNIVAL_GOLFPROPS_ONE` 限制场内道具配方。
`componentactions.lua` 的 `golfclub` collector 产生 `ACTIONS.GOLF_START_AIMING`。
`actions.lua` 再把瞄准交给 `components/golfclub.lua`，挥杆后由 `golfable:OnHit` 推动物理速度。
`TERRAFORM_REMOVE` 是同一玩法的移除分支，它要求目标带 `terraformerremoveable` 组件。
高尔夫链路把球场、视觉子实体和 placement blocker 通过 `proxy_destroy_entity` 绑定回球场。
`carnivalgame_golfprops.lua` 对 spring 和 moving wall 提供客户端动画同步、平面 decal 和更明确的 golfable 搜索测试。
`carnivalgame_golfball.lua` 只在贴近地面时处理洞口吸附和停止检测，避免空中状态误入洞逻辑。
`carnivalgame_golf_tee.lua` 把自定义模式的退 token 放到 deactivate 阶段，并在 asleep 时跳过不必要的视觉特效。

### `0x63033311` 阵营与目标 / `Leader`、`Follower` 与 `Combat` / 验证点

`Leader:AddFollower` 会把 follower 加进 leader 的集合，并调用 `follower.components.follower:SetLeader(self.inst)`。
`Follower:SetLeader` 会处理原 leader、item source、leash、`leaderchanged` 事件和友军 target 清理。
`Combat:TryRetarget` 只在 target fn 返回可用目标时更新 target。
所以阵营关系通常是“谁跟随谁”和“谁可被设为 target”的组合，而不是单一全局状态。

## `0x63034111` 结构细节 / 活动对配方和资源的影响 / `require_special_event` / 需要核对的字段

`Recipe` 支持 `require_special_event`。
`GetValidRecipe` 会用 `IsSpecialEventActive` 过滤它。
`simutil.lua` 的 `ApplyEvent` 还会把活动对应的 `TECH` 解锁为可用。
因此活动配方要同时核对 `constants.lua`、`recipe.lua`、`recipes.lua` 和世界设置。

## `0x63034211` 结构细节 / 活动 Prefab 的生命周期 / `carnival_host` 与 Plaza / 需要核对的事件

`carnival_host` 自身也检查 `IsSpecialEventActive(SPECIAL_EVENTS.CARNIVAL)`。
`carnival_plaza` 在 `RegisterPlaza` 后推动 `ms_carnivalplazabuilt`。
host 监听这个事件后可被召唤到 plaza。
阅读活动 prefab 时，要先找世界组件如何登记实体，再看 prefab 自己的 stategraph 和 brain。

## `0x63034311` 结构细节 / 年度活动 Manager / `yoth_knightmanager` / 搜索信号

`yoth_knightmanager` 监听 `ms_knightshrineactivated` 和 `ms_knightshrinedeactivated`。
`IsKnightShrineActive` 同时要求有 shrine 且 `IsSpecialEventActive(SPECIAL_EVENTS.YOTH)`。
这类 manager 说明事件系统经常把“世界中有触发实体”和“活动开关开启”组合成实际可用状态。

## `0x63035100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "SPECIAL_EVENTS|WORLD_SPECIAL_EVENT|WORLD_EXTRA_EVENTS|IsSpecialEventActive" \
  scripts/constants.lua \
  scripts/simutil.lua

rg -n "SPECIAL_EVENT_GLOBAL_PREFABS|SPECIAL_EVENT_BACKEND_PREFABS|SPECIAL_EVENT_FRONTEND_PREFABS" \
  scripts/mainfunctions.lua \
  scripts/gamelogic.lua \
  scripts/prefabs/event_deps.lua

rg -n "carnivalevent|RegisterPlaza|SpawnCarnivalHost|IsKnightShrineActive" \
  scripts/prefabs/forest.lua \
  scripts/components/carnivalevent.lua \
  scripts/prefabs/carnival_plaza.lua \
  scripts/components/yoth_knightmanager.lua

rg -n "carnivalgame_golfgame_kit|CARNIVAL_GOLFPROPS_ONE|IsGolfPropWithinGolfArea" \
  scripts/recipes.lua

rg -n "GOLF_START_AIMING|GOLF_START_CHARGING|TERRAFORM_REMOVE|golfclub|golfable" \
  scripts/actions.lua \
  scripts/componentactions.lua \
  scripts/components/golfclub.lua \
  scripts/components/golfable.lua \
  scripts/components/terraformerremoveable.lua \
  scripts/prefabs/carnivalgame_golfgame.lua \
  scripts/prefabs/carnivalgame_golfball.lua \
  scripts/prefabs/carnivalgame_golfprops.lua \
  scripts/prefabs/carnivalgame_golf_tee.lua

rg -n "AddFollower|SetLeader|TryRetarget|SetTarget|leaderchanged" \
  scripts/components/leader.lua \
  scripts/components/follower.lua \
  scripts/components/combat.lua
~~~

### `0x63035111` 推荐顺序 / 最小闭环

先从 `constants.lua` 确认活动名和开关函数。
再读 `simutil.lua` 的活动应用函数。
然后读 `mainfunctions.lua` 与 `gamelogic.lua`，确认活动 prefab 何时加载。
最后用 Carnival、Carnival 高尔夫或 YOTH 作为样例，追到世界组件、具体 prefab 和 leader/follower/combat 关系。
