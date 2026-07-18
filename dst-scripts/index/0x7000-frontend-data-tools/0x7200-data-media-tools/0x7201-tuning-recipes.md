# `0x72010000` Tuning 与 Recipes

`TUNING`、`Recipe2` 和 `AllRecipes` 是数据入口，不是行为入口。
读这类文件时要先确认表如何生成，再追到 UI、replica、RPC handler 和 server component 的读取方。

## `0x72011111` 本页定位 / 要回答的运行时问题 / 数值和配方在哪里变成行为 / 验证点

`scripts/tuning.lua` 只维护 `TUNING`、`TUNING_MODIFIERS` 和 `ORIGINAL_TUNING`。
`scripts/recipe.lua` 负责 `Recipe`、`Recipe2`、`Ingredient` 和 `AllRecipes`。
`scripts/recipes.lua` 用大量 `Recipe2(...)` 调用注册官方配方。
`scripts/components/builder.lua` 与 `scripts/components/builder_replica.lua` 才是制作行为的主要读取方。

## `0x72011211` 本页定位 / Mod API 与官方配方的区别 / `AddRecipe2` / 边界条件

官方 `recipes.lua` 直接调用 `Recipe2(...)`，配方的 `rpc_id` 先来自递增编号。
`AddRecipe2` 是 `scripts/modutil.lua` 暴露给 mod 环境的包装入口，并会调用 `SetModRPCID()`。

## `0x72012000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/tuning.lua` | `TUNING` | 全局数值常量 |
| `scripts/tuning.lua` | `AddTuningModifier` | 延迟 modifier 机制 |
| `scripts/tuning_override.lua` | override table | 覆盖部分 tuning 值 |
| `scripts/recipe.lua` | `Ingredient` | 材料结构 |
| `scripts/recipe.lua` | `Recipe` | 配方基类 |
| `scripts/recipe.lua` | `Recipe2` | 配方构造入口 |
| `scripts/recipe.lua` | `AllRecipes` | 运行时配方表 |
| `scripts/recipes.lua` | `Recipe2(...)` | 官方配方注册列表 |
| `scripts/recipes_filter.lua` | `CRAFTING_FILTER_DEFS` | 制作菜单过滤数据 |
| `scripts/modutil.lua` | `env.AddRecipe2` | mod 配方注册包装 |
| `scripts/components/builder.lua` | `Builder:MakeRecipe` | server 制作实体生成 |
| `scripts/components/builder_replica.lua` | `RemoteMakeRecipeFromMenu` | client 到 server 的制作请求 |
| `scripts/networkclientrpc.lua` | `MakeRecipeFromMenu` | 以 `rpc_id` 还原 recipe |
| `scripts/widgets/redux/craftingmenu_widget.lua` | `AllRecipes` | 前端制作列表 |

### `0x72012111` 主锚点 / `scripts/recipe.lua` / 搜索信号

先搜 `AllRecipes = {}`、`Recipe = Class` 和 `Recipe2 = Class`。
再确认构造函数把 `Recipe` 写入 `AllRecipes[name]`，并给官方配方分配递增 `rpc_id`。

### `0x72012211` 数值锚点 / `scripts/tuning.lua` / 验证点

`TUNING`、`TUNING_MODIFIERS` 和 `ORIGINAL_TUNING` 在文件开头初始化。
`Tune()` 内部会重建 `TUNING` 表，并在文件底部安装 metatable。
modifier 只有在 key 被置为 nil 后才经 `__index` 参与计算。

## `0x72013000` 运行流程

~~~mermaid
flowchart TD
    A["tuning.lua: Tune() builds TUNING"]
    A --> B["components / prefabs read TUNING keys"]
    A --> C["AddTuningModifier stores modifier"]
    C --> D["TUNING __index calculates nil keys"]
    E["recipe.lua defines Ingredient / Recipe / Recipe2"]
    F["recipes.lua calls Recipe2"]
    E --> F
    F --> G["AllRecipes[name]"]
    G --> H["crafting UI and filters"]
    G --> I["builder_replica remote request"]
    I --> J["networkclientrpc restores by rpc_id"]
    J --> K["Builder:MakeRecipe / DoBuild"]
~~~

### `0x72013111` `TUNING` 阶段 / 数值读取方 / 边界条件

不要只停在 `TUNING` 定义处。
如果排查战斗、饥饿或船只数值，要继续搜索具体 key 的读取方。

### `0x72013211` `Recipe2` 阶段 / 配方对象字段 / 验证点

`Recipe` 会根据 `IsCharacterIngredient` 与 `IsTechIngredient` 把材料拆到三个数组。
`product`、`placer`、`testfn`、`canbuild`、`builder_tag`、`builder_skill`、`sg_state` 都会影响后续制作路径。
`limitedamount` 与 `getlimitedrecipecount` 还会进入制作站限制的网络同步路径。

### `0x72013311` UI 与 Builder 阶段 / `AllRecipes` 的读取方 / 验证点

制作 UI 读取 `AllRecipes` 做展示、搜索、pin 和过滤。
client 侧通过 `builder_replica` 发起制作请求。
server 侧 `networkclientrpc.lua` 用 recipe `rpc_id` 还原对象，再交给 `components/builder.lua` 做材料扣除和实体生成。

## `0x72014111` 结构细节 / `tuning.lua` / Modifier 机制 / 需要核对的字段

`TUNING_MODIFIERS` 保存 modifier 函数和基础值。
`ORIGINAL_TUNING` 记录被 modifier 改写前的值，在 `tuning.lua` 中主要作为全局表初始化并随 `Tune()` 重置。
读数值异常时要检查是否有 modifier，而不只看字面常量。

## `0x72014211` 结构细节 / `recipe.lua` / `Recipe` 构造函数 / 需要核对的字段

`Recipe` 构造函数会设置 `rpc_id`。
官方配方默认使用递增编号。
mod 配方通过 `env.AddRecipe2()` 调用 `SetModRPCID()`，避免和官方编号路径混淆。
`networkclientrpc.lua` 的 `MakeRecipeFromMenu`、`MakeRecipeAtPoint` 与 `BufferBuild` 会按 `rpc_id` 遍历 `AllRecipes`。

## `0x72014311` 结构细节 / `recipes.lua` / 官方数据列表 / 需要核对的字段

`recipes.lua` 顶部 `require("recipe")`。
随后定义 `PROTOTYPER_DEFS`、placer test function、角色限制与活动配方，并批量调用 `Recipe2`。
配方显示分类更多要回到 `recipes_filter.lua` 与制作 UI 读取方核对。
Carnival 高尔夫 cutout 道具使用统一的 `CUTOUT_SMART_RADIUS`。
每个 cutout 不维护独立半径表。

## `0x72014411` 结构细节 / Tuning 键 / Desiccant 与 Fumarole / 验证点

`tuning.lua` 定义 `DESICCANT_HELD_TEMPERATURE`、`DESSICANT_MIN_TEMPERATURE` 和 `DESSICANT_THRESHOLD_TEMPERATURE`。
这些键由 `prefabs/desiccant.lua` 读取，用来决定干燥剂是否继续外部控制含水量。
`TRAP_FUMAROLE_TEMP_MODIFIER` 为 `-20`。
`trap_fumarole.lua` 和 `standardcomponents.lua` 会读取 `inventoryitemtemperature.externalheaterpower`。
它们会按该值对 fumarole 温度 modifier 做线性调节。

## `0x72015100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "TUNING = \\{|TUNING_MODIFIERS|AddTuningModifier|setmetatable\\(TUNING" \
  scripts/tuning.lua

rg -n "Ingredient = Class|Recipe = Class|Recipe2 = Class|AllRecipes\\[name\\]" \
  scripts/recipe.lua

rg -n "Recipe2\\(|PROTOTYPER_DEFS|env.AddRecipe2" \
  scripts/recipes.lua scripts/modutil.lua

rg -n \
  -e "CUTOUT_SMART_RADIUS" \
  -e "DESICCANT_HELD_TEMPERATURE|DESSICANT_MIN_TEMPERATURE|DESSICANT_THRESHOLD_TEMPERATURE" \
  -e "TRAP_FUMAROLE_TEMP_MODIFIER" \
  scripts/recipes.lua \
  scripts/tuning.lua \
  scripts/prefabs/desiccant.lua \
  scripts/prefabs/trap_fumarole.lua \
  scripts/standardcomponents.lua

rg -n "MakeRecipeFromMenu|MakeRecipeAtPoint|BufferBuild|rpc_id" \
  scripts/components/builder.lua \
  scripts/components/builder_replica.lua \
  scripts/networkclientrpc.lua
~~~

### `0x72015111` 推荐顺序 / 最小闭环

抽样一个 `Recipe2("lighter", ...)`。
先在 `recipes.lua` 看注册参数。
再到 `recipe.lua` 看字段落位和 `rpc_id`。
接着到 `craftingmenu_widget.lua` 看展示。
最后沿 `builder_replica.lua -> networkclientrpc.lua -> components/builder.lua` 看它如何变成 server 行为。
