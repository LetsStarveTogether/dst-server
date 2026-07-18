# `0x70000000` 前端数据与工具

本区把前端栈、HUD、制作界面、静态数据、媒体特效和调试工具放在同一条阅读线上。
阅读时先判断 Lua 文件是在消费输入、维护 screen 栈、展示数据、注册数据，还是请求权威端改变实体。

目录级语义由本 README 承载，独立专题文件只解释具体运行链路。

## `0x70001111` 区域定位 / 读者要解决的问题 / 运行时入口与数据入口 / 验证点

`scripts/frontend.lua` 和 `scripts/input.lua` 是前端运行入口。
`scripts/tuning.lua`、`scripts/recipes.lua`、`scripts/strings.lua` 是数据入口。
`scripts/fx.lua`、`scripts/skin_assets.lua`、`scripts/screens/redux/scrapbookdata.lua` 更接近注册表或展示素材。

## `0x70001211` 区域定位 / 本区边界 / 不把 UI 误读成权威 Gameplay / 边界条件

HUD 和 widget 可以消费输入、播放声音、打开 screen、改变本地 UI 状态，或通过 replica / `playercontroller` 请求远端制作。
真正改变世界的权威逻辑通常仍落在 server component、`playercontroller` RPC 或 prefab 行为里。

## `0x70002000` 源码锚点

| 文件 | 入口 | 用途 |
| --- | --- | --- |
| `scripts/input.lua` | `Input:OnControl` | 先让 `TheFrontEnd` 消费输入 |
| `scripts/frontend.lua` | `FrontEnd` | 维护 `screenstack`、焦点、fade 和 debug panel |
| `scripts/screens/playerhud.lua` | `PlayerHud` | 游戏内 HUD screen |
| `scripts/widgets/widget.lua` | `Widget` | UI 树、焦点和 `OnControl` 递归 |
| `scripts/widgets/controls.lua` | `Controls` | HUD 控件集合 |
| `scripts/components/playercontroller.lua` | `PlayerController` | 地图控制、放置模式和远端制作请求 |
| `scripts/recipe.lua` | `Recipe2` | 配方对象与 `AllRecipes` |
| `scripts/recipes.lua` | `Recipe2(...)` | 官方配方注册 |
| `scripts/tuning.lua` | `TUNING` | 数值常量与 modifier |
| `scripts/strings.lua` | `STRINGS` | 文本表 |
| `scripts/translator.lua` | `TranslateStringTable` | 递归替换文本表 |
| `scripts/skin_assets.lua` | `skin_assets` | 皮肤资产列表 |
| `scripts/screens/redux/scrapbookdata.lua` | generated table | Scrapbook 展示数据 |
| `scripts/fx.lua` | `fx` table | 通用 FX 数据表 |
| `scripts/prefabs/fx.lua` | `MakeFx` | 把 `fx` table 变成 prefab |
| `scripts/util.lua` | `DebugSpawn` | 调试生成实体 |
| `scripts/consolecommands.lua` | `c_` functions | 控制台命令入口 |

### `0x70002111` 锚点读取顺序 / 从输入到展示 / 搜索信号

先读 `Input:OnControl`、`FrontEnd:OnControl`、`FrontEnd:PushScreen`。
再读 `Widget:OnControl`、`PlayerHud:OnControl` 和 `Controls:ToggleMap`。

### `0x70002211` 从数据到展示 / 配方、文本和素材 / 验证点

数据页应先找注册点，再找读取方。
例如 `Recipe2` 写入 `AllRecipes`，`CraftingMenuHUD:RebuildRecipes` 生成 `valid_recipes`，制作 UI 再按搜索、过滤和详情面板展示。

## `0x70003000` 运行关系图

~~~mermaid
flowchart TD
    A["engine input callback"]
    A --> B["TheInput:OnControl"]
    B --> C["TheFrontEnd:OnControl"]
    C --> D["top screen OnControl"]
    D --> E["Widget focus tree"]
    D --> F["PlayerHud shortcuts"]
    B --> G["Input event handlers when UI did not consume"]
    F --> H["Controls / crafting menu / map / chat"]
    H --> I["replica builder or playercontroller request"]
    I --> K["server component / RPC / placement"]
    J["TUNING / STRINGS / Recipe2 / fx table"]
    J --> H
~~~

### `0x70003111` 关系图读法 / 前端消费优先 / 边界条件

如果 `TheFrontEnd:OnControl` 返回 `true`，输入不会继续派发到 `Input.oncontrol`。
这也是排查 UI 挡住 gameplay 操作时最先验证的分叉。

### `0x70003211` 数据只解释展示 / 数据表与执行方分离 / 验证点

`TUNING`、`STRINGS`、`skin_assets`、`scrapbookdata` 自身不执行 gameplay。
需要顺着读取方追到 component、prefab、screen、widget 或 `playercontroller` 请求入口。

## `0x70004111` 目录索引 / README 载体 / 二级目录 / 链接校验

以下入口先进入目录 README，再进入具体专题文件。

- [Frontend UI](0x7100-frontend-ui/README.md)
- [Frontend 与输入](0x7100-frontend-ui/0x7101-frontend-input.md)
- [Screens Widgets HUD](0x7100-frontend-ui/0x7102-screens-widgets-hud.md)
- [Crafting UI](0x7100-frontend-ui/0x7103-crafting-ui.md)
- [数据媒体与工具](0x7200-data-media-tools/README.md)
- [Tuning 与 Recipes](0x7200-data-media-tools/0x7201-tuning-recipes.md)
- [Localization Skins Scrapbook](0x7200-data-media-tools/0x7202-localization-skins-scrapbook.md)
- [媒体 FX 与 Audio](0x7200-data-media-tools/0x7203-media-fx-audio.md)
- [Tools Debug](0x7200-data-media-tools/0x7204-tools-debug.md)

## `0x70005100` 阅读与验证路线 / 从哪里开始读源码

~~~bash
rg -n "function Input:OnControl|function FrontEnd:OnControl|function FrontEnd:PushScreen" \
  scripts/input.lua scripts/frontend.lua

rg -n "function PlayerHud:OnControl|function Controls:ToggleMap|IsMapControlsEnabled|function DoRecipeClick" \
  scripts/screens/playerhud.lua scripts/widgets/controls.lua \
  scripts/components/playercontroller.lua scripts/widgets/widgetutil.lua

rg -n "Recipe2\\(|TranslateStringTable|function DebugSpawn|local fx =" \
  scripts/recipes.lua scripts/translator.lua scripts/util.lua scripts/fx.lua
~~~

### `0x70005111` 最小闭环 / 抽样动作

抽样一条 `CONTROL_MAP`。
从 `Input:OnControl` 追到 `PlayerHud:OnControl`，再追到 `Controls:ToggleMap`、`IsMapControlsEnabled` 和 `TheFrontEnd:PushScreen`。
抽样一条制作按钮。
从 `CraftingMenuDetails:_MakeBuildButton` 追到 `DoRecipeClick`、`replica.builder:MakeRecipeFromMenu` 和 `playercontroller:RemoteMakeRecipeFromMenu`。
