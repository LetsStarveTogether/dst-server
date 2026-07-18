# `0x71030000` 制作 UI

制作 UI 的核心不是配方表本身，而是“配方注册、可见性 meta、展示、点击、replica 请求、server builder 执行”的链路。
读这条链路时要特别区分普通物品制作、材料不足时的子材料制作，以及带 placer 的放置型制作。

## `0x71031111` 本页定位 / 要回答的运行时问题 / 点击制作按钮后发生什么 / 验证点

`CraftingMenuDetails:_MakeBuildButton` 调用 `DoRecipeClick`。
`DoRecipeClick` 通过 `owner.replica.builder` 判断已知配方、材料、buffered build 和 placer 分支。

## `0x71031211` 本页定位 / UI 与权威执行 / `builder_replica` 到 `builder` / 边界条件

`builder_replica.lua` 中的 `Builder:MakeRecipeFromMenu` 优先调用本地 `components.builder`。
如果本地没有 server builder，则走 `components.playercontroller:RemoteMakeRecipeFromMenu`。
带 `recipe.placer` 的配方不会在菜单点击时直接生成最终 prefab，而是先进入 `StartBuildPlacementMode`。

## `0x71032000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/recipe.lua` | `Recipe2` | 构造配方并写入 `AllRecipes` |
| `scripts/recipes.lua` | `Recipe2(...)` | 官方配方数据 |
| `scripts/widgets/redux/craftingmenu_hud.lua` | `CraftingMenuHUD` | HUD 制作菜单开关 |
| `scripts/widgets/redux/craftingmenu_widget.lua` | `CraftingMenuWidget` | 搜索、过滤、详情面板 |
| `scripts/widgets/redux/craftingmenu_details.lua` | `_MakeBuildButton` | 制作按钮点击入口 |
| `scripts/widgets/widgetutil.lua` | `DoRecipeClick` | 制作点击的共享决策函数 |
| `scripts/components/builder_replica.lua` | `Builder:MakeRecipeFromMenu` | client 侧请求路径 |
| `scripts/components/builder.lua` | `Builder:MakeRecipeFromMenu` | server 侧执行入口 |
| `scripts/components/playercontroller.lua` | `StartBuildPlacementMode` | placer 预览和放置路径 |
| `scripts/components/playercontroller.lua` | `RemoteMakeRecipeAtPoint` | 放置型制作的远端请求 |

### `0x71032111` 主锚点 / `scripts/widgets/widgetutil.lua` / 搜索信号

先搜 `function DoRecipeClick`。
这里能看到 `busy`、`buffered`、`knows`、`has_ingredients`、`recipe.placer`、`CanCraftIngredient` 和 `SetCraftingAutopaused`。

### `0x71032211` UI 锚点 / `scripts/widgets/redux/craftingmenu_details.lua` / 验证点

制作按钮的 `SetOnClick` 会读取 skin spinner，并先记录是否已经 buffered。
非长按路径会调用 `DoRecipeClick(self.owner, self.data.recipe, skin)`。
如果 `DoRecipeClick` 返回不保持打开，并且配置允许 buffered build 自动关闭，按钮会调用 `owner.HUD:CloseCrafting()`。

## `0x71033000` 运行流程

~~~mermaid
flowchart TD
    A["Recipe2 in recipes.lua"]
    A --> B["AllRecipes in recipe.lua"]
    B --> C["CraftingMenuHUD:RebuildRecipes"]
    C --> D["valid_recipes + meta.build_state"]
    D --> E["CraftingMenuWidget filter/search/detail"]
    E --> F["CraftingMenuDetails build button"]
    F --> G["DoRecipeClick"]
    G --> H{"recipe.placer?"}
    H -->|no| I["replica.builder:MakeRecipeFromMenu"]
    H -->|yes| J["BufferBuild + StartBuildPlacementMode"]
    I --> K{"server builder local?"}
    K -->|yes| L["components.builder:MakeRecipeFromMenu"]
    K -->|no| M["playercontroller:RemoteMakeRecipeFromMenu"]
    J --> N["MakeRecipeAtPoint / RemoteMakeRecipeAtPoint"]
~~~

### `0x71033111` 数据注册阶段 / `Recipe2` / 验证点

`Recipe2` 继承 `Recipe`。
`Recipe` 构造函数会把普通材料、角色材料和科技材料拆到不同字段。
它最终把对象写入 `AllRecipes[name]`。

### `0x71033211` UI 展示阶段 / `CraftingMenuHUD` / 验证点

`CraftingMenuHUD:RebuildRecipes` 遍历 `AllRecipes`，并为每个可见配方写入 `valid_recipes[recipe.name].meta`。
`meta.build_state` 会区分 `buffered`、`freecrafting`、`has_ingredients`、`no_ingredients`、`prototype`、`hint` 和 `hide`。
`CraftingMenuWidget:OnUpdate` 只对搜索缓存分批处理，每轮最多验证 30 个 recipe，避免搜索时一次性扫完整表。

### `0x71033311` 执行阶段 / `DoRecipeClick` / 边界条件

普通物品可以直接走 `MakeRecipeFromMenu`。
有 `recipe.placer` 的配方通常先 `BufferBuild`，再由 `playercontroller` 进入放置模式。
如果材料不足，`DoRecipeClick` 会尝试 `CanCraftIngredient` 并让 server builder 先制作当前配方需要的子材料。
如果配方还未解锁但当前科技可原型，成功制作后会触发研究机和解锁逻辑。

## `0x71034111` 结构细节 / `CraftingMenuHUD` / 打开和关闭 / 需要核对的字段

`CraftingMenuHUD:Open` 会设置 `TheFrontEnd.crafting_navigation_mode = true`。
它会启用 UI root、刷新 help text、打开菜单动画，并调用 `SetCraftingAutopaused(true)`。
`CraftingMenuHUD:Close` 会保存 `TheCraftingMenuProfile` 并关闭 crafting autopause。
打开时如果传入搜索模式，会把焦点放到 `craftingmenu.search_box`。
关闭时会禁用 `ui_root`、`craftingmenu` 和 `pinbar`。

## `0x71034211` 结构细节 / `BuilderReplica` / 本地和远端分叉 / 需要核对的字段

如果实体有 `components.builder`，replica 直接转给本地 server builder。
否则它通过 `components.playercontroller` 发送远端制作请求。
同一个 replica 文件还提供 `MakeRecipeAtPoint`，供放置型制作在确定坐标后使用。

## `0x71034311` 结构细节 / `Builder` / `MakeRecipeFromMenu` / 边界条件

server builder 会先检查 inventory 是否由自己打开，再检查材料、已知配方和可原型科技。
普通制作成功路径最终进入 `Builder:MakeRecipe`。
放置型制作要等 `Builder:MakeRecipeAtPoint` 看到 buffered build、合法坐标和 `CanDeployRecipeAtPoint` 后才生成最终结果。
失败路径会停在材料、解锁、原型条件或部署坐标校验上。

## `0x71035100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "Recipe2 = Class|AllRecipes\\[name\\]|function Recipe" \
  scripts/recipe.lua

rg -n "function DoRecipeClick|MakeRecipeFromMenu|BufferBuild|StartBuildPlacementMode|RemoteMakeRecipeAtPoint" \
  scripts/widgets/widgetutil.lua scripts/components/builder_replica.lua \
  scripts/components/playercontroller.lua

rg -n "_MakeBuildButton|function CraftingMenuHUD:Open|function CraftingMenuHUD:RebuildRecipes" \
  scripts/widgets/redux/craftingmenu_details.lua \
  scripts/widgets/redux/craftingmenu_hud.lua

rg -n "function CraftingMenuWidget:OnUpdate" \
  scripts/widgets/redux/craftingmenu_widget.lua
~~~

### `0x71035111` 推荐顺序 / 最小闭环

抽样 `lighter` 这种没有 placer 的配方。
确认点击后走 `MakeRecipeFromMenu`。
再抽样一个有 `placer` 的建筑配方。
确认它先走 `BufferBuild` 和 `StartBuildPlacementMode`，而不是立刻生成最终 prefab。
继续追到放置确认后的 `MakeRecipeAtPoint` 或 `RemoteMakeRecipeAtPoint`，再回到 server builder 的 `MakeRecipeAtPoint`。
