# `0x63010000` 物品与制作

物品与制作页解释一个 recipe 如何从 UI 状态变成服务端 `ACTIONS.BUILD`，再落到背包或世界实体。
这里不做完整配方清单。
完整文件目录仍放在 `0x8000-reference`。

## `0x63011111` 本页定位 / 要回答的运行时问题 / `Recipe2` 到 `Builder:DoBuild` / 验证点

关键问题不是“某个物品在哪一行定义”，而是 `Recipe2`、`AllRecipes`、`Builder`、`Inventory` 和 `SpawnPrefab`
分别在什么时候参与。
`scripts/recipe.lua` 创建配方对象。
`scripts/recipes.lua` 批量声明官方配方。
`scripts/widgets/redux/craftingmenu_hud.lua` 只计算可见状态和点击入口。
服务端权威结果在 `scripts/components/builder.lua`。

## `0x63011211` 本页定位 / 与前端制作页的边界 / 本页只追玩法副作用 / 不重复 UI 细节

`0x700000` 区域解释 crafting UI 面板、筛选和显示。
本页只把 UI 当作输入端，重点核对 `builder_replica`、RPC、`Builder:MakeRecipe*`、`Builder:DoBuild`
和 `Inventory:GiveItem`。

## `0x63012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/recipe.lua` | `Recipe2` / `GetValidRecipe` | 配方对象与活动过滤 |
| `scripts/recipes.lua` | `Recipe2(...)` | 官方配方声明 |
| `scripts/crafting_sorting.lua` | `CraftableSort` | 菜单排序与 build state 输入 |
| `scripts/widgets/redux/craftingmenu_hud.lua` | `RebuildRecipes` | client 侧可制作状态 |
| `scripts/components/builder_replica.lua` | `MakeRecipeFromMenu` | client 到 server 的分流 |
| `scripts/components/playercontroller.lua` | `RemoteMakeRecipe*` | 发送 recipe `rpc_id` |
| `scripts/networkclientrpc.lua` | `MakeRecipeFromMenu` | server 解析 `AllRecipes` |
| `scripts/components/builder.lua` | `MakeRecipe` / `DoBuild` | 权威制作执行 |
| `scripts/actions.lua` | `ACTIONS.BUILD.fn` | `BufferedAction` 到 `DoBuild` |
| `scripts/components/inventory.lua` | `GiveItem` | 产物进入背包或容器 |

### `0x63012111` 配方数据锚点 / `scripts/recipe.lua` / 搜索信号

`Recipe2` 继承 `Recipe`，最终把对象写入 `AllRecipes[name]`。
`GetValidRecipe` 会拒绝 deconstruction recipe，并检查 `require_special_event` 是否由 `IsSpecialEventActive`
满足。
因此活动配方不能只看 `recipes.lua` 是否声明，还要看世界活动开关。

### `0x63012211` 执行锚点 / `scripts/components/builder.lua` / 搜索信号

`Builder:MakeRecipe` 不是直接生成物品。
它创建 `BufferedAction(self.inst, nil, ACTIONS.BUILD, ...)` 并推给 `locomotor`。
真正扣材料、`SpawnPrefab(recipe.product)`、`builditem` / `buildstructure` 事件和 `prod:OnBuilt` 在
`Builder:DoBuild` 中发生。

## `0x63013000` 运行流程

~~~mermaid
flowchart TD
    A["recipes.lua: Recipe2(...)"]
    A --> B["recipe.lua: AllRecipes[name]"]
    B --> C["craftingmenu_hud.lua: RebuildRecipes"]
    C --> D["builder_replica.lua: MakeRecipeFromMenu / MakeRecipeAtPoint"]
    D --> E["playercontroller.lua: RemoteMakeRecipe*"]
    E --> F["networkclientrpc.lua: lookup recipe.rpc_id"]
    F --> G["builder.lua: MakeRecipe* / BufferBuild"]
    G --> H["BufferedAction(ACTIONS.BUILD)"]
    H --> I["actions.lua: ACTIONS.BUILD.fn"]
    I --> J["builder.lua: DoBuild"]
    J --> K["RemoveIngredients + SpawnPrefab"]
    K --> L["inventory.lua: GiveItem or world placement"]
~~~

### `0x63013111` 菜单制作 / `MakeRecipeFromMenu` / 边界条件

菜单制作只适用于没有 placer 的普通物品。
`Builder:MakeRecipeFromMenu` 先要求背包 UI 没有被玩法关闭，再检查 `HasIngredients`、`KnowsRecipe`、
临时科技、角色标签、角色技能和可学习状态。
材料不足时，它还会尝试 `_TryMakeIngredientRecipe`，把缺失材料可被制作的情况变成子配方制作链。

### `0x63013211` 放置制作 / `BufferBuild` 与 `MakeRecipeAtPoint` / 边界条件

带 placer 的建筑和部署物通常先走 `BufferBuild`。
`BufferBuild` 预扣材料并把 `buffered_builds[recname]` 标记为 true。
随后 `MakeRecipeAtPoint` 还要检查 `TheWorld.Map:CanDeployRecipeAtPoint(pt, recipe, rot)`。
这也是船上禁建逻辑、地形测试和旋转合法性进入制作链的地方。

### `0x63013311` 动作执行 / `ACTIONS.BUILD.fn` / 验证点

`ACTIONS.BUILD.fn` 只把动作转回 `act.doer.components.builder:DoBuild(...)`。
状态机负责播放 build 动作和预测边界，但权威的生成、扣材料、事件推送仍在 `Builder:DoBuild`。

## `0x63014111` 结构细节 / `Recipe2` 字段 / 可影响运行结果的字段 / 需要核对的字段

- `product` 决定 `SpawnPrefab(recipe.product)` 的目标 prefab。
- `placer` 决定是否进入放置制作链。
- `builder_tag` / `builder_skill` 会限制角色或技能。
- `require_special_event` 会由 `GetValidRecipe` 检查活动开关。
- `manufactured` 表示生成责任交给 crafting station。
- `dropitem` 会让产物掉到世界，而不是直接 `Inventory:GiveItem`。
- `numtogive` 和 `override_numtogive_fn` 会改变产物数量。

## `0x63014211` 结构细节 / 背包与世界副作用 / `Inventory:GiveItem` 与 `buildstructure` / 需要核对的分支

`Builder:DoBuild` 对 `prod.components.inventoryitem` 做分支。
有 `inventoryitem` 的产物会尝试装备、堆叠或 `GiveOrDropItem`。
没有 `inventoryitem` 的产物会设置位置和旋转，并推送 `buildstructure` 与 `onbuilt`。
因此“制作成功”不总是等于“物品进入背包”。

## `0x63014311` 结构细节 / 海洋配方交界 / `boat_item`、`anchor_item` 与 `mast_item` / 搜索信号

海洋物品仍由 `recipes.lua` 声明。
例如 `boat_item`、`anchor_item`、`mast_item`、`boat_rotator_kit` 和 `boat_magnet_kit` 都是 `Recipe2`
条目。
这些 recipe 的产物再进入 `scripts/prefabs/boat.lua`、`scripts/prefabs/anchor.lua`
或 `scripts/prefabs/mast.lua` 的 prefab 装配。

## `0x63014411` 结构细节 / 活动高尔夫配方交界 / `carnivalgame_golfgame_kit_*` / 搜索信号

Carnival 高尔夫先用 `carnivalgame_golfgame_kit_easy`、`carnivalgame_golfgame_kit_medium`、
`carnivalgame_golfgame_kit_hard` 和 `carnivalgame_golfgame_kit_diy` 生成球场套件。
场内道具配方使用 `TECH.CARNIVAL_GOLFPROPS_ONE`，并通过对应 prototyper 判断是否处在 `carnivalgame_golfgame` 内。
这些道具配方通常没有普通材料，关键约束在 `testfn` 和 `overridecandeployrecipeatpointfn`。
因此排查高尔夫道具能否放置时，要先读 `recipes.lua` 的 `IsGolfPropWithinGolfArea`，再读 `carnivalgame_golfgame.lua` 的区域边界。
`recipes.lua` 让 cutout 道具使用统一的 `CUTOUT_SMART_RADIUS` 作为 smart radius。
球场 kit 还会保存 `_available_courses`，拆除再投放时可以避开刚用过的 course code。
自定义 tee 的 crafting 入口依赖 `OPEN_CRAFTING` 和 `prototyper.redirect_to_prototyper`，但 tee 自身会移除 `prototyper` tag，避免显示成普通科技站。

## `0x63015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "Recipe2|AllRecipes|GetValidRecipe|require_special_event" \
  scripts/recipe.lua \
  scripts/recipes.lua

rg -n "carnivalgame_golfgame_kit|CARNIVAL_GOLFPROPS_ONE|IsGolfPropWithinGolfArea" \
  scripts/recipes.lua

rg -n "CUTOUT_SMART_RADIUS|ResetAvailableCourses|OPEN_CRAFTING|redirect_to_prototyper|hideprototyperaction" \
  scripts/recipes.lua \
  scripts/actions.lua \
  scripts/prefabs/carnivalgame_golfgame.lua \
  scripts/prefabs/carnivalgame_golf_tee.lua

rg -n "MakeRecipeFromMenu|MakeRecipeAtPoint|BufferBuild|RemoteMakeRecipe" \
  scripts/components/builder_replica.lua \
  scripts/components/playercontroller.lua \
  scripts/networkclientrpc.lua

rg -n "MakeRecipe|DoBuild|ACTIONS.BUILD|GiveItem|SpawnPrefab" \
  scripts/components/builder.lua \
  scripts/actions.lua \
  scripts/components/inventory.lua
~~~

### `0x63015111` 推荐顺序 / 最小闭环

先读 `scripts/recipe.lua` 的 `Recipe2` 和 `GetValidRecipe`。
再读 `scripts/recipes.lua` 中一个简单 `Recipe2` 条目。
如果抽样活动配方，可以读 `carnivalgame_golfgame_kit_easy`，再读 `TECH.CARNIVAL_GOLFPROPS_ONE` 下面的高尔夫道具配方。
然后顺着 `builder_replica` 到 `networkclientrpc` 的 `rpc_id` 查找。
最后读 `Builder:DoBuild`，确认产物到底进入背包、装备栏、世界坐标还是 crafting station 自己处理。
